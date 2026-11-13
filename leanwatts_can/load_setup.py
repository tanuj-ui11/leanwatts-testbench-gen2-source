"""Calibration connection settings and optional read-only load diagnostics."""
from __future__ import annotations

import copy
import json
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from .automation import AutomationSettings
from .maynuo import LoadIdentity, LoadReading, MaynuoLoad, SerialSettings
from . import theme
from .version import PRODUCT, VERSION


class LoadSetupPage(QWidget):
    configured_changed = Signal(dict)
    CALIBRATION_PROFILE = "m8437b-008c-002a"
    PROFILE_IDENTITY = LoadIdentity(140, 42)
    PROFILE_MODES = {"CC": 0x3B01, "CV": 0x3B02}

    def __init__(self, data_dir: Path, parent=None, load_factory=None):
        super().__init__(parent)
        self._path = Path(data_dir) / "station-load-setup.json"
        self._load_factory = load_factory or MaynuoLoad
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="load-probe")
        self._future = None
        self._disposed = False
        self._restoring = True
        self._saved = {}
        self._identity = None
        self._observations = {}
        self._profile_selected = True
        self._available_ports = []
        self._port_dialog = None
        self._last_diagnostics = None
        layout = QVBoxLayout(self)
        title = QLabel("Load setup")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        description = QLabel("Select the load COM port once for this station. The session screen handles charger parameters, calibration and post-calibration tests.")
        description.setWordWrap(True)
        layout.addWidget(description)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._build_load_tab()
        self._build_duty_tab()
        self.tabs.currentChanged.connect(lambda index: self.save_button.setVisible(index != 0))
        self.status = QLabel("Select the USB serial port. The known load profile is confirmed when calibration connects.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.copy_diagnostics_button = QPushButton("Copy diagnostics")
        self.copy_diagnostics_button.setEnabled(False)
        self.copy_diagnostics_button.clicked.connect(self.copy_diagnostics)
        row.addWidget(self.copy_diagnostics_button)
        self.save_button = QPushButton("Save settings")
        self.save_button.setObjectName("primaryButton")
        self.save_button.clicked.connect(self.save_settings)
        row.addStretch()
        row.addWidget(self.save_button)
        layout.addLayout(row)
        self.save_button.setVisible(False)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._finish_probe)
        self._restore()
        self._restoring = False

    def _build_load_tab(self):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 6, 0, 0)
        card = theme.Card("Load bank connection", "Maynuo M8437B")
        page_layout.addWidget(card)
        page_layout.addStretch()
        box = card.body
        connection = QHBoxLayout()
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumWidth(115)
        self.port.setPlaceholderText("COM port")
        self.port.lineEdit().setPlaceholderText("Choose COM")
        self.port.setToolTip("Click Find ports to choose from a visible list, or type the known load COM port.")
        self.baud = QComboBox()
        self.baud.addItems([str(v) for v in (2400, 9600, 14400, 28800, 57600, 115200)])
        self.baud.setCurrentText("9600")
        self.address = QSpinBox()
        self.address.setRange(1, 200)
        self.parity = QComboBox()
        self.parity.addItems(["N", "E", "O"])
        for text, widget in (("Port", self.port), ("Baud", self.baud), ("Address", self.address), ("Parity", self.parity)):
            connection.addWidget(QLabel(text))
            connection.addWidget(widget)
        self.refresh_button = QPushButton("Find ports")
        self.refresh_button.clicked.connect(self.choose_port)
        connection.addWidget(self.refresh_button)
        box.addLayout(connection)
        note = QLabel("On the load: Shift → 0 (Menu) → CONFIG → BAUDRATE SET / ADDRESS SET / COMM.PARITY. Match these values here; NONE = N, EVEN = E, ODD = O.")
        note.setWordWrap(True)
        box.addWidget(note)
        profile_note = QLabel("Device profile: model 0x008C / firmware 0x002A · CC 0x3B01 / CV 0x3B02. The connected identity is confirmed before calibration starts.")
        profile_note.setWordWrap(True)
        self.profile_button = QPushButton("Save calibration connection")
        self.profile_button.setObjectName("Primary")
        self.profile_button.clicked.connect(self.save_calibration_connection)
        box.addWidget(self.profile_button)
        automatic_note = QLabel("Save this connection once, then return to Auto Calibration Session. Calibration learns the charger response automatically.")
        automatic_note.setWordWrap(True)
        box.addWidget(automatic_note)
        diagnostic_page = QWidget()
        diagnostic_box = QVBoxLayout(diagnostic_page)
        diagnostic_box.addWidget(profile_note)
        row = QHBoxLayout()
        self.probe_button = QPushButton("1 · Read load")
        self.cc_button = QPushButton("2 · Read CC mode")
        self.cv_button = QPushButton("3 · Read CV mode")
        for button, kind in ((self.probe_button, "probe"), (self.cc_button, "CC"), (self.cv_button, "CV")):
            button.clicked.connect(lambda checked=False, k=kind: self.start_probe(k))
            row.addWidget(button)
        diagnostic_box.addLayout(row)
        self.instructions = QLabel(
            "Keep load input OFF. On the load: I-SET → keep or re-enter the displayed value → Enter, then click Read CC mode here. "
            "Repeat with V-SET → keep or re-enter the displayed value → Enter, then Read CV mode. "
            "These buttons only read the selected mode; they do not change it."
        )
        self.instructions.setWordWrap(True)
        diagnostic_box.addWidget(self.instructions)
        self.identity_label = QLabel("Expected load: model 0x008C · firmware 0x002A")
        self.identity_label.setWordWrap(True)
        self.reading_label = QLabel("Voltage —     Current —     Input —     Mode code —")
        self.reading_label.setWordWrap(True)
        self.mode_label = QLabel("Calibration profile: CC 0x3B01     CV 0x3B02 · identity confirmed when connecting")
        diagnostic_box.addWidget(self.identity_label)
        box.addWidget(self.reading_label)
        diagnostic_box.addWidget(self.mode_label)
        self.readings_match = QCheckBox("Voltage and current readings match the load front panel")
        self.protocol_approved = QCheckBox("M97-compatible protocol verified on this M8437B bench")
        self.termination_ramp_approved = QCheckBox("Continuous CC reduction verified on this load")
        diagnostic_box.addWidget(self.readings_match)
        diagnostic_box.addWidget(self.protocol_approved)
        diagnostic_box.addWidget(self.termination_ramp_approved)
        termination_note = QLabel("End-current testing requires confirmed current changes while load input stays ON. Leave this unchecked until that behavior is verified.")
        termination_note.setWordWrap(True)
        diagnostic_box.addWidget(termination_note)
        self.manual_setup_button = QPushButton("Save manually captured load setup")
        self.manual_setup_button.clicked.connect(self.save_manual_settings)
        diagnostic_box.addWidget(self.manual_setup_button)
        diagnostic_box.addStretch()
        box.addStretch()
        self.tabs.addTab(page, "USB load")
        self.tabs.addTab(diagnostic_page, "Manual diagnostics")
        for widget in (self.port, self.baud, self.parity):
            widget.currentTextChanged.connect(self._serial_changed)
        self.address.valueChanged.connect(self._serial_changed)
        for widget in (self.readings_match, self.protocol_approved, self.termination_ramp_approved):
            widget.toggled.connect(self._dirty)

    def _build_duty_tab(self):
        page = QWidget()
        box = QVBoxLayout(page)
        note = QLabel("These are the starting trims for the first charger of a variant in each session. Following chargers use the first successful unit's final trims +2, within these bounds. Current tuning can use up to 3.0 duty after learning the response; voltage tuning uses up to 1.0. Near target it aims for 0.1 A and 0.05 V changes. These bounds are trim values, not volts or amps.")
        note.setWordWrap(True)
        box.addWidget(note)
        grid = QGridLayout()
        for column, title in enumerate(("Axis", "Start", "Minimum", "Maximum", "Increasing duty")):
            grid.addWidget(QLabel(title), 0, column)
        self.duty_fields = {}
        self.directions = {}
        for row, axis in enumerate(("current", "voltage"), 1):
            grid.addWidget(QLabel(axis.title()), row, 0)
            for column, suffix in enumerate(("initial", "min", "max"), 1):
                field = QLineEdit({"initial": "80.0", "min": "60.0", "max": "85.0"}[suffix])
                field.setValidator(QDoubleValidator(0, 1000, 1, field))
                field.setPlaceholderText("Enter bound" if suffix != "initial" else "80.0")
                field.textChanged.connect(self._dirty)
                self.duty_fields[f"{axis}_{suffix}"] = field
                grid.addWidget(field, row, column)
            direction = QComboBox()
            direction.addItem("Detect automatically", 0)
            direction.addItem("Raises output (+1)", 1)
            direction.addItem("Lowers output (-1)", -1)
            direction.currentIndexChanged.connect(self._dirty)
            self.directions[axis] = direction
            grid.addWidget(direction, row, 4)
        box.addLayout(grid)
        self.duty_approved = QCheckBox("Enable automatic tuning for a manually captured load setup")
        self.duty_approved.toggled.connect(self._dirty)
        box.addWidget(self.duty_approved)
        detail = QLabel("Tuning uses 3 stable readings between adjustments; saving still requires 8 stable readings at the target. Direction and response are learned from small changes during each run. Manual diagnostics remain available for other load setups.")
        detail.setWordWrap(True)
        box.addWidget(detail)
        box.addStretch()
        self.tabs.addTab(page, "Calibration tuning")

    @property
    def settings_dict(self) -> dict:
        return copy.deepcopy(self._saved)

    def calibration_profile(self, *, for_calibration=True) -> dict:
        """Freeze the selected connection and the previously verified bench map.

        This performs no I/O and does not manufacture a measurement comparison
        or mode observation. The run worker must match PROFILE_IDENTITY before
        it enables control; the codes are not defaults for arbitrary Maynuo loads.
        """
        settings = self._serial_settings()
        automation = self._tuning_settings(approved=True) if for_calibration else asdict(AutomationSettings())
        if for_calibration:
            automation.update(auto_direction=True, stage_timeout=1800.0,
                              maximum_adjustments=300, current_output_step=0.1,
                              voltage_output_step=0.05, tuning_samples=3,
                              tuning_seconds=0.6, max_duty_step=1.0)
        candidate = AutomationSettings(**automation)
        error = candidate.validate(calibration=for_calibration)
        if error:
            raise ValueError(error)
        identity = self.PROFILE_IDENTITY
        return {
            "schema": 1, "calibration_profile": self.CALIBRATION_PROFILE,
            "port": settings.port, "baudrate": settings.baudrate,
            "address": settings.address, "parity": settings.parity,
            "model_code": identity.model_code, "firmware_code": identity.firmware_code,
            "cc_mode_code": self.PROFILE_MODES["CC"],
            "cv_mode_code": self.PROFILE_MODES["CV"],
            "protocol_approved": True, "termination_ramp_approved": False,
            "automation_settings": asdict(candidate),
            "source": "Previously verified M8437B bench profile; identity checked on every connection",
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }

    def save_calibration_connection(self):
        if self.busy or self._disposed:
            return
        self._profile_selected = True
        self.save_settings()

    def save_manual_settings(self):
        if self.busy or self._disposed:
            return
        self._profile_selected = False
        self.save_settings()

    @property
    def busy(self) -> bool:
        return self._future is not None

    def _serial_settings(self) -> SerialSettings:
        result = SerialSettings(self.port.currentText().strip(), int(self.baud.currentText()),
                                self.address.value(), self.parity.currentText())
        result.validate()
        return result

    @staticmethod
    def _key(settings: SerialSettings):
        return (settings.port, settings.baudrate, settings.address, settings.parity)

    def _dirty(self, *args):
        if self._restoring:
            return
        self._saved = {}
        self.configured_changed.emit({})

    def _serial_changed(self, *args):
        if self._restoring:
            return
        self._identity = None
        self._observations.clear()
        self.readings_match.setChecked(False)
        self.protocol_approved.setChecked(False)
        self.termination_ramp_approved.setChecked(False)
        self.identity_label.setText("Expected load: model 0x008C · firmware 0x002A" if self._profile_selected else "Device: not read")
        self.reading_label.setText("Voltage —     Current —     Input —     Mode code —")
        self._show_modes()
        self._dirty()

    def refresh_ports(self):
        # Enumeration only: pyserial's list_ports does not open a COM port.
        self._available_ports = []
        try:
            from serial.tools import list_ports
            selected = self.port.currentText()
            entries = list(list_ports.comports())
            self._available_ports = [(entry.device, entry.description or "Serial port") for entry in entries]
            devices = [entry[0] for entry in self._available_ports]
            old = self._restoring
            self._restoring = True
            self.port.clear()
            self.port.addItems(devices)
            self.port.setCurrentText(selected)
            self._restoring = old
            self.status.setText(f"Found {len(devices)} port(s). Choose the load's port." if devices else "No COM ports found. Check the USB connection and driver on this laptop.")
        except Exception as exc:
            self.status.setText(f"Could not list serial ports: {exc}")

    def choose_port(self):
        if self.busy or self._disposed:
            return
        if self._port_dialog is not None:
            self._port_dialog.raise_()
            return
        self.refresh_ports()
        if not self._available_ports:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Choose the load-bank COM port")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(560, 330)
        layout = QVBoxLayout(dialog)
        help_text = QLabel("Select the Maynuo load's USB port, then click Use selected port. This only selects a port; it does not connect or control the load.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        dialog.ports = QListWidget()
        for device, description in self._available_ports:
            item = QListWidgetItem(f"{device}  —  {description}")
            item.setData(Qt.ItemDataRole.UserRole, device)
            item.setToolTip(description)
            dialog.ports.addItem(item)
            if device == self.port.currentText():
                dialog.ports.setCurrentItem(item)
        layout.addWidget(dialog.ports, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        dialog.use_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        dialog.use_button.setText("Use selected port")
        dialog.use_button.setEnabled(dialog.ports.currentItem() is not None)
        dialog.ports.itemSelectionChanged.connect(lambda: dialog.use_button.setEnabled(dialog.ports.currentItem() is not None))

        def use_selection():
            item = dialog.ports.currentItem()
            if item is None:
                return
            device = item.data(Qt.ItemDataRole.UserRole)
            self.port.setCurrentText(device)
            self.status.setText(f"{device} selected. Save the calibration connection or return to Operator and start Auto calibration.")
            dialog.accept()

        buttons.accepted.connect(use_selection)
        buttons.rejected.connect(dialog.reject)
        dialog.ports.itemDoubleClicked.connect(lambda _: use_selection())
        layout.addWidget(buttons)
        dialog.finished.connect(lambda _: setattr(self, "_port_dialog", None))
        self._port_dialog = dialog
        dialog.open()

    def _set_busy(self, busy):
        for widget in (self.port, self.baud, self.address, self.parity, self.refresh_button,
                       self.probe_button, self.cc_button, self.cv_button, self.save_button,
                       self.profile_button, self.manual_setup_button):
            widget.setEnabled(not busy)

    def start_probe(self, kind="probe"):
        if self._disposed or self._future is not None:
            return
        try:
            settings = self._serial_settings()
        except (ValueError, TypeError) as exc:
            self.status.setText(str(exc))
            return
        if kind not in ("probe", "CC", "CV"):
            return
        if kind in ("CC", "CV"):
            self._profile_selected = False
            self._show_modes()
        self._set_busy(True)
        self._last_diagnostics = None
        self.copy_diagnostics_button.setEnabled(False)
        self.status.setText("Reading the load… No settings are being changed.")
        self._future = self._executor.submit(self._probe_worker, settings, kind)
        self._timer.start()

    def _probe_worker(self, settings, kind):
        # Rated M8437B maxima are constructor guards only; this worker has no
        # write call. Actual automation uses recipe/fixture limits instead.
        load = None
        result, error = None, None
        try:
            load = self._load_factory(settings, 500.0, 120.0, 3700.0)
            load.open()
            identity = load.probe()
            reading = load.read()
            if kind in ("CC", "CV") and reading.input_on:
                raise ValueError("Mode capture requires load input OFF. Turn it OFF on the front panel, then read the selected mode.")
            result = settings, kind, identity, reading
        except Exception as exc:
            error = str(exc)
        finally:
            if load is not None:
                try:
                    load.close()
                except Exception as exc:
                    error = (error + " " if error else "") + f"Could not close load port: {exc}"
        report = {"schema": 1, "application": PRODUCT + " " + VERSION,
                  "checked_at": datetime.now(timezone.utc).isoformat(), "check": kind,
                  "instrument_writes": False, "error": error,
                  "serial_settings": asdict(settings), "transactions": []}
        if load is not None and callable(getattr(load, "diagnostics", None)):
            report.update(load.diagnostics())
        if result is not None:
            report.update(identity=asdict(identity), reading=asdict(reading))
        # Keep the latest check locally and offer the same evidence in the UI.
        # An unwritable folder must not hide the original communication error.
        destination = self._path.parent / "diagnostics" / "load-probe-latest.json"
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            report["saved_file"] = str(destination)
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
            temporary.replace(destination)
        except OSError as exc:
            report.pop("saved_file", None)
            report["save_error"] = str(exc)
        return result, error, report

    def copy_diagnostics(self):
        if self._last_diagnostics is not None:
            QApplication.clipboard().setText(json.dumps(self._last_diagnostics, indent=2))

    def _finish_probe(self):
        if self._future is None or not self._future.done():
            return
        future, self._future = self._future, None
        self._timer.stop()
        self._set_busy(False)
        if self._disposed:
            return
        try:
            result, error, report = future.result()
            self._last_diagnostics = report
            self.copy_diagnostics_button.setEnabled(True)
            self.copy_diagnostics_button.setToolTip(report.get("saved_file", "Copy the communication check, including the received bytes."))
            if error is not None:
                self.status.setText(f"Read-only check failed: {error}\nClick Copy diagnostics to share the exact reply.")
                return
            settings, kind, identity, reading = result
            if self._key(settings) != self._key(self._serial_settings()):
                raise ValueError("Connection settings changed. Repeat the read-only check.")
            self._accept_probe(settings, kind, identity, reading)
        except Exception as exc:
            self.status.setText(f"Read-only check failed: {exc}")

    def _accept_probe(self, settings: SerialSettings, kind: str, identity: LoadIdentity, reading: LoadReading):
        if self._identity is not None and self._identity != identity:
            self._observations.clear()
            self.readings_match.setChecked(False)
            self.protocol_approved.setChecked(False)
            self.termination_ramp_approved.setChecked(False)
        self._identity = identity
        self.identity_label.setText(identity.label)
        load_status = list(reading.faults)
        if reading.unregulated:
            load_status.append("Load unregulated (not at setpoint)")
        self.reading_label.setText(f"Voltage {reading.voltage:.3f} V     Current {reading.current:.3f} A     Input {'ON' if reading.input_on else 'OFF'}     Mode 0x{reading.mode_code:04X} ({reading.mode_code})"
                                  + ("\nLoad status: " + "; ".join(load_status) if load_status else ""))
        if kind in ("CC", "CV"):
            self._observations[kind] = {"connection": list(self._key(settings)),
                "model_code": identity.model_code, "firmware_code": identity.firmware_code,
                "mode_code": reading.mode_code, "input_on": reading.input_on,
                "voltage": reading.voltage, "current": reading.current,
                "captured_at": datetime.now(timezone.utc).isoformat()}
            self._dirty()
        self._show_modes()
        self.status.setText(f"{kind + ' mode captured. ' if kind != 'probe' else ''}Read-only check complete. Compare the readings with the load front panel.")

    def _show_modes(self):
        if self._profile_selected:
            self.mode_label.setText("Calibration profile: CC 0x3B01     CV 0x3B02 · identity confirmed when connecting")
            return
        parts = []
        for mode in ("CC", "CV"):
            observed = self._observations.get(mode)
            parts.append(f"{mode} mode: 0x{observed['mode_code']:04X}" if observed else f"{mode} mode: not read")
        self.mode_label.setText("     ".join(parts))

    def _collect(self) -> dict:
        if self._profile_selected:
            return self.calibration_profile()
        settings = self._serial_settings()
        if self._identity is None or any(mode not in self._observations for mode in ("CC", "CV")):
            raise ValueError("Read the load's CC and CV modes before saving.")
        for observed in self._observations.values():
            if (observed.get("connection") != list(self._key(settings)) or observed.get("input_on") is not False
                or observed.get("model_code") != self._identity.model_code
                or observed.get("firmware_code") != self._identity.firmware_code):
                raise ValueError("Mode observations must come from this load and connection with input OFF.")
        cc, cv = (self._observations[mode]["mode_code"] for mode in ("CC", "CV"))
        if any(type(code) is not int or not 0 <= code <= 0xFFFF for code in (cc, cv)):
            raise ValueError("Mode observations must be 16-bit instrument codes.")
        if cc == cv:
            raise ValueError("CC and CV returned the same mode code. Select each mode on the front panel and repeat both reads.")
        if not self.readings_match.isChecked() or not self.protocol_approved.isChecked():
            raise ValueError("An engineer must verify the readings and protocol compatibility before saving.")
        automation = self._tuning_settings(approved=self.duty_approved.isChecked())
        return {"schema": 1, "port": settings.port, "baudrate": settings.baudrate,
            "address": settings.address, "parity": settings.parity,
            "model_code": self._identity.model_code, "firmware_code": self._identity.firmware_code,
            "cc_mode_code": cc, "cv_mode_code": cv, "protocol_approved": True,
            "readings_match": True, "termination_ramp_approved": self.termination_ramp_approved.isChecked(),
            "automation_settings": automation, "observations": copy.deepcopy(self._observations),
            "source": self._identity.source, "saved_at": datetime.now(timezone.utc).isoformat()}

    def _tuning_settings(self, *, approved: bool) -> dict:
        automation = {"commissioned": approved}
        automation["auto_direction"] = True
        automation.update({axis + "_direction": field.currentData() for axis, field in self.directions.items()})
        for key, field in self.duty_fields.items():
            try:
                value = float(field.text())
                if not math.isfinite(value) or not 0 <= value <= 1000 or abs(value * 10 - round(value * 10)) > 1e-7:
                    raise ValueError("Duty value is outside the supported range or precision.")
            except ValueError:
                if approved:
                    raise ValueError("Enter the starting duty and both bounds for current and voltage, in 0.1 steps.")
                continue
            automation[key] = value
        if approved:
            candidate = AutomationSettings(**automation)
            error = candidate.validate()
            if error:
                raise ValueError(error)
            automation = asdict(candidate)
        return automation

    def save_settings(self):
        if self._future is not None or self._disposed:
            return
        try:
            result = self._collect()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
            temporary.replace(self._path)
            self._saved = result
            self.configured_changed.emit(copy.deepcopy(result))
            self._show_modes()
            if result.get("calibration_profile") == self.CALIBRATION_PROFILE:
                self.status.setText("Calibration connection saved. Return to Operator, select the variant and start Auto calibration.")
            else:
                self.status.setText("Station setup saved. Automatic calibration tuning is " + ("enabled." if result["automation_settings"]["commissioned"] else "not enabled; connection setup is saved."))
        except Exception as exc:
            self.status.setText(str(exc))

    def _restore(self):
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if data.get("schema") != 1:
                raise ValueError("Unsupported setup format")
            self.port.setCurrentText(data["port"])
            self.baud.setCurrentText(str(data["baudrate"]))
            self.address.setValue(data["address"])
            self.parity.setCurrentText(data["parity"])
            expected_identity = LoadIdentity(data["model_code"], data["firmware_code"])
            profile = data.get("calibration_profile")
            if profile is not None and (profile != self.CALIBRATION_PROFILE or expected_identity != self.PROFILE_IDENTITY):
                raise ValueError("Unsupported calibration profile or load identity")
            self._profile_selected = expected_identity == self.PROFILE_IDENTITY
            # A saved profile is an expected identity, not a fresh observation.
            self._identity = None if profile else expected_identity
            self._observations = data.get("observations", {})
            self.protocol_approved.setChecked(data.get("protocol_approved") is True)
            self.readings_match.setChecked(data.get("readings_match") is True)
            self.termination_ramp_approved.setChecked(data.get("termination_ramp_approved") is True)
            auto = data.get("automation_settings", {})
            for key, field in self.duty_fields.items():
                if key in auto:
                    field.setText(str(auto[key]))
            for axis, field in self.directions.items():
                direction = auto.get(axis + "_direction", 0) if auto.get("commissioned") is True else 0
                index = field.findData(direction)
                field.setCurrentIndex(index if index >= 0 else 0)
            self.duty_approved.setChecked(auto.get("commissioned") is True)
            self._saved = self._collect()
            self.identity_label.setText(expected_identity.label + (" · calibration profile" if self._profile_selected else " · saved setup"))
            self._show_modes()
            self.status.setText("Saved station setup loaded. Device identity and measurements will be checked again when connecting.")
        except Exception as exc:
            self._saved = {}
            self.status.setText(f"Saved setup needs review: {exc}")

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        if self._port_dialog is not None:
            self._port_dialog.reject()
        self._timer.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def closeEvent(self, event):
        self.dispose()
        super().closeEvent(event)
