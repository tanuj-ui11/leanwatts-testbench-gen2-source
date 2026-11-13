"""Standalone firmware workspace. All programmer I/O belongs to its controller."""
from __future__ import annotations

import copy
from dataclasses import asdict, is_dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from . import theme
from .serial_numbers import validate_serial
from .workspace_identity import WorkspaceIdentity


def _label(text="", *, large=False):
    result = QLabel(text)
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setWordWrap(True)
    result.setMinimumWidth(0)
    result.setObjectName("Caption")
    if large:
        result.setStyleSheet("font-size: 22px; font-weight: 600;")
    return result


def _target_dict(target):
    return asdict(target) if is_dataclass(target) else copy.deepcopy(target) if isinstance(target, dict) else {}


def _target_identity(target):
    return str(target.get("identity") or target.get("serial_number") or target.get("root") or "")


class FirmwareProfileDialog(QDialog):
    """Collect an admin-approved image and separately installed vendor tool."""
    def __init__(self, existing=None, target=None, config=None, parent=None):
        super().__init__(parent)
        self.profile = None
        existing = copy.deepcopy(existing or {})
        target = _target_dict(target)
        self.setWindowTitle("Approve firmware for this variant")
        self.resize(720, 560)
        self.setMinimumSize(620, 510)
        body = QVBoxLayout(self)
        body.setSpacing(9)
        body.addWidget(_label("Approve firmware for this variant", large=True))
        body.addWidget(_label("Choose the complete firmware BIN and GDLink_CLI.exe from the official GigaDevice 2.2.3.41771 installation."))
        form = QFormLayout()
        self.cli_path = QLineEdit(str(existing.get("cli_path", "")))
        self.cli_path.setReadOnly(True)
        self.cli_path.setPlaceholderText("Select GDLink_CLI.exe · keep its supporting files together")
        tool_browse = QPushButton("Choose tool")
        tool_browse.clicked.connect(self._choose_tool)
        tool_row = QHBoxLayout()
        tool_row.addWidget(self.cli_path, 1)
        tool_row.addWidget(tool_browse)
        form.addRow("GigaDevice programmer", tool_row)
        download = QPushButton("Download GigaDevice tool")
        download.setToolTip("Download the official package, extract it, then choose GDLink_CLI.exe above.")
        download.clicked.connect(self._download_tool)
        form.addRow("", download)
        self.firmware_path = QLineEdit(str(existing.get("firmware_path", "")))
        self.firmware_path.setReadOnly(True)
        self.firmware_path.setPlaceholderText("Choose the complete .bin firmware file")
        browse = QPushButton("Choose file")
        browse.clicked.connect(self._choose_file)
        file_row = QHBoxLayout()
        file_row.addWidget(self.firmware_path, 1)
        file_row.addWidget(browse)
        form.addRow("Firmware file", file_row)
        self.declared_mcu = QLineEdit(str(existing.get("declared_mcu", "GD32F103C8T6")))
        self.declared_mcu.setPlaceholderText("GD32F103… · exact approved part marking")
        form.addRow("Expected chip", self.declared_mcu)
        self.flash_size = QLineEdit(str(existing.get("flash_size_kib", 64)))
        self.flash_size.setToolTip("The BIN must cover this complete span. Programming replaces all bytes in this span.")
        form.addRow("Flash size (KiB)", self.flash_size)
        self.flash_start = QLineEdit(f"0x{existing.get('flash_start', 0x08000000):08X}")
        form.addRow("Flash start address", self.flash_start)
        self.speed = QLineEdit(str(existing.get("speed_khz", 1000)))
        form.addRow("SWD speed (kHz)", self.speed)
        self.programmer_serial = str(target.get("serial_number", ""))
        self.target_summary = _label("GD-Link V3  ·  " + self.programmer_serial)
        form.addRow("Selected programmer", self.target_summary)
        body.addLayout(form)
        body.addWidget(_label("Defaults match the supplied production screenshots. Each run checks the detected chip, backs up flash, programs once and compares the full flash readback with this file. Existing firmware and calibration in that flash span will be replaced."))
        self.confirm = QCheckBox("I approve this firmware and these settings for the selected variant.")
        self.confirm.setChecked(False)
        body.addWidget(self.confirm)
        self.error = _label("")
        self.error.setStyleSheet(f"color: {theme.DANGER};")
        body.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("Approve for this variant")
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        body.addWidget(self.buttons)

    def _choose_tool(self):
        selected, _ = QFileDialog.getOpenFileName(self, "Select GigaDevice console", self.cli_path.text(),
                                                 "GigaDevice console (GDLink_CLI.exe)")
        if selected:
            self.cli_path.setText(selected)

    def _download_tool(self):
        from .gdlink_cli import OFFICIAL_DOWNLOAD_URL
        QDesktopServices.openUrl(QUrl(OFFICIAL_DOWNLOAD_URL))

    def _choose_file(self):
        selected, _ = QFileDialog.getOpenFileName(self, "Choose firmware", self.firmware_path.text(),
                                                 "Complete firmware image (*.bin)")
        if selected:
            self.firmware_path.setText(selected)

    def _save(self):
        try:
            cli = Path(self.cli_path.text())
            if cli.name.casefold() != "gdlink_cli.exe" or not cli.is_file():
                raise ValueError("Choose the installed GDLink_CLI.exe from GigaDevice 2.2.3.41771.")
            path = Path(self.firmware_path.text())
            if path.suffix.lower() != ".bin" or not path.is_file():
                raise ValueError("Choose an existing complete .bin firmware file.")
            chip = self.declared_mcu.text().strip()
            if not chip or len(chip) > 120 or any(ord(char) < 32 for char in chip):
                raise ValueError("Enter the exact expected chip from the working production setup.")
            try:
                size = int(self.flash_size.text().strip(), 10)
            except ValueError as exc:
                raise ValueError("Enter the approved writable flash span as a whole number of KiB.") from exc
            if not 1 <= size <= 4096:
                raise ValueError("Confirm the approved flash span in KiB (1–4096).")
            start = int(self.flash_start.text().strip(), 0)
            speed = int(self.speed.text().strip(), 10)
            if not 0 <= start <= 0xFFFFFFFF or not 100 <= speed <= 10000:
                raise ValueError("Check the flash address and SWD speed (100–10000 kHz).")
            if not self.programmer_serial:
                raise ValueError("Select an identified GD-Link programmer first.")
            if not self.confirm.isChecked():
                raise ValueError("Confirm approval of this firmware and these settings.")
            self.profile = dict(firmware_path=str(path.resolve()), declared_mcu=chip,
                flash_start=start, flash_size_kib=size, speed_khz=speed,
                cli_path=str(cli.resolve()), programmer_serial=self.programmer_serial, approved=True)
            self.accept()
        except (ValueError, OSError) as exc:
            self.error.setText(str(exc))


class FirmwareProgramConfirmDialog(QDialog):
    """Per-unit preflight: never carry physical confirmations between chargers."""
    def __init__(self, serial, profile, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Confirm charger programming")
        self.setMinimumWidth(560)
        body = QVBoxLayout(self)
        body.addWidget(_label("Program this charger", large=True))
        body.addWidget(_label(f"Charger: {serial}\nFirmware: {profile.get('firmware_name', Path(profile.get('firmware_path', '')).name)}\nChip: {profile.get('declared_mcu', '')}"))
        body.addWidget(_label("This replaces the approved flash span, including any calibration stored there. Keep the programmer connected until the result is shown."))
        self.ac_off = QCheckBox("Charger AC power is OFF and will remain OFF during programming.")
        self.fixture_power = QCheckBox("MCU is fixture-powered; keep this charger and SWD connected until finished.")
        body.addWidget(self.ac_off)
        body.addWidget(self.fixture_power)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.program_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.program_button.setText("PROGRAM CHARGER")
        self.program_button.setEnabled(False)
        for checkbox in (self.ac_off, self.fixture_power):
            checkbox.toggled.connect(lambda: self.program_button.setEnabled(self.ac_off.isChecked() and self.fixture_power.isChecked()))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        body.addWidget(self.buttons)


class FirmwarePage(QWidget):
    sign_in_requested = Signal()
    context_changed = Signal()
    setup_requested = Signal()
    discover_requested = Signal()
    save_profile_requested = Signal(object)
    program_requested = Signal(object)
    stop_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Root")
        self._recipe = self._operator = None
        self._profile = None
        self._busy = self._external_busy = self._submitted = False
        self._process_running = False
        self._readiness = ""
        self._report = None
        self._last_events = ""
        self._build()
        self._controls()

    @property
    def busy(self):
        return self._busy

    @property
    def selected_target(self):
        return copy.deepcopy(self.targets.currentData())

    def _build(self):
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 10, 0, 0)
        body.setSpacing(10)
        setup = theme.Card()
        setup.body.setContentsMargins(18, 12, 18, 12)
        setup.body.setSpacing(8)
        header = QHBoxLayout()
        header.addWidget(_label("Firmware programming", large=True))
        self.identity = WorkspaceIdentity()
        self.variant = self.identity.variant
        self.context = self.identity.operator
        self.identity.sign_in_requested.connect(self.sign_in_requested.emit)
        self.identity.changed.connect(self.context_changed.emit)
        header.addWidget(self.identity, 1)
        self.setup_button = QPushButton("Firmware setup")
        self.setup_button.setToolTip("Administrator approval is required to change firmware or programmer settings.")
        self.setup_button.clicked.connect(self.setup_requested.emit)
        header.addWidget(self.setup_button)
        setup.body.addLayout(header)
        hardware = QHBoxLayout()
        hardware.addWidget(_label("Programmer"))
        self.targets = QComboBox()
        self.targets.setMinimumContentsLength(25)
        self.targets.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.targets.currentIndexChanged.connect(self._controls)
        hardware.addWidget(self.targets, 1)
        self.find_button = QPushButton("Find programmer")
        self.find_button.setToolTip("Find connected GD-Link USB adapters by serial number. The target MCU is not accessed during discovery.")
        self.find_button.clicked.connect(self.discover_requested.emit)
        hardware.addWidget(self.find_button)
        setup.body.addLayout(hardware)
        self.profile_summary = _label("No approved firmware is configured for this variant.")
        setup.body.addWidget(self.profile_summary)
        controls = QHBoxLayout()
        self.serial = QLineEdit()
        self.serial.setMinimumHeight(40)
        self.serial.setPlaceholderText("Scan charger · WO-S00430-1-001")
        self.serial.textChanged.connect(self._serial_changed)
        self.serial.returnPressed.connect(self._request_program)
        self.other_number = QPushButton("Other number")
        self.other_number.setCheckable(True)
        self.other_number.toggled.connect(self._controls)
        self.start_button = QPushButton("START PROGRAMMING")
        self.start_button.setObjectName("Primary")
        self.start_button.setMinimumHeight(44)
        self.start_button.clicked.connect(self._request_program)
        self.stop_button = QPushButton("STOP")
        self.stop_button.setObjectName("Danger")
        self.stop_button.setMinimumHeight(44)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        controls.addWidget(self.serial, 2)
        controls.addWidget(self.other_number)
        controls.addWidget(self.start_button, 2)
        controls.addWidget(self.stop_button)
        setup.body.addLayout(controls)
        self.ready_reason = _label()
        setup.body.addWidget(self.ready_reason)
        body.addWidget(setup)

        work = theme.Card()
        work.body.setContentsMargins(18, 12, 18, 12)
        work.body.setSpacing(7)
        self.stage = _label("Firmware progress", large=True)
        work.body.addWidget(self.stage)
        self.detail = _label("Find GD-Link, approve firmware once in Firmware setup, then scan each charger and start.")
        work.body.addWidget(self.detail)
        info = QHBoxLayout()
        self.result = theme.Tile("Programming + flash readback", "")
        self.elapsed = theme.Tile("Elapsed", "s")
        self.result.value.setStyleSheet("font-size: 19px;")
        self.elapsed.value.setStyleSheet("font-size: 24px;")
        info.addWidget(self.result, 3)
        info.addWidget(self.elapsed, 1)
        work.body.addLayout(info)
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setPlaceholderText("Programmer discovery, transfer and result details appear here.")
        self.activity.setMaximumBlockCount(1000)
        self.activity.setMinimumHeight(120)
        work.body.addWidget(self.activity, 1)
        body.addWidget(work, 1)
        footer = QHBoxLayout()
        self.status = _label("Standalone programming · calibration is not started by this tab.")
        footer.addWidget(self.status, 1)
        self.report_button = QPushButton("Open report folder")
        self.report_button.clicked.connect(self.open_report_folder)
        footer.addWidget(self.report_button)
        body.addLayout(footer)

    def refresh_context(self, recipe=None, operator=None, busy=False, readiness=""):
        self._recipe, self._operator = recipe, copy.deepcopy(operator)
        self._external_busy = bool(busy)
        self._readiness = str(readiness or "")
        self.identity.set_operator(operator)
        self._controls()

    def set_profiles(self, records, selected=0):
        self.identity.set_profiles(records, selected)

    def set_targets(self, targets):
        if self._busy:
            return
        previous = _target_identity(_target_dict(self.selected_target))
        self.targets.blockSignals(True)
        self.targets.clear()
        selected = 0
        for index, item in enumerate(targets or []):
            target = _target_dict(item)
            title = str(target.get("description") or "GD-Link V3")
            if target.get("serial_number"):
                title += " · " + str(target["serial_number"])
            self.targets.addItem(title, copy.deepcopy(item))
            if _target_identity(target) == previous:
                selected = index
        if not self.targets.count():
            self.targets.addItem("No programmer found", None)
        self.targets.setCurrentIndex(selected)
        self.targets.blockSignals(False)
        self._controls()

    def set_profile(self, profile):
        self._profile = copy.deepcopy(profile) if profile else None
        if not self._profile:
            self.profile_summary.setText("No approved firmware is configured for this variant.")
        else:
            path = Path(str(profile.get("firmware_name") or profile.get("firmware_path") or profile.get("filename", "Firmware")))
            digest = str(profile.get("firmware_sha256") or profile.get("sha256") or "")
            summary = f"Firmware: {path.name}  ·  Expected chip: {profile.get('declared_mcu', 'Not set')}"
            if profile.get("flash_size_kib"):
                summary += f"  ·  {profile['flash_size_kib']} KiB  ·  SWD {profile.get('speed_khz', 1000)} kHz"
            self.profile_summary.setText(summary)
            self.profile_summary.setToolTip(summary + ("\nImage SHA-256: " + digest if digest else ""))
        self._controls()

    def _controls(self, *_):
        locked = self._busy or self._external_busy
        serial_error = validate_serial(self.serial.text(), allow_custom=self.other_number.isChecked())
        configured = bool(self._profile and self._profile.get("approved") is True and self._profile.get("backend") == "gdlink_cli")
        ready = bool(self._recipe is not None and self._operator and self.selected_target and configured)
        reason = self._readiness
        if self._busy:
            reason = "Programming in progress."
        elif locked and not reason:
            reason = "Wait for the active operation to finish."
        reason = reason or ("Sign in with your operator ID." if not self._operator else
                            "Choose a saved variant here." if self._recipe is None else
                            "Click Find programmer and select GD-Link." if not self.selected_target else
                            "Open Firmware setup and approve firmware for this variant." if not configured else serial_error)
        self.start_button.setEnabled(not locked and ready and not reason)
        self.start_button.setToolTip(reason or "Program this charger using the selected variant's approved firmware.")
        self.ready_reason.setText(reason or "Ready. Start programming to confirm this charger's connections.")
        self.identity.set_locked(locked)
        for widget in (self.targets, self.find_button, self.setup_button, self.serial, self.other_number):
            widget.setEnabled(not locked)
        self.stop_button.setEnabled(self._busy and not self._submitted and not self._process_running)
        self.stop_button.setText("WAIT" if self._busy and self._process_running else "FLASHING" if self._busy and self._submitted else "STOP")
        self.stop_button.setToolTip("Programming cannot be interrupted after submission; result monitoring continues."
                                    if self._submitted else "Cancel before the firmware file is submitted.")
        self.report_button.setEnabled(self._report is not None)

    def _serial_changed(self, *_):
        self.other_number.setChecked(False)
        self._controls()

    def _request_program(self):
        self._controls()
        if self.start_button.isEnabled():
            confirmation = self._confirm_program()
            if confirmation:
                self.program_requested.emit(dict(serial=self.serial.text().strip(), serial_override=self.other_number.isChecked(),
                                                 target=self.selected_target, **confirmation))

    def _confirm_program(self):
        dialog = FirmwareProgramConfirmDialog(self.serial.text().strip(), self._profile, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.ac_off.isChecked() and dialog.fixture_power.isChecked():
            return dict(ac_off_confirmed=True, fixture_power_confirmed=True)
        return None

    def set_busy(self, busy):
        previous = self._busy
        self._busy = bool(busy)
        if self._busy and not previous:
            self._submitted = False
            self._process_running = False
            self._last_events = ""
            self._report = None
            self.activity.clear()
            self.result.set("—")
            self.elapsed.set("—")
        elif previous and not self._busy:
            self._process_running = False
        self._controls()

    def update_snapshot(self, snapshot):
        labels = {"VERIFIED": "Programmed · readback verified", "PROGRAMMER_REPORTED_SUCCESS": "Programmer reports success", "FAILED": "Programming failed",
                  "UNCONFIRMED": "Result unconfirmed", "CANCELLED": "Cancelled before submission",
                  "preflight": "Checking programming setup", "backup": "Reading original firmware",
                  "programming": "Programming and checking flash", "verify": "Checking flash readback"}
        stage = str(snapshot.get("stage") or snapshot.get("state") or snapshot.get("phase") or "Firmware programming")
        self.stage.setText(labels.get(stage, stage))
        self.detail.setText(str(snapshot.get("detail", "")))
        self._submitted = self._submitted or any(snapshot.get(key) is True for key in
            ("submitted", "file_submitted", "write_started"))
        self._process_running = snapshot.get("process_running") is True
        if self._busy and self._submitted:
            self.status.setText("Programming cannot be interrupted; waiting for the programmer result.")
        status = str(snapshot.get("status") or snapshot.get("outcome") or "")
        self.result.set(labels.get(status, status.replace("_", " ")) or "—")
        elapsed = snapshot.get("elapsed_seconds")
        self.elapsed.set(f"{elapsed:.1f}" if type(elapsed) in (int, float) else "—")
        events = snapshot.get("events") or snapshot.get("log") or []
        if not events and snapshot.get("detail"):
            line = str(snapshot.get("timestamp", "")) + "  " + str(snapshot["detail"])
            events = self.activity.toPlainText().splitlines()
            if not events or events[-1] != line:
                events.append(line)
        if isinstance(events, str):
            text = events
        else:
            lines = []
            for event in events[-500:]:
                if isinstance(event, dict):
                    lines.append(str(event.get("timestamp", "")) + "  " + str(event.get("message") or event.get("detail") or event))
                else:
                    lines.append(str(event))
            text = "\n".join(lines)
        if text != self._last_events:
            self._last_events = text
            self.activity.setPlainText(text)
            self.activity.verticalScrollBar().setValue(self.activity.verticalScrollBar().maximum())
        if not self._busy and status in labels:
            self.set_status(labels[status] + ". Calibration has not been started.")
        self._controls()

    def set_status(self, text):
        self.status.setText(str(text))

    def set_report(self, path):
        self._report = Path(path).resolve() if path else None
        if self._report:
            self.set_status("Programming report saved. Calibration has not been started.")
        self._controls()

    def open_report_folder(self):
        if self._report:
            folder = self._report if self._report.is_dir() else self._report.parent
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def edit_profile(self, existing=None, target=None, config=None):
        dialog = FirmwareProfileDialog(existing, target or self.selected_target, config, self)
        return copy.deepcopy(dialog.profile) if dialog.exec() == QDialog.DialogCode.Accepted else None


FirmwareProgrammingPage = FirmwarePage
