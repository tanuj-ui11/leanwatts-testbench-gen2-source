"""Compact operator workspace. Live observations never substitute for load feedback."""
from __future__ import annotations

import math
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPlainTextEdit, QProgressBar, QPushButton, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import theme
from .automation import AutomationEngine, DemoBench
from .core import Recipe


LOAD_SETUP = "Choose the Maynuo COM port in Load setup, then start Auto calibration."


def text(value, role="Caption"):
    widget = QLabel(value)
    widget.setObjectName(role)
    widget.setWordWrap(True)
    widget.setMinimumWidth(0)
    return widget


class OperatorPage(QWidget):
    report_saved = Signal()
    connections_requested = Signal()
    manual_requested = Signal()
    recipes_requested = Signal()
    run_active_changed = Signal(bool)

    def __init__(self, data_dir: Path, legacy_page):
        super().__init__()
        self.setObjectName("Root")
        self.data_dir = data_dir
        self.legacy_page = legacy_page
        self.records = []
        self.engine = None
        self.snapshot = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="operator-workflow")
        self._future = None
        self._closing = False
        self._stop_pending = False
        self._last_report = None
        self._generation = 0
        self._future_generation = 0
        self.start_guard = lambda: ""
        self.live_readiness = lambda process: LOAD_SETUP
        self.live_run_factory = None
        self._cancel = threading.Event()
        self._announced_active = False
        self._last_readiness = None
        self._mode = "simulation"
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    @property
    def busy(self):
        return self._future is not None or bool(self.snapshot and self.snapshot.get("is_running"))

    def _build(self):
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 10, 0, 0)
        body.setSpacing(8)
        setup = theme.Card()
        setup.body.setContentsMargins(12, 8, 12, 8)
        row = QHBoxLayout()
        self.variant = QComboBox()
        self.variant.setMinimumWidth(180)
        self.variant.currentIndexChanged.connect(self._variant_changed)
        self.serial = QLineEdit()
        self.serial.setPlaceholderText("Scan or enter charger serial")
        self.operator = QLineEdit()
        self.operator.setPlaceholderText("Operator name")
        self.operator.setMaximumWidth(150)
        self.serial.textChanged.connect(self._identity_changed)
        self.operator.textChanged.connect(self._identity_changed)
        for name, widget, stretch in (("Variant", self.variant, 2), ("Serial", self.serial, 2), ("Operator", self.operator, 1)):
            row.addWidget(text(name))
            row.addWidget(widget, stretch)
        setup.body.addLayout(row)
        self.targets = text("Choose a saved variant.")
        setup.body.addWidget(self.targets)
        body.addWidget(setup)

        actions = QHBoxLayout()
        self.calibrate = QPushButton("Start auto calibration")
        self.calibrate.setObjectName("Primary")
        self.calibrate.clicked.connect(lambda: self.start("calibration"))
        self.test = QPushButton("2   Auto test & report")
        self.test.setObjectName("Primary")
        self.test.clicked.connect(lambda: self.start("test"))
        self.stop_button = QPushButton("■   Stop")
        self.stop_button.setObjectName("Danger")
        self.stop_button.clicked.connect(self.stop)
        for widget in (self.calibrate, self.test, self.stop_button):
            widget.setMinimumHeight(42)
            actions.addWidget(widget, 1)
        body.addLayout(actions)
        self.readiness = text(LOAD_SETUP)
        self.readiness.setWordWrap(False)
        status_row = QHBoxLayout()
        status_row.addWidget(self.readiness, 1)
        self.standalone_ready = QCheckBox("Charger restarted for standalone test")
        self.standalone_ready.setToolTip("After calibration, restart charger power without CAN start commands. Confirm before every real standalone test.")
        self.standalone_ready.setVisible(False)
        self.standalone_ready.toggled.connect(self._lock_controls)
        status_row.addWidget(self.standalone_ready)
        body.addLayout(status_row)

        columns = QHBoxLayout()
        columns.setSpacing(10)
        left = QVBoxLayout()
        columns.addLayout(left, 3)
        live = theme.Card("Live measurements", "")
        live.body.setContentsMargins(14, 10, 14, 10)
        live.body.setSpacing(6)
        live.setMinimumWidth(275)
        live.setMaximumWidth(365)
        columns.addWidget(live, 2)
        body.addLayout(columns, 1)

        progress = theme.Card()
        progress.body.setContentsMargins(14, 10, 14, 10)
        progress.body.setSpacing(5)
        self.stage = text("Ready for setup", "StatusText")
        self.stage.setStyleSheet("font-size: 23px; font-weight: 600;")
        self.detail = text("Select a variant and identify the charger. Current and voltage calibration run automatically in sequence.")
        self.detail.setMinimumHeight(32)
        progress.body.addWidget(self.stage)
        progress.body.addWidget(self.detail)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setMaximumHeight(12)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet("QProgressBar { background: #252034; border: 0; border-radius: 4px; } QProgressBar::chunk { background: #A78BFA; border-radius: 4px; }")
        progress.body.addWidget(self.progress)
        metrics = QHBoxLayout()
        self.target_value = text("Target\n—")
        self.measured_value = text("Measured\n—")
        self.error_value = text("Error\n—")
        for widget in (self.target_value, self.measured_value, self.error_value):
            metrics.addWidget(widget, 1)
        progress.body.addLayout(metrics)
        self.stage_info = text("Load setting and settling progress will appear here.")
        progress.body.addWidget(self.stage_info)
        left.addWidget(progress)

        tabs = QTabWidget()
        self.results = QTableWidget(3, 4)
        self.results.setHorizontalHeaderLabels(["Stage", "Measured", "Target ± tol.", "Result"])
        self.results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.results.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.results.setMinimumHeight(120)
        theme.style_table(self.results, row_h=26)
        for index, name in enumerate(("CC current", "End voltage", "End current")):
            for column, value in enumerate((name, "—", "—", "Not run")):
                self.results.setItem(index, column, QTableWidgetItem(value))
        tabs.addTab(self.results, "Results")
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumBlockCount(1000)
        self.events.setMinimumHeight(80)
        theme.mono(self.events)
        tabs.addTab(self.events, "Activity")
        left.addWidget(tabs, 1)

        self.source = text("Waiting for live CAN data")
        live.body.addWidget(self.source)
        tiles = QGridLayout()
        self.current = theme.Tile("Current", "A", True)
        self.voltage = theme.Tile("Voltage", "V", True)
        for tile in (self.current, self.voltage):
            tile.value.setStyleSheet("font-size: 30px;")
            tile.setMinimumHeight(90)
            tile.set("—")
        tiles.addWidget(self.current, 0, 0)
        tiles.addWidget(self.voltage, 0, 1)
        live.body.addLayout(tiles)
        self.power = text("Power  — W")
        self.error_side = text("Target error  —")
        self.faults = text("Charger faults  —")
        self.reference = text("Load reference  —\nMaynuo control is awaiting setup.")
        for widget in (self.power, self.error_side, self.faults, self.reference):
            live.body.addWidget(widget)
        live.body.addStretch(1)
        self.connection_button = QPushButton("Load connection")
        self.connection_button.clicked.connect(self.connections_requested)
        self.manual_button = QPushButton("Manual calibration")
        self.manual_button.clicked.connect(self.manual_requested)

        footer = QHBoxLayout()
        self.report_status = text("Reports are saved automatically after each run.")
        footer.addWidget(self.report_status, 1)
        footer.addWidget(self.connection_button)
        footer.addWidget(self.manual_button)
        self.open_report = QPushButton("Open report")
        self.open_report.setEnabled(False)
        self.open_report.clicked.connect(self._open_report)
        footer.addWidget(self.open_report)
        body.addLayout(footer)

    def set_profiles(self, records, selected=0):
        if self.busy:
            return
        self.records = records
        self.variant.blockSignals(True)
        self.variant.clear()
        for record in records:
            self.variant.addItem(f"{record['recipe']['name']} · r{record['revision']}")
        self.variant.setCurrentIndex(selected)
        self.variant.blockSignals(False)
        self._variant_changed()

    def _variant_changed(self, *args):
        if not self.records or self.variant.currentIndex() < 0:
            return
        self._show_targets()
        self._identity_changed()
        self.refresh()

    def _show_targets(self):
        if not self.records or self.variant.currentIndex() < 0:
            self.targets.setText("Choose a saved variant.")
            return
        recipe = Recipe.from_dict(self.records[self.variant.currentIndex()]["recipe"])
        targets = f"Current {recipe.cc_current:g} A ± {recipe.current_tolerance:g}   ·   Voltage {recipe.end_voltage:g} V ± {recipe.voltage_tolerance:g}"
        if self._mode == "simulation":
            targets += f"   ·   End current {recipe.end_current:g} A ± {recipe.end_current_tolerance:g}"
        self.targets.setText(targets)

    def _identity_changed(self, *args):
        if not self.busy and hasattr(self, "standalone_ready"):
            self.standalone_ready.setChecked(False)
        if self.busy or not self.snapshot:
            return
        record = self.records[self.variant.currentIndex()] if self.records and self.variant.currentIndex() >= 0 else {}
        if (self.snapshot.get("unit_serial") == self.serial.text().strip()
                and self.snapshot.get("operator") == self.operator.text().strip()
                and self.snapshot.get("recipe") == record.get("recipe")
                and self.snapshot.get("recipe_revision") == record.get("revision")):
            return
        self.snapshot = None
        self.engine = None
        self.stage.setText("Ready for the next run")
        self.detail.setText("The selected unit or variant changed. Previous results have been cleared.")
        self.progress.setValue(0)
        self.target_value.setText("Target\n—")
        self.measured_value.setText("Measured\n—")
        self.error_value.setText("Error\n—")
        self.error_side.setText("Target error  —")
        self.stage_info.setText("The previous report remains available below.")
        self.power.setText("Power  — W")
        self.faults.setText("No fault data")
        self.reference.setText("No independent load readings")
        self.results.setRowCount(0)
        self.events.clear()
        self.refresh()

    def set_mode(self, mode):
        if mode == self._mode:
            return
        self._generation += 1
        if self.busy:
            self._cancel.set()
            self._stop_pending = True
            # The same worker finishes an in-flight tick before stopping. Keep
            # tracking this future even after clearing the old mode's display.
            self._future = self._executor.submit(self._tick_worker, True)
        self._mode = mode
        self.standalone_ready.setChecked(False)
        # Production currently exposes calibration only. Keep the separate test
        # implementation available in simulation until that workflow is enabled.
        self.standalone_ready.setVisible(False)
        self.test.setVisible(mode == "simulation")
        self.snapshot = None
        self.stage.setText("Ready for demonstration" if mode == "simulation" else "Ready for auto calibration")
        self.detail.setText("No hardware will be controlled." if mode == "simulation" else LOAD_SETUP)
        self.progress.setValue(0)
        self.target_value.setText("Target\n—")
        self.measured_value.setText("Measured\n—")
        self.error_value.setText("Error\n—")
        self.stage_info.setText("Load setting and settling progress will appear here.")
        names = ("CC current", "End voltage", "End current") if mode == "simulation" else ("Current calibration", "Voltage calibration")
        self.results.setRowCount(len(names))
        for row, name in enumerate(names):
            for column, value in enumerate((name, "—", "—", "Pending")):
                self.results.setItem(row, column, QTableWidgetItem(value))
        self.events.clear()
        self.voltage.set("—")
        self.current.set("—")
        self.power.setText("Power  — W")
        self.error_side.setText("Target error  —")
        self.faults.setText("No fault data")
        self.reference.setText("No independent load readings")
        self.report_status.setText("Demonstration records are labelled SIMULATION." if mode == "simulation" else "Calibration records save automatically.")
        self.open_report.setText("Open report" if mode == "simulation" else "Open calibration record")
        self._show_targets()
        self.refresh()

    def start(self, process):
        if self.busy or self._closing:
            return
        real = self._mode != "simulation"
        if real and process != "calibration":
            self.stage.setText("Auto calibration")
            self.detail.setText("The separate test process will be added after calibration is established.")
            return
        reason = self.live_readiness(process) if real else ""
        if real and (reason or self.live_run_factory is None):
            self.stage.setText("Bench setup required")
            self.detail.setText(reason or LOAD_SETUP)
            return
        reason = self.start_guard()
        if reason:
            self.stage.setText("Finish the active process first")
            self.detail.setText(reason)
            return
        if not self.serial.text().strip() or not self.operator.text().strip():
            self.stage.setText("Enter the serial and operator")
            self.detail.setText("Enter this charger's serial and the operator name before starting.")
            return
        if not self.records or self.variant.currentIndex() < 0:
            self.stage.setText("Choose a saved variant")
            return
        record = self.records[self.variant.currentIndex()]
        recipe = Recipe.from_dict(record["recipe"])
        recipe_error = recipe.validate_calibration() if process == "calibration" else recipe.validate()
        if recipe_error:
            self.stage.setText("Review the variant recipe")
            self.detail.setText(recipe_error)
            return
        self.engine = None
        # Freeze identity and construction inputs before handing work off. The
        # factory must capture configuration on the GUI thread, not read widgets.
        try:
            factory = self.live_run_factory(recipe, process, self._cancel) if real else None
        except Exception as exc:
            self.stage.setText("Review the bench setup")
            self.detail.setText(str(exc))
            return
        engine_class = AutomationEngine
        identity = (self.serial.text().strip(), self.operator.text().strip(), record["revision"])
        self._stop_pending = False
        self._cancel.clear()
        self._generation += 1
        self._future_generation = self._generation
        self.snapshot = None
        self._last_report = None
        self.open_report.setEnabled(False)
        self.report_status.setText("Calibration record saves automatically when the run ends." if real else "Demonstration only · report will be labelled SIMULATION.")
        self.standalone_ready.setChecked(False)
        self.stage.setText("Connecting devices" if real else "Starting demonstration")
        self.detail.setText("Preparing current calibration, then voltage calibration. Use Stop to cancel." if real else "No hardware will be controlled.")
        self.progress.setValue(0)
        self.target_value.setText("Target\n—")
        self.measured_value.setText("Measured\n—")
        self.error_value.setText("Error\n—")
        self.stage_info.setText("Opening the selected load and CAN connections…" if real else "Preparing simulated readings…")
        self._future = self._executor.submit(self._start_worker, process, recipe, identity, factory, engine_class)
        self._lock_controls()

    def _start_worker(self, process, recipe, identity, factory, engine_class):
        bench, settings = factory() if factory else (DemoBench(recipe), None)
        self.engine = engine_class(recipe, identity[0], identity[1], recipe_revision=identity[2],
                                   report_dir=self.data_dir / "reports", bench=bench, settings=settings)
        if self._cancel.is_set():
            return self.engine.snapshot()
        if process == "calibration":
            self.engine.start_calibration()
        else:
            self.engine.start_test()
        return self.engine.snapshot()

    def _tick_worker(self, stop=False):
        if self.engine is None:
            return {}
        if stop or self._cancel.is_set():
            self.engine.stop()
        else:
            self.engine.tick()
        return self.engine.snapshot()

    def stop(self):
        if self.busy:
            self._cancel.set()
            self._stop_pending = True
            self.detail.setText("Stopping the active process…")

    def _lock_controls(self):
        active = self.busy
        if active != self._announced_active:
            self._announced_active = active
            self.run_active_changed.emit(active)
        for widget in (self.variant, self.serial, self.operator):
            widget.setEnabled(not active)
        available = bool(self.records) and not active
        for widget, process in ((self.calibrate, "calibration"), (self.test, "test")):
            if self._mode != "simulation" and process == "test":
                widget.setEnabled(False)
                widget.setVisible(False)
                continue
            reason = self.live_readiness(process) if self._mode != "simulation" else ""
            if self._mode != "simulation" and self.live_run_factory is None:
                reason = reason or LOAD_SETUP
            widget.setEnabled(available and not reason)
            widget.setToolTip("Run an isolated demonstration; no hardware access." if self._mode == "simulation" else reason or "Automatically calibrate and save current, then voltage, using this variant.")
        self.standalone_ready.setEnabled(not active)
        self.stop_button.setEnabled(active and not self._stop_pending)

    def refresh(self):
        if self._closing:
            return
        if self._future is not None and self._future.done():
            future, self._future = self._future, None
            try:
                result = future.result()
                if self._future_generation == self._generation:
                    self.snapshot = result
                    self._render_run(self.snapshot)
                else:
                    self._record_report(result)
            except Exception as exc:
                self.snapshot = None
                self.stage.setText("Process could not finish")
                self.detail.setText(str(exc))
        if self._future is None and self.snapshot and self.snapshot.get("is_running"):
            self._future = self._executor.submit(self._tick_worker, self._stop_pending)
        if self.snapshot and self.snapshot.get("is_running") and not self.snapshot.get("simulation"):
            stamp = (self.snapshot.get("reading") or {}).get("timestamp")
            if stamp is not None and time.monotonic() - stamp > 2:
                self.source.setText("Load readings stale · waiting for instrument")
                self.current.set("—")
                self.voltage.set("—")
                self.power.setText("Power (V × I)  — W")
                self.error_side.setText("Target error  —")
        if self._mode != "simulation" and not (self.snapshot and not self.snapshot.get("simulation")):
            self._render_can()
        elif not self.snapshot:
            self.source.setText("SIMULATION · no hardware data")
            self.voltage.set("—")
            self.current.set("—")
        calibration_ready = self.live_readiness("calibration") if self._mode != "simulation" else ""
        current_readiness = (self._mode, calibration_ready)
        if self._mode != "simulation" and not self.busy and self.snapshot is None and current_readiness != self._last_readiness:
            if not calibration_ready:
                self.stage.setText("Ready for auto calibration")
                self.detail.setText("Choose the variant, enter serial and operator, then Start auto calibration. Current and voltage run in sequence.")
            else:
                self.stage.setText("Choose the load connection")
                self.detail.setText(calibration_ready)
        self._last_readiness = current_readiness
        self.readiness.setText("SIMULATION ONLY · No hardware access" if self._mode == "simulation"
                               else "Calibration running · instruments reserved" if self.busy else calibration_ready or "Ready · current → voltage → save")
        self.readiness.setToolTip(calibration_ready)
        self.calibrate.setText("1   Demo calibration" if self._mode == "simulation" else "Start auto calibration")
        self.test.setText("2   Demo test & report" if self._mode == "simulation" else "2   Auto test & report")
        self._lock_controls()

    @staticmethod
    def _number(value, unit="", signed=False):
        if not isinstance(value, (float, int)) or not math.isfinite(value):
            return "—"
        return (f"{value:+.3f}" if signed else f"{value:.3f}") + (" " + unit if unit else "")

    def _render_run(self, snapshot):
        self.stage.setText(str(snapshot.get("state", "Ready")))
        self.detail.setText(str(snapshot.get("detail", "")))
        self.progress.setValue(int(snapshot.get("progress", 0)))
        unit = snapshot.get("unit", "")
        self.target_value.setText("Target\n" + self._number(snapshot.get("target"), unit))
        self.measured_value.setText("Measured\n" + self._number(snapshot.get("measured"), unit))
        error = self._number(snapshot.get("error"), unit, True)
        self.error_value.setText("Error\n" + error)
        self.error_side.setText("Target error  " + error)
        reading = snapshot.get("reading") or {}
        self.source.setText(("SIMULATION · " if snapshot.get("simulation") else "")
                            + ("load reference" if snapshot.get("is_running") else "last recorded reading"))
        self.current.set(self._number(reading.get("current")))
        self.voltage.set(self._number(reading.get("voltage")))
        self.power.setText("Power (V × I)  " + self._number(reading.get("power"), "W"))
        observations = snapshot.get("charger_observations") or {}
        stamps = snapshot.get("charger_observed_at") or {}
        can_fault = observations.get("faultWord")
        fresh_fault = isinstance(can_fault, int) and 0 <= time.monotonic() - stamps.get("faultWord", -100) <= 2
        load_status = str(reading.get("fault") or "Load faults clear")
        if reading.get("unregulated"):
            load_status += " · Load unregulated (not at setpoint)"
        load_status += " · CAN " + (f"0x{can_fault:04X}" if fresh_fault else "—")
        self.faults.setText("Simulation · no real fault data" if snapshot.get("simulation") else load_status)
        self.reference.setText("Load " + str(reading.get("load_mode", "OFF")) + " · " + self._number(reading.get("load_setpoint"))
                               + "\n" + ("Simulated readings · not a calibration certificate" if snapshot.get("simulation") else str(reading.get("source", ""))))
        purpose = snapshot.get("stability_purpose")
        stability_label = ("Confirming before save" if purpose == "save" else
                           "Tuning stability" if purpose == "tuning" else "Stable readings")
        step_target = snapshot.get("output_step_requested")
        tuning_mode = snapshot.get("tuning_mode")
        step_text = ""
        if isinstance(step_target, (int, float)) and step_target > 0 and purpose != "save":
            step_text = ("   ·   Calculated coarse adjustment" if tuning_mode == "coarse" else
                         "   ·   Learning output response" if tuning_mode == "learning" else
                         f"   ·   Fine step target {step_target:g} {snapshot.get('unit', '')}")
        self.stage_info.setText(f"{stability_label} {snapshot.get('stable_count', 0)} / {snapshot.get('stable_required', 0)}{step_text}   ·   Elapsed {snapshot.get('elapsed_seconds', 0):.0f} s")
        results = list(snapshot.get("results", []))
        names = ("Current calibration", "Voltage calibration") if snapshot.get("operation") == "calibration" else ("CC current", "End voltage", "End current")
        completed = {item.get("name") for item in results}
        for name in names:
            matched = name in completed or (name == "End current" and any(str(item).startswith("End current") for item in completed))
            if not matched:
                axis = "voltage" if "voltage" in name.lower() else "current"
                active = snapshot.get("is_running") and str(snapshot.get("phase", "")).endswith(axis) and name != "End current"
                results.append({"name": name, "status": "In progress" if active else "Not run" if not snapshot.get("is_running") else "Pending"})
        self.results.setRowCount(len(results))
        for row, result in enumerate(results):
            target = self._number(result.get("target")) + " ± " + self._number(result.get("tolerance"))
            status = result.get("status", "")
            display_status = "Save sent" if status == "SAVE REQUEST ACCEPTED" else "Demo in range" if snapshot.get("simulation") and status == "PASS" else status
            cells = (result.get("name", ""), self._number(result.get("measured"), result.get("unit", "")), target, display_status)
            for col, value in enumerate(cells):
                item = QTableWidgetItem(str(value))
                item.setToolTip(status + ": " + str(result.get("detail", result.get("reason", ""))))
                self.results.setItem(row, col, item)
        events = "\n".join(snapshot.get("events", [])[-500:])
        if self.events.toPlainText() != events:
            self.events.setPlainText(events)
            self.events.verticalScrollBar().setValue(self.events.verticalScrollBar().maximum())
        self._record_report(snapshot)

    def _record_report(self, snapshot):
        paths = snapshot.get("report_paths") or {}
        report = paths.get("html")
        if snapshot.get("report_error"):
            self.report_status.setText("Report could not be saved. Check available disk space and folder access.")
            self.report_status.setToolTip(str(snapshot["report_error"]))
            self.open_report.setEnabled(False)
            return
        if report and not Path(report).is_file():
            self.report_status.setText("Report file unavailable. Check the report folder.")
            self.open_report.setEnabled(False)
            return
        if report and report != self._last_report:
            self._last_report = report
            self.open_report.setEnabled(True)
            self.report_status.setText(("Demo report saved" if snapshot.get("simulation") else "Calibration record saved")
                                      + " · " + str(snapshot.get("unit_serial", "")) + " · " + str(snapshot.get("outcome", "")))
            self.report_status.setToolTip(str(report))
            self.report_saved.emit()

    def _render_can(self):
        page = self.legacy_page
        now = time.monotonic()
        values = page._live_telemetry
        stamps = page._live_telemetry_at

        def fresh(key):
            value = values.get(key)
            return value if (page.can_started and isinstance(value, (int, float)) and math.isfinite(value)
                             and 0 <= now - stamps.get(key, -100) <= 2) else None

        voltage, current = fresh("outputVoltage"), fresh("outputCurrent")
        self.voltage.set(self._number(voltage))
        self.current.set(self._number(current))
        valid = voltage is not None and current is not None
        self.source.setText("LIVE CAN · charger readings" if valid else "CAN readings unavailable / stale")
        self.power.setText("Power  " + (self._number(voltage * current, "W") if valid else "— W"))
        fault = fresh("faultWord")
        self.faults.setText(f"Charger fault word  0x{int(fault):04X}" if fault is not None else "Charger faults  — (no fresh data)")
        self.error_side.setText("Target error  —")
        self.reference.setText("Load reference  —\nMaynuo measurement setup required.")

    def _open_report(self):
        if self._last_report and Path(self._last_report).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self._last_report).resolve())))

    def dispose(self):
        self.timer.stop()
        self._closing = True
        self._cancel.set()
        if self.busy:
            self._executor.submit(self._tick_worker, True)
        # Physical transports have their own bounded timeouts; don't freeze Qt
        # while an in-flight native call completes and the queued stop runs.
        self._executor.shutdown(wait=self._mode == "simulation", cancel_futures=False)
