"""Independent diagnostic-program workspace. Controllers own hardware work."""
from __future__ import annotations

import copy
import math
from pathlib import Path

from PySide6.QtCore import QByteArray, QMimeData, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import theme
from .serial_numbers import validate_serial
from .test_programs import DEFAULT_FILENAME, ensure_programs, list_programs, load_program
from .workspace_identity import WorkspaceIdentity


def _label(text="", large=False):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setObjectName("Caption")
    if large:
        label.setStyleSheet("font-size: 20px; font-weight: 600;")
    return label


def _number(value, suffix=""):
    if type(value) in (int, float) and math.isfinite(value):
        return f"{value:.3f}".rstrip("0").rstrip(".") + suffix
    return "—" if value is None else str(value)


class TestProgramPage(QWidget):
    """Program selection and run display; contains no transport or worker logic.

    ``run_requested`` supplies one frozen file snapshot and charger identity.
    Call ``set_busy(True)`` when the host accepts it, then ``update_snapshot``
    while running. ``refresh_context`` supplies this tab's operator and selected
    variant. A ZIP supplied to ``set_result_bundle`` can be copied as a file.
    """
    run_requested = Signal(object)
    stop_requested = Signal()
    sign_in_requested = Signal()
    context_changed = Signal()
    STAGES = ("Baseline open voltage", "Baseline current", "Charger parameters", "Current calibration",
              "Voltage calibration", "Open-circuit voltage", "Full current", "End-current cutoff")

    def __init__(self, programs_dir, parent=None):
        super().__init__(parent)
        self.setObjectName("Root")
        self.programs_dir = Path(programs_dir)
        self._busy = False
        self._external_busy = False
        self._recipe = None
        self._operator = None
        self._readiness = ""
        self._entries = []
        self._bundle = None
        self._last_events = ""
        self._has_run = False
        self._build()
        try:
            ensure_programs(self.programs_dir)
        except OSError as exc:
            self.set_status("Could not create the test-program folder: " + str(exc))
        self.refresh_programs()

    @property
    def busy(self):
        return self._busy

    def _build(self):
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 10, 0, 0)
        body.setSpacing(8)
        choose = theme.Card()
        choose.body.setContentsMargins(16, 10, 16, 10)
        choose.body.setSpacing(6)
        header = QHBoxLayout()
        header.addWidget(_label("Test programs", True))
        self.identity = WorkspaceIdentity()
        self.variant = self.identity.variant
        self.context = self.identity.operator
        self.identity.sign_in_requested.connect(self.sign_in_requested.emit)
        self.identity.changed.connect(self.context_changed.emit)
        header.addWidget(self.identity, 1)
        choose.body.addLayout(header)
        selection = QHBoxLayout()
        self.programs = QComboBox()
        self.programs.setMinimumContentsLength(24)
        self.programs.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.programs.currentIndexChanged.connect(self._selection_changed)
        selection.addWidget(self.programs, 1)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_programs)
        self.folder_button = QPushButton("Program folder")
        self.folder_button.clicked.connect(self.open_programs_folder)
        selection.addWidget(self.refresh_button)
        selection.addWidget(self.folder_button)
        choose.body.addLayout(selection)
        self.description = _label("Add a JSON program to the folder, then click Refresh.")
        self.description.setMaximumHeight(48)
        self.description.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
        choose.body.addWidget(self.description)
        controls = QHBoxLayout()
        self.serial = QLineEdit()
        self.serial.setPlaceholderText("Scan charger · WO-S00430-1-001")
        self.serial.setMinimumHeight(38)
        self.serial.textChanged.connect(self._serial_changed)
        self.serial.returnPressed.connect(self._request_run)
        self.other_number = QPushButton("Other number")
        self.other_number.setCheckable(True)
        self.other_number.toggled.connect(self._controls)
        self.start_button = QPushButton("START FULL CYCLE")
        self.start_button.setObjectName("Primary")
        self.start_button.setMinimumHeight(40)
        self.start_button.clicked.connect(self._request_run)
        self.stop_button = QPushButton("STOP")
        self.stop_button.setObjectName("Danger")
        self.stop_button.setMinimumHeight(40)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        controls.addWidget(self.serial, 2)
        controls.addWidget(self.other_number)
        controls.addWidget(self.start_button, 1)
        controls.addWidget(self.stop_button)
        choose.body.addLayout(controls)
        self.ready_reason = _label()
        choose.body.addWidget(self.ready_reason)
        body.addWidget(choose)

        live = QHBoxLayout()
        self.current = theme.Tile("Load current", "A")
        self.voltage = theme.Tile("Load voltage", "V")
        self.error_tile = theme.Tile("Target error", "")
        self.elapsed = theme.Tile("Run elapsed", "s")
        for tile in (self.current, self.voltage, self.error_tile, self.elapsed):
            tile.value.setStyleSheet("font-size: 24px;")
            live.addWidget(tile, 1)
        body.addLayout(live)

        progress = theme.Card()
        progress.body.setContentsMargins(16, 10, 16, 10)
        progress.body.setSpacing(5)
        self.stage = _label("Ready for a test program", True)
        self.detail = _label("Start program, then switch charger AC ON while waiting for voltage (15 s).")
        progress.body.addWidget(self.stage)
        progress.body.addWidget(self.detail)
        sections = QHBoxLayout()
        self.results = QTableWidget(0, 5)
        self.results.setHorizontalHeaderLabels(["Stage", "Before", "After", "Time", "Result"])
        self.results.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.results.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.results.setColumnWidth(0, 180)
        self.results.setMinimumHeight(150)
        theme.style_table(self.results, row_h=29)
        sections.addWidget(self.results, 3)
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setPlaceholderText("Live activity and command outcomes appear here.")
        self.activity.setMaximumBlockCount(1000)
        self.activity.setMinimumWidth(180)
        sections.addWidget(self.activity, 2)
        progress.body.addLayout(sections, 1)
        body.addWidget(progress, 1)

        footer = QHBoxLayout()
        self.status = _label("Diagnostic runs are separate from normal production acceptance.")
        footer.addWidget(self.status, 1)
        self.result_folder_button = QPushButton("Result folder")
        self.result_folder_button.clicked.connect(self.open_result_folder)
        self.copy_button = QPushButton("Copy result file")
        self.copy_button.setToolTip("Copy the complete run ZIP, including CAN trace, ready to paste or attach.")
        self.copy_button.clicked.connect(self.copy_result_file)
        footer.addWidget(self.result_folder_button)
        footer.addWidget(self.copy_button)
        body.addLayout(footer)

    def refresh_context(self, recipe=None, operator=None, busy=False, readiness=""):
        self._recipe, self._operator = recipe, copy.deepcopy(operator)
        self._external_busy = bool(busy)
        self._readiness = str(readiness or "")
        self.identity.set_operator(operator)
        self.context.setToolTip(self._readiness)
        if not self._busy and not self._has_run:
            self.detail.setText(self._ready_hint())
        self._controls()

    def set_profiles(self, records, selected=0):
        self.identity.set_profiles(records, selected)

    def refresh_programs(self):
        if self._busy or self._external_busy:
            return
        selected = self.programs.currentText()
        try:
            self._entries = list_programs(self.programs_dir)
        except OSError as exc:
            self._entries = []
            self.set_status("Could not read test programs: " + str(exc))
        self.programs.blockSignals(True)
        self.programs.clear()
        for entry in self._entries:
            self.programs.addItem(entry["name"], entry)
            self.programs.setItemData(self.programs.count() - 1, entry["error"] or entry["program"]["description"],
                                      Qt.ItemDataRole.ToolTipRole)
        found = self.programs.findText(selected)
        if found < 0 or (self._entries[found]["error"] and selected != DEFAULT_FILENAME):
            found = self.programs.findText(DEFAULT_FILENAME)
        self.programs.setCurrentIndex(found if found >= 0 else 0)
        self.programs.blockSignals(False)
        self._selection_changed()

    def _selection_changed(self, *_):
        entry = self.programs.currentData()
        if not entry:
            self.description.setText("Add a JSON program to the Program folder, then click Refresh.")
            self.start_button.setText("START TEST PROGRAM")
        elif entry["error"]:
            self.description.setText("This program cannot run: " + entry["error"])
        else:
            program = entry["program"]
            self.description.setText(program["description"])
            self.start_button.setText({"full_cycle": "START FULL CYCLE", "calibration_only": "START CALIBRATION",
                                       "test_only": "START TESTS"}[program["workflow"]])
        self.description.setToolTip(self.description.text())
        if not self._busy:
            self._has_run = False
            self.results.setRowCount(len(self.STAGES))
            workflow = (entry.get("program") or {}).get("workflow") if entry else None
            for row, name in enumerate(self.STAGES):
                skipped = workflow == "test_only" and row < 5 or workflow == "calibration_only" and row >= 5
                for column, value in enumerate((name, "—", "—", "—", "NOT RUN" if skipped else "PENDING")):
                    self.results.setItem(row, column, QTableWidgetItem(value))
            self.detail.setText(self._ready_hint())
        self._controls()
        self.context_changed.emit()

    def _ready_hint(self):
        entry = self.programs.currentData() or {}
        program = entry.get("program") or {}
        if program.get("workflow") == "test_only":
            return "Each run saves its program, measurements and CAN trace in one shareable file."
        threshold = (program.get("baseline") or {}).get("engage_voltage")
        if threshold is None:
            threshold = (self._recipe.get("cc_test_load_voltage") if isinstance(self._recipe, dict)
                         else getattr(self._recipe, "test_cv_voltage", None))
        hint = "Start program, then switch charger AC ON while waiting for voltage (15 s)."
        if type(threshold) in (int, float) and math.isfinite(threshold):
            hint += f" Load engages above {threshold:g} V."
        return hint

    def _controls(self, *_):
        locked = self._busy or self._external_busy
        entry = self.programs.currentData()
        problem = validate_serial(self.serial.text(), allow_custom=self.other_number.isChecked())
        reason = self._readiness
        if self._busy:
            reason = "Test program running."
        elif locked and not reason:
            reason = "Wait for the active operation to finish."
        reason = reason or ("Sign in with your operator ID." if not self._operator else
                            "Choose a saved variant here." if self._recipe is None else
                            "Choose a test program." if not entry else entry["error"] or problem)
        self.start_button.setEnabled(not locked and not reason)
        self.start_button.setToolTip(reason or "Run the selected program with this tab's variant and operator.")
        self.ready_reason.setText(reason or "Ready to run the selected test program.")
        self.identity.set_locked(locked)
        self.stop_button.setEnabled(self._busy)
        for widget in (self.programs, self.refresh_button, self.serial, self.other_number):
            widget.setEnabled(not locked)
        self.copy_button.setEnabled(not self._busy and self._bundle is not None)
        self.result_folder_button.setEnabled(self._bundle is not None)

    def _serial_changed(self, *_):
        self.other_number.setChecked(False)
        self._controls()

    def _request_run(self):
        self._controls()
        if not self.start_button.isEnabled():
            return
        entry = self.programs.currentData()
        try:
            # Re-read only on explicit Start. Later edits cannot mutate this run.
            frozen = load_program(entry["path"])
        except (OSError, ValueError, RecursionError) as exc:
            self.set_status("Program changed or could not be loaded: " + str(exc))
            self.refresh_programs()
            return
        self.run_requested.emit(dict(program=copy.deepcopy(frozen["program"]),
            program_path=str(frozen["path"]), program_sha256=frozen["sha256"],
            program_source=frozen["source"], serial=self.serial.text().strip(),
            serial_override=self.other_number.isChecked()))

    def set_busy(self, busy):
        was_busy = self._busy
        self._busy = bool(busy)
        if self._busy and not was_busy:
            self._has_run = True
            self.results.setRowCount(0)
            self.activity.clear()
            self._last_events = ""
            self._bundle = None
            self.set_status("Recording this run. The trace includes parameter and calibration writes before the test-only phase.")
        self._controls()

    def update_snapshot(self, snapshot):
        self.stage.setText(str(snapshot.get("stage") or snapshot.get("state") or "Test program"))
        self.detail.setText(str(snapshot.get("detail", "")))
        reading = snapshot.get("reading") or {}
        self.current.set(_number(reading.get("current")))
        self.voltage.set(_number(reading.get("voltage")))
        self.error_tile.set(_number(snapshot.get("error")))
        self.error_tile.unit.setText(str(snapshot.get("unit", "")))
        self.elapsed.set(_number(snapshot.get("elapsed_seconds")))
        stages = snapshot.get("stage_details") or []
        self.results.setRowCount(len(stages))
        active_row = None
        for row, item in enumerate(stages):
            unit = " " + str(item.get("unit", "")) if item.get("unit") else ""
            values = (item.get("name", item.get("stage", "")), _number(item.get("before"), unit),
                      _number(item.get("after", item.get("measured")), unit),
                      _number(item.get("duration_seconds"), " s"), item.get("status", "PENDING"))
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(item.get("detail", "")))
                self.results.setItem(row, column, cell)
            if item.get("status") == "RUNNING":
                active_row = row
        if active_row is not None:
            self.results.scrollToItem(self.results.item(active_row, 0))
        events = []
        for key, title in (("calibration", "Calibration"), ("test", "Tests")):
            part = snapshot.get(key) or {}
            if part.get("events"):
                events.append(title + "\n" + "\n".join(str(event) for event in part["events"][-300:]))
        if not events:
            events = ["\n".join(str(event) for event in snapshot.get("events", [])[-300:])]
        text = "\n\n".join(events)
        if text != self._last_events:
            self._last_events = text
            self.activity.setPlainText(text)
            self.activity.verticalScrollBar().setValue(self.activity.verticalScrollBar().maximum())

    def set_status(self, text):
        self.status.setText(str(text))

    def set_result_bundle(self, path):
        path = Path(path) if path else None
        self._bundle = path.resolve() if path and path.is_file() else None
        self.set_status("Run file ready: " + self._bundle.name if self._bundle else "No completed run file is available.")
        self._controls()

    def open_programs_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.programs_dir.resolve())))

    def open_result_folder(self):
        if self._bundle:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._bundle.parent)))

    def copy_result_file(self):
        if self._busy or self._bundle is None:
            return
        if not self._bundle.is_file():
            self.set_result_bundle(None)
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(self._bundle))])
        mime.setData('application/x-qt-windows-mime;value="Preferred DropEffect"', QByteArray(b"\x01\x00\x00\x00"))
        QApplication.clipboard().setMimeData(mime)
        self.set_status("Copied the run ZIP. Paste it into a folder or attach it to this chat.")
