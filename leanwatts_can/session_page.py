"""Operator session workspace; all instrument work stays on one background worker."""
from __future__ import annotations

import copy
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import theme
from .account_settings import LoginDialog
from .automation import AutomationSettings, DemoBench
from .core import Recipe
from .production_flow import ProductionFlow, validate_test_recipe
from .session_seed import select_session_seed, starting_point_text
from .session_store import OperatorStore, SessionStore, is_test_stage, test_stage_name
from .serial_numbers import validate_serial


def caption(value="", *, large=False):
    label = QLabel(value)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setObjectName("Caption")
    if large:
        label.setStyleSheet("font-size: 23px; font-weight: 600;")
    return label


def number(value, unit=""):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return "—"
    return f"{value:.3f}".rstrip("0").rstrip(".") + (" " + unit if unit else "")


def local_time(value):
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime("%d %b · %H:%M")
    except (ValueError, TypeError):
        return "—"


class SessionModeDialog(QDialog):
    """Select the production rule once; resuming never changes it."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Start session")
        self.setMinimumWidth(420)
        body = QVBoxLayout(self)
        body.addWidget(caption("Choose the session workflow", large=True))
        self.mode_select = QComboBox()
        for key, label in SessionStore.MODES.items():
            self.mode_select.addItem(label, key)
        self.mode_select.setCurrentIndex(self.mode_select.findData("independent"))
        body.addWidget(self.mode_select)
        self.mode_help = caption("Calibration only / Test only: enable the chosen process.\n"
                               "Both independently: run either process when needed.\n"
                               "Both mandatorily: calibrate, then test the same charger before scanning the next one.\n"
                               "This choice stays fixed until the session is closed.")
        self.mode_help.setMinimumHeight(100)
        body.addWidget(self.mode_help)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Start session")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        body.addWidget(buttons)

    @property
    def mode(self):
        return self.mode_select.currentData()


class SessionPage(QWidget):
    run_active_changed = Signal(bool)
    report_saved = Signal()
    connections_requested = Signal()
    recipes_requested = Signal()
    settings_requested = Signal()
    manual_requested = Signal()

    STAGES = ("Baseline open voltage", "Baseline current", "Charger parameters", "Current calibration", "Voltage calibration",
              "Open-circuit voltage test", "Full-current test", "End-current cutoff test")

    def __init__(self, data_dir, legacy_page, store, parent=None):
        super().__init__(parent)
        self.setObjectName("Root")
        self.data_dir = Path(data_dir)
        self.legacy_page = legacy_page
        self.store = store
        self.accounts = OperatorStore(self.data_dir)
        self.records = []
        self.live_run_factory = None
        self.live_readiness = lambda process: "Choose the load connection in Load setup."
        self.start_guard = lambda: ""
        self._mode = "manual"
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="production-session")
        self._future = None
        self._cancel = threading.Event()
        self._closing = False
        self._announced_active = False
        self._stop_pending = False
        self._run_tests = False
        self._run_calibration = True
        self.flow = self.engine = None
        self.snapshot = None
        self._attempt_id = None
        self._attempt_serial = None  # serial of the last run that used Other number
        self._attempt_recorded = False
        self._session = None
        self._authenticated_operator = None
        self._last_report = None
        self._activity_dialog = None
        self._last_readiness = None
        self._carried_override_serial = None  # serial the kept Other-number choice belongs to
        self._build()
        try:
            self._session = self.store.active()
        except Exception as exc:
            self.detail.setText("Session records could not be opened: " + str(exc))
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    @property
    def busy(self):
        return self._future is not None or bool(self.snapshot and self.snapshot.get("is_running"))

    @property
    def session_open(self):
        return self._session is not None

    def _build(self):
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 10, 0, 0)
        body.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(10)
        session = theme.Card()
        session.body.setContentsMargins(18, 12, 18, 12)
        session.body.setSpacing(6)
        session.body.addWidget(caption("Calibration session", large=True))
        self.session_identity = caption("Log in to begin a production session.")
        session.body.addWidget(self.session_identity)
        self.session_totals = caption("One combined report for all chargers in the session.")
        session.body.addWidget(self.session_totals)
        self.session_mode_label = caption("Choose the workflow when starting the session.")
        session.body.addWidget(self.session_mode_label)
        session_actions = QHBoxLayout()
        self.begin_session = QPushButton("START SESSION")
        self.begin_session.setObjectName("Primary")
        self.begin_session.setMinimumHeight(42)
        self.begin_session.clicked.connect(self._start_session)
        self.close_session = QPushButton("CLOSE SESSION")
        self.close_session.setMinimumHeight(42)
        self.close_session.clicked.connect(self._close_session)
        session_actions.addWidget(self.begin_session)
        session_actions.addWidget(self.close_session)
        session.body.addLayout(session_actions)
        top.addWidget(session, 2)

        unit = theme.Card()
        unit.body.setContentsMargins(18, 12, 18, 12)
        unit.body.setSpacing(6)
        select = QHBoxLayout()
        select.addWidget(caption("Variant"))
        self.variant = QComboBox()
        self.variant.setMinimumWidth(150)
        self.variant.currentIndexChanged.connect(self._variant_changed)
        select.addWidget(self.variant, 1)
        self.serial = QLineEdit()
        self.serial.setPlaceholderText("WO-S00430-1-001")
        self.serial.setToolTip("Standard examples: WO-S00430-1-001 or WO-S00261-BS1-019. Select Other number for a different format.")
        self.serial.setMinimumHeight(38)
        self.serial.textChanged.connect(self._identity_changed)
        self.serial.returnPressed.connect(lambda: self.start(self._default_process()))
        select.addWidget(self.serial, 2)
        self.other_number = QPushButton("Other number")
        self.other_number.setCheckable(True)
        self.other_number.setMinimumHeight(38)
        self.other_number.setToolTip("Allow another charger-number format for this unit. Both mandatorily keeps it until calibration and testing finish.")
        self.other_number.toggled.connect(self._serial_mode_changed)
        select.addWidget(self.other_number)
        unit.body.addLayout(select)
        actions = QHBoxLayout()
        self.calibrate = QPushButton("Parameters +\nCalibration")
        self.calibrate.setObjectName("Primary")
        self.calibrate.setMinimumHeight(50)
        self.calibrate.clicked.connect(lambda: self.start("calibration"))
        self.test_only = QPushButton("Test")
        self.test_only.setMinimumHeight(50)
        self.test_only.clicked.connect(lambda: self.start("test_only"))
        self.stop_button = QPushButton("STOP")
        self.stop_button.setObjectName("Danger")
        self.stop_button.setMinimumHeight(50)
        self.stop_button.clicked.connect(self.stop)
        actions.addWidget(self.calibrate, 3)
        actions.addWidget(self.test_only, 2)
        actions.addWidget(self.stop_button, 1)
        unit.body.addLayout(actions)
        self.targets = caption("Choose a saved variant.")
        unit.body.addWidget(self.targets)
        top.addWidget(unit, 3)
        body.addLayout(top)

        live = QHBoxLayout()
        self.current = theme.Tile("Load current", "A")
        self.voltage = theme.Tile("Load voltage", "V")
        self.error_tile = theme.Tile("Target error", "")
        self.timer_tile = theme.Tile("Unit elapsed", "s")
        for tile in (self.current, self.voltage, self.error_tile, self.timer_tile):
            tile.setMinimumHeight(66)
            tile.value.setStyleSheet("font-size: 25px;")
            live.addWidget(tile, 1)
        body.addLayout(live)

        stages = theme.Card()
        stages.body.setContentsMargins(18, 12, 18, 12)
        stages.body.setSpacing(5)
        stage_line = QHBoxLayout()
        self.stage = caption("Ready for a session", large=True)
        stage_line.addWidget(self.stage, 1)
        self.source = caption("No active load readings")
        stage_line.addWidget(self.source)
        stages.body.addLayout(stage_line)
        self.detail = caption("Start a session, choose the variant and scan the first charger.")
        stages.body.addWidget(self.detail)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(8)
        self.progress.setValue(0)
        self.progress.setStyleSheet(f"QProgressBar {{ background: {theme.BORDER_CARD}; border: 0; border-radius: 3px; }} "
                                   f"QProgressBar::chunk {{ background: {theme.ACCENT}; border-radius: 3px; }}")
        stages.body.addWidget(self.progress)
        self.results = QTableWidget(len(self.STAGES), 5)
        self.results.setHorizontalHeaderLabels(["Stage", "Initial measured", "Final", "Time", "Result"])
        self.results.horizontalHeaderItem(1).setToolTip(
            "Baseline: original output before parameter or trim changes. "
            "Calibration: first stable load reading after entering calibration, before adjustment. "
            "Tests: first stable test reading. Parameters: initial charger readback.")
        self.results.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.results.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            self.results.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        # On a small production screen only this table may scroll.
        self.results.setMinimumHeight(130)
        theme.style_table(self.results, row_h=29)
        stages.body.addWidget(self.results, 1)
        self.stage_info = caption("Choose Parameters + Calibration or Test.")
        self.stage_info.setMinimumHeight(38)
        stages.body.addWidget(self.stage_info)
        body.addWidget(stages, 1)

        foot = QHBoxLayout()
        self.report_status = caption("Session reports save automatically as chargers finish.")
        foot.addWidget(self.report_status, 1)
        self.activity_button = QPushButton("Activity")
        self.activity_button.clicked.connect(self._show_activity)
        foot.addWidget(self.activity_button)
        self.open_report = QPushButton("Session report")
        self.open_report.clicked.connect(self._open_report)
        foot.addWidget(self.open_report)
        self.setup_button = QPushButton("Load setup")
        self.setup_button.clicked.connect(self.connections_requested)
        foot.addWidget(self.setup_button)
        body.addLayout(foot)
        self._render_stages([])

    def set_profiles(self, records, selected=0):
        if self.busy:
            return
        self.records = copy.deepcopy(records)
        self.variant.blockSignals(True)
        self.variant.clear()
        for record in self.records:
            suffix = " · Draft" if record.get("production_ready") is False else ""
            self.variant.addItem(f"{record['recipe']['name']} · r{record['revision']}" + suffix)
        self.variant.setCurrentIndex(min(selected, len(records) - 1))
        self.variant.blockSignals(False)
        self._sync_pending_unit()
        self._variant_changed()

    def _sync_pending_unit(self):
        pending = self.store.pending_unit()
        if not pending:
            return None
        index = next((i for i, record in enumerate(self.records)
                      if record.get("revision") == pending["recipe_revision"]
                      and record.get("recipe") == pending["recipe"]), None)
        self.variant.blockSignals(True)
        if index is None:
            index = len(self.records)
            self.records.append(dict(recipe=copy.deepcopy(pending["recipe"]),
                revision=pending["recipe_revision"], frozen_session=True))
            self.variant.addItem(f"{pending['recipe_name']} · r{pending['recipe_revision']} · Session copy")
        self.variant.setCurrentIndex(index)
        self.variant.blockSignals(False)
        self.serial.blockSignals(True)
        self.serial.setText(pending["serial"])
        self.serial.blockSignals(False)
        self.other_number.blockSignals(True)
        self.other_number.setChecked(bool(pending.get("serial_override")))
        self.other_number.blockSignals(False)
        self.other_number.setText("Other number ✓" if pending.get("serial_override") else "Other number")
        return pending

    def _variant_changed(self, *args):
        index = self.variant.currentIndex()
        if 0 <= index < len(self.records):
            recipe = Recipe.from_dict(self.records[index]["recipe"])
            prefix = "Draft — complete Variant Recipes. " if self.records[index].get("production_ready") is False else ""
            self.targets.setText(prefix + f"{recipe.cc_current:g} A ± {recipe.current_tolerance:g}  ·  "
                                 f"{recipe.end_voltage:g} V ± {recipe.voltage_tolerance:g}  ·  "
                                 f"Cutoff {recipe.end_current:g} A ± {recipe.end_current_tolerance:g}")
        else:
            self.targets.setText("Save a variant in Variant recipes.")
        self._identity_changed()

    def _identity_changed(self, *args):
        if self.busy:
            return
        carried = self._carried_override_serial
        if carried is not None and self.serial.text().strip() != carried:
            # The kept choice belonged to the previous charger; the next one needs a fresh choice.
            self._carried_override_serial = None
            self.other_number.blockSignals(True)
            self.other_number.setChecked(False)
            self.other_number.blockSignals(False)
            self.other_number.setText("Other number")
            self.serial.setPlaceholderText("WO-S00430-1-001")
        if self.snapshot:
            self.snapshot = None
            self.flow = self.engine = None
            self._render_stages([])
            self.progress.setValue(0)
            self.stage.setText("Ready for the next charger")
            self.detail.setText("Scan the serial number, then choose Parameters + Calibration or Test.")
            for tile in (self.current, self.voltage, self.error_tile, self.timer_tile):
                tile.set("—")
            self.source.setText("No active load readings")
        self._lock_controls()

    def _serial_mode_changed(self, checked):
        self._carried_override_serial = None
        self.serial.setPlaceholderText("Enter other charger number" if checked else "WO-S00430-1-001")
        self.other_number.setText("Other number ✓" if checked else "Other number")
        self._identity_changed()

    def _reset_serial_mode(self, *, keep_for_serial=None):
        if self._sync_pending_unit():
            return
        if keep_for_serial is not None and self.other_number.isChecked() \
                and self.serial.text().strip() == keep_for_serial:
            # Same charger, next process (e.g. Test after Parameters + Calibration): keep its
            # Other number choice until the serial changes or the operator toggles it.
            self._carried_override_serial = keep_for_serial
            return
        self._carried_override_serial = None
        # Keep the completed result visible while requiring a fresh choice for
        # the next unit; toggled handlers normally clear the previous result.
        self.other_number.blockSignals(True)
        self.other_number.setChecked(False)
        self.other_number.blockSignals(False)
        self.other_number.setText("Other number")
        self.serial.setPlaceholderText("WO-S00430-1-001")

    def set_mode(self, mode):
        if mode == self._mode:
            return
        if self.busy:
            self.stop()
            return
        self._mode = mode
        self.snapshot = None
        self.flow = self.engine = None
        self._render_stages([])
        self.stage.setText("Simulation session" if mode == "simulation" else "Ready for a session")
        self.detail.setText("Simulation does not operate instruments; its results are labelled SIMULATION."
                            if mode == "simulation" else "Start a session, choose the variant and scan the first charger.")
        self._lock_controls()

    def _start_session(self):
        if self.busy:
            return
        try:
            if not self.accounts.list_operators():
                self.detail.setText("Create the first administrator and operator accounts in Settings.")
                self.settings_requested.emit()
                return
            login = LoginDialog(self.accounts, self, title="Resume session" if self._session else "Start session")
            if login.exec() != QDialog.DialogCode.Accepted:
                return
            operator = login.operator
            if self._session:
                owner = self._session.get("operator", {}).get("id")
                if operator["id"] != owner and operator.get("role") != "admin":
                    raise ValueError("Resume using this session's operator ID, or an administrator account.")
            else:
                choice = SessionModeDialog(self)
                if choice.exec() != QDialog.DialogCode.Accepted:
                    return
                self._session = self.store.start(operator, mode=choice.mode)
                self._reset_serial_mode()
                self.serial.clear()
                self._identity_changed()
            self._authenticated_operator = operator
            same_operator = operator["id"] == self._session.get("operator", {}).get("id")
            self.stage.setText("Session started" if same_operator else "Recovered session")
            self.detail.setText("Choose the variant and scan the first charger. Each completed unit joins this session report."
                                if same_operator else "Close this recovered session, then start a new session under your own operator ID.")
            self.serial.setFocus()
            self.report_saved.emit()
        except Exception as exc:
            self.detail.setText(str(exc))
        self.refresh()

    def _close_session(self):
        if self.busy or not self._session or not self._authenticated_operator:
            return
        try:
            closed = self.store.close()
            self._last_report = (closed.get("report_paths") or {}).get("html")
            self._session = None
            self._authenticated_operator = None
            self._reset_serial_mode()
            self.stage.setText("Session closed")
            incomplete = closed.get("completion_state") == "INCOMPLETE"
            self.detail.setText("Session closed as INCOMPLETE. Every attempt remains in History reports."
                                if incomplete else "The session report is ready in History reports.")
            self.report_status.setText("INCOMPLETE · Session closed and report saved." if incomplete
                                       else "Session closed and report saved.")
            self.report_saved.emit()
        except Exception as exc:
            self.detail.setText("Session could not close: " + str(exc))
        self.refresh()

    def _readiness(self, process="calibration"):
        if process not in ("calibration", "test_only"):
            return "Choose Parameters + Calibration or Test."
        if not self._session or not self._authenticated_operator:
            return "Start or resume an operator session."
        if self._authenticated_operator.get("id") != self._session.get("operator", {}).get("id"):
            return "Close this recovered session, then start a session under your own operator ID."
        reason = self.store.process_readiness(run_calibration=process == "calibration", run_tests=process == "test_only")
        if reason:
            return reason
        index = self.variant.currentIndex()
        if not 0 <= index < len(self.records):
            return "Choose a saved variant."
        record = self.records[index]
        if record.get("production_ready") is False and process != "test_only":
            return "Variant is saved as Draft. Complete and save it in Variant Recipes before calibration."
        try:
            recipe = Recipe.from_dict(record["recipe"])
            error = validate_test_recipe(recipe) if process == "test_only" else recipe.validate_parameter_calibration()
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            error = str(exc)
        if error:
            return "Complete Variant Recipes: " + error
        error = validate_serial(self.serial.text(), allow_custom=self.other_number.isChecked())
        if error:
            return error
        reason = self.store.readiness(self.serial.text(), recipe, record["revision"],
                                      run_calibration=process == "calibration", run_tests=process == "test_only")
        if reason:
            return reason
        if self._mode != "simulation":
            if self.records[index].get("simulation_only"):
                return "Save the actual production variant in Variant recipes."
            if self.live_run_factory is None:
                return "The load and CAN connection are not configured."
            checks = ("test_only",) if process == "test_only" else ("calibration",)
            for check in checks:
                reason = self.live_readiness(check)
                if reason:
                    return reason
        return self.start_guard()

    def _default_process(self):
        return "test_only" if (self._session or {}).get("session_mode") == "test_only" else "calibration"

    def start(self, process="calibration"):
        if self.busy or self._closing:
            return
        reason = self._readiness(process)
        if reason:
            self.detail.setText(reason)
            return
        try:
            record = copy.deepcopy(self.records[self.variant.currentIndex()])
            operator_id = self._authenticated_operator["id"]
            if not any(item["id"] == operator_id and item.get("enabled") for item in self.accounts.list_operators()):
                raise ValueError("This operator account is disabled. Close the session and contact an administrator.")
            recipe = Recipe.from_dict(record["recipe"])
            run_tests = process == "test_only"
            run_calibration = process != "test_only"
            error = validate_test_recipe(recipe) if not run_calibration else recipe.validate_parameter_calibration()
            if error:
                raise ValueError(error)
            serial_override = self.other_number.isChecked()
            error = validate_serial(self.serial.text(), allow_custom=serial_override)
            if error:
                raise ValueError(error)
            serial = self.serial.text().strip()
            self._attempt_serial = serial if serial_override else None
            self._cancel.clear()
            # These factory builders run on Qt's thread to freeze widget state.
            # Only the returned factories may open instruments on the worker.
            if self._mode == "simulation":
                cal_factory = (lambda: (DemoBench(recipe), AutomationSettings.demo())) if run_calibration else None
                test_factory = (lambda: (DemoBench(recipe, test_start_method="external_restart"), AutomationSettings.demo())) if run_tests else None
            else:
                test_factory = self.live_run_factory(recipe, "test_only", self._cancel,
                    test_start_method="external_restart") if run_tests else None
                cal_factory = self.live_run_factory(recipe, "calibration", self._cancel) if run_calibration else None
            starting_point = (select_session_seed(self.store.active(), recipe, record["revision"])
                              if self._mode != "simulation" and run_calibration else None)
            self._attempt_id = self.store.begin_unit(serial, recipe.to_dict(), record["revision"], run_tests=run_tests,
                                                     run_calibration=run_calibration, serial_override=serial_override)
            self._session = self.store.active()
            self._attempt_recorded = False
            self._stop_pending = False
            self._run_tests = run_tests
            self._run_calibration = run_calibration
            self.snapshot = None
            self._render_stages([], tests_not_run=not run_tests, calibration_not_run=not run_calibration)
            self.progress.setValue(0)
            self.stage.setText("Preparing charger")
            self.detail.setText(("Measuring baseline, checking charger parameters, then calibrating current and voltage"
                                + (" before testing." if run_tests else ". Post-calibration tests are not selected."))
                                if run_calibration else "Preparing the load. Follow the AC restart instruction to begin testing.")
            self._future = self._executor.submit(self._start_worker, recipe, serial,
                                                copy.deepcopy(self._session["operator"]), record["revision"],
                                                cal_factory, test_factory, run_tests, starting_point=starting_point,
                                                run_calibration=run_calibration, serial_override=serial_override)
        except Exception as exc:
            self.detail.setText(str(exc))
        self._lock_controls()

    def _start_worker(self, recipe, serial, operator, revision, cal_factory, test_factory, run_tests, *, starting_point=None, run_calibration=True, serial_override=False):
        try:
            self.flow = self.engine = ProductionFlow(recipe, serial, operator, revision,
                cal_factory, test_factory, cancellation_event=self._cancel, run_tests=run_tests,
                starting_point=starting_point, run_calibration=run_calibration, serial_override=serial_override)
            self.flow.start()
            return self._snapshot_worker()
        except Exception as exc:
            try:
                if self.flow is not None:
                    self.flow.stop()
            finally:
                self._record_unexpected_failure(exc)
            raise

    def _tick_worker(self, stop=False):
        try:
            if self.flow is None:
                return {}
            if stop or self._cancel.is_set():
                self.flow.stop()
            else:
                self.flow.tick()
            return self._snapshot_worker()
        except Exception as exc:
            try:
                if self.flow is not None:
                    self.flow.stop()
            finally:
                self._record_unexpected_failure(exc)
            raise

    def _snapshot_worker(self):
        snapshot = self.flow.snapshot()
        if not snapshot.get("is_running") and not self._attempt_recorded:
            result = snapshot.get("unit_result")
            if not isinstance(result, dict):
                raise RuntimeError("The charger process ended without a complete result record.")
            self.store.finish_unit(self._attempt_id, result)
            self._attempt_recorded = True
        return snapshot

    def _record_unexpected_failure(self, exc):
        if self._attempt_id is None or self._attempt_recorded:
            return
        result = {"outcome": "FAIL", "calibration_result": "FAIL" if self._run_calibration else "NOT RUN", "test_result": "NOT RUN",
                  "detail": str(exc), "stage_details": [], "calibration": {}, "test": {}}
        if self.flow is not None:
            try:
                snapshot = self.flow.snapshot()
                if isinstance(snapshot.get("unit_result"), dict):
                    result.update(snapshot["unit_result"])
                result.update({key: snapshot.get(key) for key in ("calibration", "test", "stage_details")})
            except Exception:
                pass
        result["outcome"] = "FAIL"
        result["detail"] = str(exc)
        self.store.finish_unit(self._attempt_id, result)
        self._attempt_recorded = True

    def stop(self):
        if self.busy:
            self._cancel.set()
            self._stop_pending = True
            self.detail.setText("Stopping the process and releasing the instruments…")
            self._lock_controls()

    def _lock_controls(self):
        active = self.busy
        if active != self._announced_active:
            self._announced_active = active
            self.run_active_changed.emit(active)
        logged_in = bool(self._session and self._authenticated_operator)
        pending = self._sync_pending_unit()
        self.begin_session.setText("RESUME SESSION" if self._session and not logged_in else "SESSION ACTIVE" if logged_in else "START SESSION")
        self.begin_session.setEnabled(not active and not logged_in)
        self.close_session.setEnabled(not active and logged_in)
        self.variant.setEnabled(not active and logged_in and not pending)
        self.serial.setEnabled(not active and logged_in and not pending)
        self.other_number.setEnabled(not active and logged_in and not pending)
        for button, process in ((self.calibrate, "calibration"),
                                (self.test_only, "test_only")):
            reason = self._readiness(process) if not active else ""
            button.setEnabled(not active and not reason)
            button.setToolTip(reason)
        self.stop_button.setEnabled(active and not self._stop_pending)
        self.setup_button.setEnabled(not active)
        self.open_report.setEnabled(bool(self._last_report or self._session))

    def refresh(self):
        if self._closing:
            return
        if self._future is not None and self._future.done():
            future, self._future = self._future, None
            try:
                self.snapshot = future.result()
                self._render_run(self.snapshot)
                if self.snapshot and not self.snapshot.get("is_running"):
                    self._reset_serial_mode(keep_for_serial=self._attempt_serial)
                    self._session = self.store.active()
                    self._last_report = ((self._session or {}).get("report_paths") or {}).get("html")
                    self.report_saved.emit()
                    self.serial.selectAll()
                    self.serial.setFocus()
            except Exception as exc:
                self._reset_serial_mode()
                self.snapshot = None
                self._session = self.store.active()
                self.stage.setText("Process stopped")
                self.detail.setText(str(exc))
                self.report_status.setText("The attempt is recorded as unsuccessful in this session."
                                           if self._attempt_recorded else "The attempt could not be saved. Review the error before starting another charger.")
                self.report_saved.emit()
        if self._future is None and self.snapshot and self.snapshot.get("is_running"):
            self._future = self._executor.submit(self._tick_worker, self._stop_pending)
        self._render_session()
        reason = self._readiness(self._default_process()) if not self.busy else ""
        readiness_key = (self._mode, reason)
        if not self.busy and self.snapshot is None and readiness_key != self._last_readiness:
            if self._session and self._authenticated_operator:
                self.detail.setText(reason or "Ready. Choose Parameters + Calibration or Test for this charger.")
        self._last_readiness = readiness_key
        if self.snapshot and self.snapshot.get("is_running"):
            reading = self.snapshot.get("reading") or {}
            stamp = reading.get("timestamp")
            if isinstance(stamp, (int, float)) and self._mode != "simulation" and time.monotonic() - stamp > 2:
                self.source.setText("Waiting for a fresh load reading")
                self.current.set("—")
                self.voltage.set("—")
                self.error_tile.set("—")
        self._lock_controls()

    def _render_session(self):
        if not self._session:
            self.session_identity.setText("Log in to begin a production session.")
            self.session_totals.setText("One combined report for all chargers in the session.")
            self.session_mode_label.setText("Choose the workflow when starting the session.")
            return
        operator = self._session.get("operator", {})
        units = self._session.get("unit_summaries", self._session.get("units", []))
        passed = sum(unit.get("outcome") == "PASS" for unit in units)
        calibrated = sum(unit.get("calibration_completed", unit.get("outcome") == "CALIBRATED") for unit in units)
        failed = sum(unit.get("outcome") in ("FAIL", "ERROR", "INTERRUPTED", "ABORTED", "STOPPED") for unit in units)
        incomplete = sum(unit.get("outcome") == "INCOMPLETE" for unit in units)
        self.session_identity.setText(f"{operator.get('name') or operator.get('id', 'Operator')} · ID {operator.get('id', '')}\n"
                                      f"Started {local_time(self._session.get('started_at'))}")
        self.session_totals.setText(f"{len(units)} chargers  ·  {calibrated} calibrated  ·  {passed} pass  ·  {failed} unsuccessful"
                                   + (f"  ·  {incomplete} incomplete" if incomplete else ""))
        pending = self.store.pending_unit()
        mode_key = self._session.get("session_mode", "independent")
        mode = self.store.MODES.get(mode_key, "Both independently")
        hint = {"calibration_only": "Test is disabled", "test_only": "Calibration is disabled",
                "independent": "Run either process when needed", "both_required": "Calibrate, then Test each charger"}
        self.session_mode_label.setText(mode + (f" · {pending['next_step']} required for {pending['serial']}"
            if pending else " · " + hint[mode_key]))

    def _render_run(self, snapshot):
        if not snapshot:
            return
        self.stage.setText(str(snapshot.get("stage") or snapshot.get("state") or "Calibration & test"))
        self.detail.setText(str(snapshot.get("detail", "")))
        active_rows = {"baseline_open_voltage": 0, "baseline_current": 1,
                       "cal_current": 3, "cal_voltage": 4,
                       "test_ocv": 5, "test_wait_restart": 5, "test_current": 6, "test_termination": 7}
        active_row = active_rows.get(snapshot.get("phase")) if snapshot.get("is_running") else None
        reading = snapshot.get("reading") or {}
        self.current.set(number(reading.get("current")))
        self.voltage.set(number(reading.get("voltage")))
        self.error_tile.set(number(snapshot.get("error")))
        self.error_tile.unit.setText(str(snapshot.get("unit", "")))
        self.timer_tile.set(number(snapshot.get("elapsed_seconds")))
        self.source.setText(("SIMULATION · " if self._mode == "simulation" else "") +
                            ("Live load measurements" if snapshot.get("is_running") else "Last recorded load readings"))
        details = snapshot.get("stage_details") or []
        run_tests = snapshot.get("run_tests", self._run_tests)
        run_calibration = snapshot.get("run_calibration", self._run_calibration)
        if not run_calibration and (self._session or {}).get("session_mode") == "both_required":
            # The test is a separate attempt, but the operator sees the whole paired unit.
            current = next((item for item in self.store.summarize(self.store.active() or {}, evidence=True)
                            if self._attempt_id in item.get("attempt_ids", [])), None)
            if current and current.get("calibration_result") == "PASS":
                details = ([item for item in current.get("stage_details", []) if not is_test_stage(item.get("name", ""))]
                    + [item for item in details if is_test_stage(item.get("name", ""))])
                run_calibration = True
        self._render_stages(details, tests_not_run=not run_tests,
                            calibration_not_run=not run_calibration)
        complete = sum(str(item.get("status", "")).upper() not in ("", "PENDING", "RUNNING", "NOT RUN") for item in details)
        # Count only the stages selected for this unit; post-tests are optional.
        stage_count = (5 if run_calibration else 0) + (3 if run_tests else 0)
        self.progress.setValue(min(100, round(complete / stage_count * 100)))
        stable = snapshot.get("stable_required")
        if stable:
            purpose = "Confirming before save" if snapshot.get("stability_purpose") == "save" else "Stable readings"
            self.stage_info.setText(f"{purpose} {snapshot.get('stable_count', 0)} / {stable}  ·  "
                                    + ("Load unregulated; waiting" if reading.get("unregulated") else "Measurements from the Maynuo load"))
        else:
            self.stage_info.setText(("Baseline → parameters → current → voltage"
                                    + (" → post-calibration tests" if run_tests else " · Post-calibration tests: NOT RUN"))
                                    if run_calibration else "Test only · Parameters and calibration: NOT RUN")
        start_text = starting_point_text(snapshot.get("starting_point"))
        if start_text:
            self.stage_info.setText(self.stage_info.text() + "\n" + start_text)
        result = snapshot.get("unit_result") or {}
        if result and not snapshot.get("is_running"):
            pending = self.store.pending_unit()
            self.report_status.setText(f"{result.get('outcome', 'Finished')} · Attempt saved. " +
                (f"{pending['next_step']} required for this charger; retry or close the session as incomplete."
                 if pending else "Ready for the next charger."))
        else:
            self.report_status.setText("This charger will be added to the open session report when the process ends.")
        if self._activity_dialog and self._activity_dialog.isVisible():
            self._activity_dialog.log.setPlainText(self._activity_text())
        if active_row is not None and active_row != getattr(self, "_last_active_row", None):
            self.results.scrollToItem(self.results.item(active_row, 0))
        self._last_active_row = active_row

    def _render_stages(self, details, *, tests_not_run=False, calibration_not_run=False):
        def find(name):
            for item in details:
                candidate = str(item.get("name", item.get("stage", ""))).lower()
                if candidate == name.lower():
                    return item
                if is_test_stage(name) and test_stage_name(candidate) == name:
                    return item
                if name == "Full-current test" and candidate in ("cc current", "full current", "full current test"):
                    return item
                if name == "End-voltage test" and candidate in ("normal output / end voltage", "end voltage", "end voltage test"):
                    return item
                if name == "End-current cutoff test" and ("end current" in candidate or "cutoff" in candidate):
                    return item
                if name == "Charger parameters" and "parameter" in candidate:
                    return item
            return {}
        for row, name in enumerate(self.STAGES):
            item = find(name)
            unit = item.get("unit", "A" if "current" in name.lower() else "V" if "voltage" in name.lower() else "")
            before = item.get("before", item.get("initial_value", item.get("initial")))
            after = item.get("after", item.get("final_value", item.get("measured", item.get("final"))))
            duration = item.get("duration_seconds", item.get("elapsed_seconds"))
            skipped = (tests_not_run and row >= 5) or (calibration_not_run and row < 5)
            status = str(item.get("status", "NOT RUN" if skipped else "Pending"))
            if name == "Charger parameters":
                before_text = str(item.get("before_summary", before if isinstance(before, str) else "—"))
                after_text = str(item.get("after_summary", after if isinstance(after, str) else "—"))
            else:
                before_text, after_text = number(before, unit), number(after, unit)
            values = (name, before_text, after_text, number(duration, "s"), status)
            pending = not item or status.upper() in ("PENDING", "NOT RUN", "NOT TESTED")
            color = theme.MUTED if pending else theme.DANGER if status.upper() in ("FAIL", "ERROR", "INTERRUPTED", "ABORTED", "STOPPED") else theme.TEXT
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setForeground(QColor(color))
                cell.setToolTip(str(item.get("detail", item.get("note", ""))))
                self.results.setItem(row, col, cell)

    def _activity_text(self):
        snapshot = self.snapshot or {}
        lines = []
        for key, title in (("calibration", "Calibration"), ("test", "Post-calibration tests")):
            events = (snapshot.get(key) or {}).get("events", [])
            if events:
                lines.append(title + "\n" + "\n".join(events[-300:]))
        return "\n\n".join(lines) or str(snapshot.get("detail", "No charger activity yet."))

    def _show_activity(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Charger activity")
        dialog.resize(780, 430)
        body = QVBoxLayout(dialog)
        dialog.log = QPlainTextEdit()
        dialog.log.setReadOnly(True)
        dialog.log.setPlainText(self._activity_text())
        body.addWidget(dialog.log)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        body.addWidget(close)
        self._activity_dialog = dialog
        dialog.exec()
        self._activity_dialog = None

    def _open_report(self):
        try:
            if self._session:
                paths = self.store.report_paths(self._session["session_id"])
                path = paths.get("html")
            else:
                path = self._last_report
            if not path or not Path(path).is_file():
                raise ValueError("The session report is not available yet.")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))
        except Exception as exc:
            self.report_status.setText(str(exc))

    def dispose(self):
        self.timer.stop()
        self._closing = True
        self._cancel.set()
        if self.busy:
            self._executor.submit(self._tick_worker, True)
        self._executor.shutdown(wait=False, cancel_futures=False)
