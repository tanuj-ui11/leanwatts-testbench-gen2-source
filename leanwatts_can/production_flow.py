"""One charger: verify settings, calibrate both axes, optionally perform post-tests."""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import asdict, replace

from .automation import AutomationEngine
from .session_store import now_text
from .session_seed import apply_session_seed, starting_point_text
from .serial_numbers import validate_serial
from .version import VERSION


def validate_test_recipe(recipe):
    """Tests need acceptance points, not a complete parameter-write mapping."""
    recipe = recipe.copy()
    recipe.cc_test_load_voltage = 49.0
    error = recipe.validate()
    if error:
        return error
    if not recipe.termination_test.get("enabled"):
        return "Enable the end-current cutoff test before running the test sequence."
    if 5 <= recipe.end_current + recipe.end_current_tolerance:
        return "The fixed 5 A starting current must exceed the required cutoff plus tolerance."
    if recipe.max_current < 5 or recipe.cc_current <= 5 or recipe.end_voltage * 5 > recipe.max_power:
        return "The fixed 5 A test must fit current/power limits and be below the full-current target."
    return ""


class ProductionFlow:
    """Run start/tick/stop on one worker. Construction never opens instruments."""
    def __init__(self, recipe, serial, operator, revision, calibration_factory,
                 test_factory, cancellation_event=None, *, run_tests=True, starting_point=None,
                 run_calibration=True, serial_override=False, test_program=None):
        if type(run_tests) is not bool or type(run_calibration) is not bool or not (run_tests or run_calibration):
            raise ValueError("Choose calibration, tests, or both.")
        self.recipe = recipe.copy()
        self._serial_input = str(serial)
        self.serial = str(serial).strip()
        self.serial_override = serial_override
        self.test_program = copy.deepcopy(test_program)
        self.test_start_method = ((self.test_program or {}).get("testing") or {}).get("start_method", "external_restart")
        self.operator = copy.deepcopy(operator)
        self.revision = revision
        self.calibration_factory = calibration_factory
        self.test_factory = test_factory
        self._requested_starting_point = copy.deepcopy(starting_point)
        self.starting_point = None
        self.run_tests = run_tests
        self.run_calibration = run_calibration
        self.workflow_mode = ("test_only" if not run_calibration else
                              "parameters_calibration_test" if run_tests else "parameters_calibration")
        self.cancel = cancellation_event if cancellation_event is not None else threading.Event()
        self.engine = None
        self.calibration = None
        self.test = None
        self.is_running = False
        self.stage = "Ready"
        self.detail = "Scan the charger and start the selected workflow."
        self.unit_result = None
        self.started_at = self.finished_at = None
        self._started = 0.0
        self._pending_test = False
        self._simulation = False

    def _operator_label(self):
        return (f"{self.operator.get('name', '')} ({self.operator.get('id', '')})"
                if isinstance(self.operator, dict) else str(self.operator))

    def start(self):
        if self.is_running or self.started_at:
            raise ValueError("This charger attempt has already started.")
        if self.test_program is not None:
            from .test_programs import validate_program
            self.test_program = validate_program(self.test_program)
            expected = {"full_cycle": (True, True), "calibration_only": (True, False),
                        "test_only": (False, True)}[self.test_program["workflow"]]
            if expected != (self.run_calibration, self.run_tests):
                raise ValueError("Selected workflow does not match the frozen test program.")
        error = self.recipe.validate_parameter_calibration() if self.run_calibration else ""
        if not error and self.run_tests:
            error = validate_test_recipe(self.recipe)
        if error:
            raise ValueError(error)
        serial_error = validate_serial(self._serial_input, allow_custom=self.serial_override)
        if serial_error:
            raise ValueError(serial_error)
        self.started_at, self._started = now_text(), time.monotonic()
        self.is_running = True
        self.stage, self.detail = (("Preparing independent tests", "Opening the bench for full-current, end-voltage and cutoff tests.")
                                  if not self.run_calibration else
                                  ("Measuring charger baseline", "Measuring original output before parameter updates or calibration."))
        try:
            if self.cancel.is_set():
                return self.stop()
            if not self.run_calibration:
                self._start_tests()
                self._consume()
                return self.snapshot()
            bench, settings = self.calibration_factory()
            self._simulation = bool(bench.capabilities.simulation)
            settings, self.starting_point = apply_session_seed(
                settings, None if self._simulation else self._requested_starting_point)
            if hasattr(bench, "settings"):
                bench.settings = settings
            self.engine = AutomationEngine(self.recipe, self.serial, self._operator_label(),
                recipe_revision=self.revision, bench=bench, settings=settings, baseline_required=True,
                test_program=self.test_program)
            if self.cancel.is_set():
                return self.stop()
            self.engine.start_calibration()
            self.engine._event(starting_point_text(self.starting_point))
            self._consume()
        except Exception as exc:
            self._failure(exc)
        return self.snapshot()

    @staticmethod
    def _record(engine):
        record = engine.snapshot()
        record["samples"] = copy.deepcopy(engine.samples)
        record["automation_settings"] = asdict(engine.settings)
        record["can_trace"] = copy.deepcopy(getattr(engine.bench, "can_trace", []))
        record["trace_events"] = copy.deepcopy(getattr(engine.bench, "trace_events", []))
        record["trace_capture_complete"] = getattr(engine.bench, "trace_capture_complete", True)
        record["trace_capture_error"] = getattr(engine.bench, "trace_capture_error", "")
        return record

    def _calibration_pass(self):
        if not self.calibration or self.calibration.get("is_running"):
            return False
        statuses = {item.get("name"): item.get("status") for item in self.calibration.get("results", [])}
        preflight = self.calibration.get("preflight_result") or {}
        return ("CALIBRATED" in str(self.calibration.get("outcome", ""))
            and (self.calibration.get("baseline_result") or {}).get("status") == "PASS"
            and statuses.get("Current calibration") == "SAVE REQUEST ACCEPTED"
            and statuses.get("Voltage calibration") == "SAVE REQUEST ACCEPTED"
            and self.calibration.get("stop_outcome") == "Output below approved shutdown thresholds; load OFF confirmed"
            and (preflight.get("status") == "PASS" or self._simulation))

    def _test_pass(self):
        return bool(self.run_tests and self.test and self.test.get("outcome") == "PASS"
            and self.test.get("test_start_method") == "external_restart"
            and self.test.get("test_cleanup_verified") is True
            and (self.test.get("test_parameter_restore_result") or {}).get("status") == "PASS"
            and (self.test.get("test_parameter_restore_result") or {}).get("parameters_changed") is False
            and (self.test.get("test_parameter_restore_result") or {}).get("restoration_required") is False
            and self.test.get("can_tx_count") == 0
            and self.test.get("total_can_tx_count") == 0
            and all(self.test.get(key) == 0 for key in ("startup_can_tx_count", "planned_start_count", "test_setup_can_tx_count", "cleanup_can_tx_count"))
            and len(self.test.get("results", [])) == 3
            and all(item.get("status") == "PASS" for item in self.test["results"]))

    def _consume(self):
        if self.engine is None:
            return
        snap = self.engine.snapshot()
        self.stage, self.detail = snap["state"], snap["detail"]
        if self.engine.is_running:
            return
        if self.engine.operation == "calibration":
            self.calibration = self._record(self.engine)
            if self.cancel.is_set():
                self._finish("STOPPED", "Stopped by operator; post-tests were not started.")
            elif self._calibration_pass():
                if self.run_tests:
                    self._pending_test = True
                    self.stage = "Starting post-calibration tests"
                    self.detail = "Preparing the load for an external AC restart. Testing sends no charger commands or parameter changes."
                else:
                    self._finish("CALIBRATED", "Parameters checked and calibration completed; save requests accepted and output shutdown confirmed. Post-tests were not run. Persistence after restart was not tested.")
            else:
                self._finish("FAIL", self.calibration.get("detail", "Calibration did not complete."))
        else:
            self.test = self._record(self.engine)
            passed = self._test_pass()
            self._finish("STOPPED" if self.cancel.is_set() else "PASS" if passed else
                         "INCOMPLETE" if self.test.get("outcome") == "INCOMPLETE" else "FAIL", self.test.get("detail", ""))

    def _start_tests(self):
        bench, settings = self.test_factory()
        if (bench.capabilities.post_test_can_start or not bench.capabilities.standalone_ready
                or not bench.capabilities.passive_can):
            raise RuntimeError("External-restart tests require a command-free CAN connection with controller ACK enabled.")
        if getattr(bench.capabilities, "test_start_method", "normal") != self.test_start_method:
            raise RuntimeError("Test connection startup method does not match the selected program.")
        self._simulation = self._simulation or bool(bench.capabilities.simulation)
        settings = replace(settings, stage_timeout=min(settings.stage_timeout, 120.0))
        self.engine = AutomationEngine(self.recipe, self.serial, self._operator_label(),
            recipe_revision=self.revision, bench=bench, settings=settings, test_program=self.test_program)
        if self.cancel.is_set():
            self.stop()
            return
        self.engine.start_test()

    def tick(self):
        if not self.is_running:
            return self.snapshot()
        if self.cancel.is_set():
            return self.stop()
        try:
            if self._pending_test:
                self._pending_test = False
                self._start_tests()
            else:
                self.engine.tick()
            self._consume()
        except Exception as exc:
            self._failure(exc)
        return self.snapshot()

    def stop(self):
        self.cancel.set()
        if self.engine is not None and self.engine.is_running:
            self.engine.stop()
            if self.engine.operation == "calibration":
                self.calibration = self._record(self.engine)
            else:
                self.test = self._record(self.engine)
        if self.is_running:
            self._finish("STOPPED", "Stopped by operator. Unfinished stages are not a PASS.")
        return self.snapshot()

    def _failure(self, exc):
        detail = str(exc)
        if self.engine is not None and self.engine.is_running:
            try:
                self.engine.stop("Process failed: " + detail)
            except Exception as stop_error:
                detail += "; stop: " + str(stop_error)
        if self.engine is not None:
            if self.engine.operation == "calibration":
                self.calibration = self._record(self.engine)
            elif self.engine.operation == "test":
                self.test = self._record(self.engine)
        self._finish("FAIL", detail)
        diagnostics = getattr(exc, "diagnostics", None)
        if diagnostics is not None:
            self.unit_result["communication_diagnostics"] = copy.deepcopy(diagnostics)

    def _details(self):
        calibration = self.calibration or (self.engine.snapshot() if self.engine and self.engine.operation == "calibration" else {})
        test = self.test or (self.engine.snapshot() if self.engine and self.engine.operation == "test" else {})
        details = []
        calibration_stages = calibration.get("stage_details", [])
        details.extend(copy.deepcopy(item) for item in calibration_stages
                       if str(item.get("name", "")).startswith("Baseline "))
        preflight = calibration.get("preflight_result") or {}
        if preflight:
            details.append(dict(name="Charger parameters", before="Mismatch" if preflight.get("updated_fields") else "Readback",
                after=preflight.get("action", preflight.get("status")), unit="", duration_seconds=preflight.get("duration_seconds"),
                status=preflight.get("status"), detail=preflight.get("error", preflight.get("note", ""))))
        for record in (calibration, test):
            for stage in record.get("stage_details", []):
                if str(stage.get("name", "")).startswith("Baseline "):
                    continue
                item = copy.deepcopy(stage)
                if item.get("status") == "SAVE REQUEST ACCEPTED":
                    item["status"] = "PASS"
                    item["detail"] = "Measured target reached; calibration save request accepted. Persistence after restart was not tested."
                details.append(item)
        restoration = test.get("test_parameter_restore_result") or {}
        if restoration and restoration.get("status") not in ("NOT REQUIRED", "NOT_REQUIRED"):
            verified = test.get("test_cleanup_verified") is True and restoration.get("status") == "PASS"
            details.append(dict(name="Test cleanup", before=None, after="Load OFF" if verified else "Unconfirmed",
                unit="", status="PASS" if verified else "HOLD", duration_seconds=restoration.get("duration_seconds"),
                detail=((restoration.get("note") if verified else restoration.get("error")) or restoration.get("note") or
                        ("Load OFF confirmed; no charger command or parameter change. Switch AC OFF before disconnecting." if verified else
                         "Test cleanup is unconfirmed. Keep this charger on HOLD."))))
        if not self.run_calibration:
            for name, unit in (("Baseline open voltage", "V"), ("Baseline current", "A"),
                               ("Charger parameters", ""), ("Current calibration", "A"),
                               ("Voltage calibration", "V")):
                details.append(dict(name=name, before=None, after=None, unit=unit,
                    duration_seconds=0, status="NOT RUN", detail="Not selected for the test-only workflow."))
        if not self.run_tests:
            for name, unit in (("Open-circuit voltage", "V"), ("Full current", "A"),
                               ("End current / observed shutdown", "A")):
                details.append(dict(name=name, before=None, after=None, unit=unit,
                    duration_seconds=0, status="NOT RUN", detail="Not selected for the parameters + calibration workflow."))
        return details

    def _finish(self, outcome, detail):
        self.is_running = False
        self._pending_test = False
        self.finished_at = now_text()
        calibration_pass = self._calibration_pass()
        test_pass = self._test_pass()
        restoration = (self.test or {}).get("test_parameter_restore_result") or {}
        if (self.test and not self.test.get("test_cleanup_verified")
                and (restoration.get("required") or restoration.get("status") in ("PENDING", "RESTORING", "HOLD", "FAIL")
                     or self.test.get("startup_can_tx_count", 0))):
            cleanup_detail = restoration.get("error") or restoration.get("note") or self.test.get("detail", "")
            detail = "HOLD — test cleanup unconfirmed. Keep this charger on HOLD before release. " + cleanup_detail
            if outcome not in ("STOPPED", "FAIL"):
                outcome = "FAIL"
        if outcome == "PASS" and not (test_pass and (calibration_pass or not self.run_calibration)):
            outcome = "FAIL"
        if outcome == "CALIBRATED" and (not calibration_pass or self.run_tests or self.test is not None):
            outcome = "FAIL"
        if self._simulation:
            outcome = "SIMULATION"
        diagnostic = False
        self.stage = ("Diagnostic checks complete" if outcome == "DIAGNOSTIC COMPLETE" else
                      ("Tests complete" if not self.run_calibration else "Calibration and test complete") if outcome == "PASS" else
                      "Calibration complete — post-tests not run" if outcome == "CALIBRATED" else
                      "Process stopped" if outcome == "STOPPED" else "Review charger result")
        self.detail = detail
        self.unit_result = dict(serial=self.serial, serial_override=self.serial_override,
            started_at=self.started_at, finished_at=self.finished_at,
            application="Leanwatts Testbench Gen2", application_version=VERSION,
            elapsed_seconds=round(max(0, time.monotonic()-self._started), 3), is_running=False,
            calibration_result="NOT RUN" if not self.run_calibration else "PASS" if calibration_pass and not self._simulation else "SIMULATION" if self._simulation else "FAIL",
            test_result=("CHECKS PASSED" if diagnostic else "PASS") if test_pass and not self._simulation else
                        "NOT RUN" if self.test is None else "SIMULATION" if self._simulation else
                        "INCOMPLETE" if self.test.get("outcome") == "INCOMPLETE" else "FAIL",
            outcome=outcome, detail=detail, simulation=self._simulation,
            workflow_mode=self.workflow_mode, run_tests=self.run_tests, run_calibration=self.run_calibration,
            test_skipped=not self.run_tests,
            starting_point=copy.deepcopy(self.starting_point),
            calibration=copy.deepcopy(self.calibration), test=copy.deepcopy(self.test),
            baseline_result=copy.deepcopy((self.calibration or {}).get("baseline_result")),
            preflight_result=copy.deepcopy((self.calibration or {}).get("preflight_result")),
            stage_details=self._details(), test_method="External AC restart; normal operation; zero application CAN commands" if self.run_tests else "Post-tests not selected",
            test_program=copy.deepcopy(self.test_program), test_start_method=self.test_start_method,
            diagnostic_only=diagnostic, normal_operation_verified=bool(test_pass and not self._simulation),
            startup_voltage_result=copy.deepcopy((self.test or {}).get("startup_voltage_result")),
            persistence_verified=False)

    def snapshot(self):
        active = self.engine.snapshot() if self.engine is not None else {}
        result = {key: copy.deepcopy(value) for key, value in active.items()
                  if key not in ("state", "detail", "is_running", "stage_details")}
        result.update(stage=self.stage, state=self.stage, detail=self.detail, is_running=self.is_running,
            test_program=copy.deepcopy(self.test_program), test_start_method=self.test_start_method,
            serial_override=self.serial_override,
            starting_point=copy.deepcopy(self.starting_point),
            workflow_mode=self.workflow_mode, run_tests=self.run_tests, run_calibration=self.run_calibration,
            test_skipped=not self.run_tests,
            baseline_result=copy.deepcopy((self.calibration or (active if self.engine and self.engine.operation == "calibration" else {})).get("baseline_result")),
            calibration=copy.deepcopy(self.calibration or (active if self.engine and self.engine.operation == "calibration" else None)),
            test=copy.deepcopy(self.test or (active if self.engine and self.engine.operation == "test" else None)),
            stage_details=self._details(), unit_result=copy.deepcopy(self.unit_result),
            elapsed_seconds=round(max(0, time.monotonic()-self._started), 3) if self.started_at else 0.0)
        return result
