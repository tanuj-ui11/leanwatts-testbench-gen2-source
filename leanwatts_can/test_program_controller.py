"""Worker coordination for engineering runs, separate from production acceptance."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import threading

from PySide6.QtCore import QObject, QTimer, Signal

from .automation import AutomationSettings, DemoBench
from .core import Recipe
from .production_flow import ProductionFlow, validate_test_recipe
from .run_bundle import RunBundle
from .serial_numbers import validate_serial
from .test_programs import validate_program
from .workspace_identity import workspace_context, resolve_operator, sign_in


class TestProgramController(QObject):
    active_changed = Signal(bool)

    def __init__(self, window, page):
        super().__init__(window)
        self.window, self.page = window, page
        self._operator = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-program")
        self._future = None
        self._busy = False
        self._closing = False
        self._cancel = threading.Event()
        self.flow = None
        self.bundle = None
        self.snapshot = None
        self._result_bundle = None
        page.run_requested.connect(self.start)
        page.stop_requested.connect(self.stop)
        page.sign_in_requested.connect(self.sign_in)
        page.context_changed.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    @property
    def busy(self):
        return self._busy

    def _context(self, program=None):
        recipe, record, operator = workspace_context(self.window, self.page, self._operator)
        if not self.busy:
            reason = self.window._workspace_busy_reason("test")
            if reason:
                raise ValueError(reason)
        if program:
            workflow = program["workflow"]
            calibrate = workflow != "test_only"
            if calibrate and record.get("production_ready") is False:
                raise ValueError("Complete and save the variant before calibration.")
            error = (validate_test_recipe(recipe) if not calibrate else
                     recipe.validate_production() if workflow == "full_cycle" else recipe.validate_parameter_calibration())
            if error:
                raise ValueError(error)
            if self.window.legacy_page.live_mode != "simulation":
                if record.get("simulation_only"):
                    raise ValueError("Select an actual charger variant.")
                process = "test_only" if not calibrate else "calibration_test" if workflow == "full_cycle" else "calibration"
                error = self.window._operator_live_reason(process, record=record)
                if error:
                    raise ValueError(error)
        return recipe, record, copy.deepcopy(operator)

    def sign_in(self):
        if self.busy:
            return
        operator = sign_in(self.window, self.page)
        if operator:
            self._operator = operator
        self.refresh()

    def start(self, payload):
        if self.busy or self._closing:
            return
        if getattr(self.window, "firmware_controller", None) and self.window.firmware_controller.busy:
            self.page.set_status("Wait for firmware programming to finish.")
            return
        self.bundle = None
        self.flow = None
        self._result_bundle = None
        try:
            payload = copy.deepcopy(payload)
            program = validate_program(payload["program"])
            payload["program"] = program
            error = validate_serial(payload["serial"], allow_custom=payload["serial_override"])
            if error:
                raise ValueError(error)
            recipe, record, operator = self._context(program)
            run_calibration = program["workflow"] != "test_only"
            run_tests = program["workflow"] != "calibration_only"
            simulation = self.window.legacy_page.live_mode == "simulation"
            self._cancel.clear()
            self.bundle = RunBundle(self.window.data_dir / "test_runs", payload, recipe,
                                    record["revision"], operator, simulation=simulation)
            method = program["testing"]["start_method"]
            if simulation:
                cal_factory = lambda: (DemoBench(recipe), AutomationSettings.demo())
                test_factory = lambda: (DemoBench(recipe, post_test_can_start=False,
                                                  test_start_method=method), AutomationSettings.demo())
            else:
                test_factory = (self.window._operator_live_factory(recipe,
                    "test" if run_calibration else "test_only", self._cancel,
                    test_start_method=method, trace_sink=self.bundle.append) if run_tests else None)
                cal_factory = (self.window._operator_live_factory(recipe, "calibration", self._cancel,
                    trace_sink=self.bundle.append) if run_calibration else None)
            # Engineering runs do not inherit an unrelated Auto Calibration session's trim seed.
            seed = None
            self.snapshot = None
            self._busy = True
            self.page.set_busy(True)
            self.active_changed.emit(True)
            self.page.set_status("Recording this run in " + str(self.bundle.folder))
            self._future = self._executor.submit(self._start_worker, recipe, payload, operator,
                record["revision"], cal_factory, test_factory, run_calibration, run_tests, seed)
        except Exception as exc:
            self.page.set_status(str(exc))
            if self.bundle is not None:
                try:
                    self.page.set_result_bundle(self.bundle.finish(dict(outcome="FAIL", detail=str(exc))))
                except Exception as export_error:
                    self.page.set_status(str(exc) + "; evidence folder: " + str(self.bundle.folder) + "; " + str(export_error))
            self._busy = False
            self.page.set_busy(False)
            self.active_changed.emit(False)

    def _start_worker(self, recipe, payload, operator, revision, cal_factory, test_factory,
                      run_calibration, run_tests, seed):
        try:
            self.flow = ProductionFlow(recipe, payload["serial"], operator, revision,
                cal_factory, test_factory, cancellation_event=self._cancel,
                run_calibration=run_calibration, run_tests=run_tests,
                serial_override=payload["serial_override"], starting_point=seed,
                test_program=payload["program"])
            self.flow.start()
            return self._collect()
        except Exception as exc:
            return self._worker_error(exc)

    def _tick_worker(self):
        try:
            if self._cancel.is_set():
                self.flow.stop()
            else:
                self.flow.tick()
            return self._collect()
        except Exception as exc:
            return self._worker_error(exc)

    def _worker_error(self, error):
        detail = str(error)
        if self.flow is not None and self.flow.is_running:
            try:
                self.flow.stop()
            except Exception as stop_error:
                detail += "; stop: " + str(stop_error)
        snapshot = self.flow.snapshot() if self.flow else {}
        snapshot.update(is_running=False, state="Process stopped", detail=detail)
        result = copy.deepcopy(snapshot.get("unit_result") or {})
        result.update(outcome="FAIL", detail=detail)
        snapshot["unit_result"] = result
        return self._export(snapshot)

    def _collect(self):
        snapshot = self.flow.snapshot()
        return snapshot if snapshot.get("is_running") else self._export(snapshot)

    def _export(self, snapshot):
        if self._result_bundle is None:
            result = snapshot.get("unit_result") or dict(outcome="FAIL", detail="No completed result was available.")
            result["production_acceptance"] = False
            try:
                self._result_bundle = self.bundle.finish(result)
                snapshot["result_bundle"] = str(self._result_bundle)
            except Exception as exc:
                snapshot["export_error"] = str(exc)
                snapshot["evidence_folder"] = str(self.bundle.folder)
        return snapshot

    def stop(self):
        if self.busy:
            self._cancel.set()
            self.page.set_status("Stopping; accepted commands cannot be undone. Saving the partial trace.")

    def refresh(self):
        if self._closing:
            return
        if self._future is not None and self._future.done():
            future, self._future = self._future, None
            try:
                self.snapshot = future.result()
                self.page.update_snapshot(self.snapshot)
                if not self.snapshot.get("is_running"):
                    self._busy = False
                    self.page.set_busy(False)
                    if self.snapshot.get("result_bundle"):
                        self.page.set_result_bundle(self.snapshot["result_bundle"])
                    if self.snapshot.get("export_error"):
                        self.page.set_status("Run ended. ZIP could not be created; keep folder " +
                            self.snapshot["evidence_folder"] + ": " + self.snapshot["export_error"])
                    self.active_changed.emit(False)
            except Exception as exc:
                self._busy = False
                self.page.set_busy(False)
                self.page.set_status("Run failed: " + str(exc))
                self.active_changed.emit(False)
        if self._future is None and self._busy and self.snapshot and self.snapshot.get("is_running"):
            self._future = self._executor.submit(self._tick_worker)
        recipe = operator = None
        reason = ""
        try:
            entry = self.page.programs.currentData() or {}
            recipe, _, operator = self._context(entry.get("program") if not self.busy else None)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            reason = str(exc)
            try:
                operator = resolve_operator(self.window, self._operator)
            except (ValueError, OSError):
                operator = None
        self.page.refresh_context(recipe, operator, busy=bool(self.window._workspace_busy_reason("test")), readiness=reason)

    def dispose(self):
        self._closing = True
        self.timer.stop()
        self._cancel.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
