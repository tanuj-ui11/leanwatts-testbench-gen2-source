"""Operator automation with a bounded, synchronous instrument contract.

The real BenchSession and isolated DemoBench use the same workflow. Run each
engine on one worker thread; this module performs no hardware I/O on import.
"""
from __future__ import annotations

import copy
import csv
import html
import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .core import Recipe
from .protocol import BATTERY_NOT_CONNECTED, describe_charger_fault


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _finite(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class AutomationReading:
    voltage: float
    current: float
    power: float
    timestamp: float             # monotonic acquisition time, not GUI receipt time
    sequence: int                # strictly increasing per physical acquisition
    source: str                 # independent instrument identity
    load_mode: str
    load_setpoint: float
    load_on: bool
    fault: str = ""
    unregulated: bool = False
    charger_fault_word: int | None = None  # separate from independent-load/adapter faults
    charger_fault_observed_at: float | None = None


@dataclass(frozen=True)
class BenchCapabilities:
    ready: bool = False
    reason: str = "M8437B USB command map has not been verified."
    simulation: bool = False
    verified_load_protocol: bool = False
    independent_measurements: bool = False
    exclusive_control: bool = False
    calibration_commands: bool = False
    passive_can: bool = False
    standalone_ready: bool = False
    reference_identity: str = ""
    termination_ramp: bool = False
    post_test_can_start: bool = False
    test_start_method: str = "external_restart"


@dataclass(frozen=True)
class AutomationSettings:
    """Commissioning values, never inferred from the manual UI's codec range."""
    commissioned: bool = False
    current_direction: int = 0
    voltage_direction: int = 0
    current_initial: float = 80.0
    voltage_initial: float = 80.0
    current_min: float = 0.0
    current_max: float = 0.0
    voltage_min: float = 0.0
    voltage_max: float = 0.0
    duty_step: float = 0.1
    stable_samples: int = 8
    stable_seconds: float = 2.0
    settling_seconds: float = 0.6
    sample_max_age: float = 1.5
    stage_timeout: float = 120.0
    shutdown_timeout: float = 15.0
    shutdown_voltage: float = 5.0
    shutdown_current: float = 0.1
    maximum_adjustments: int = 100
    maximum_no_response: int = 3
    maximum_reversals: int = 8
    auto_direction: bool = False
    current_output_step: float = 0.1
    voltage_output_step: float = 0.05
    tuning_samples: int = 3
    tuning_seconds: float = 0.6
    max_duty_step: float = 1.0
    max_current_coarse_step: float = 3.0

    @classmethod
    def demo(cls):
        return cls(commissioned=True, current_direction=1, voltage_direction=1,
                   current_min=78.0, current_max=82.0,
                   voltage_min=78.0, voltage_max=82.0,
                   stable_samples=4, stable_seconds=0.6, settling_seconds=0.25)

    def validate(self, calibration=True) -> str:
        if type(self.auto_direction) is not bool:
            return "Automatic trim-direction selection must be true or false."
        if calibration and not self.commissioned:
            return "Set the calibration duty bounds and starting values first."
        directions = (self.current_direction, self.voltage_direction)
        permitted = (-1, 0, 1) if self.auto_direction else (-1, 1)
        if calibration and any(type(value) is not int or value not in permitted for value in directions):
            return "Choose automatic trim-direction detection or specify both directions as +1 or -1."
        for axis in (("current", "voltage") if calibration else ()):
            low, high, initial = (getattr(self, axis + suffix) for suffix in ("_min", "_max", "_initial"))
            if not all(_finite(v) for v in (low, high, initial)) or not 0 <= low <= initial <= high <= 1000 or low == high:
                return f"Approve finite {axis} duty bounds containing its starting value."
            if any(abs(value * 10 - round(value * 10)) > 1e-7 for value in (low, high, initial)):
                return "Duty settings must use 0.1 increments."
        if self.duty_step != 0.1:
            return "Automatic duty commands must use the 0.1 duty resolution."
        if calibration:
            for name in ("current_output_step", "voltage_output_step", "tuning_seconds", "max_duty_step", "max_current_coarse_step"):
                if not _finite(getattr(self, name)) or getattr(self, name) <= 0:
                    return f"{name} must be finite and greater than zero."
            if (not self.duty_step <= self.max_duty_step <= 1.0
                    or abs(self.max_duty_step * 10 - round(self.max_duty_step * 10)) > 1e-7):
                return "Maximum automatic duty step must be 0.1 to 1.0 in 0.1 increments."
            if (not self.duty_step <= self.max_current_coarse_step <= 3.0
                    or abs(self.max_current_coarse_step * 10 - round(self.max_current_coarse_step * 10)) > 1e-7):
                return "Maximum coarse current step must be 0.1 to 3.0 in 0.1 increments."
        for name in ("stable_seconds", "settling_seconds", "sample_max_age", "stage_timeout", "shutdown_timeout",
                     "shutdown_voltage", "shutdown_current"):
            value = getattr(self, name)
            if not _finite(value) or value <= 0:
                return f"{name} must be finite and greater than zero."
        for name in ("stable_samples", "maximum_adjustments", "maximum_no_response", "maximum_reversals"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                return f"{name} must be a positive integer."
        if self.stable_samples < 3:
            return "Use at least three independent stable samples."
        if calibration:
            if type(self.tuning_samples) is not int or not 3 <= self.tuning_samples <= self.stable_samples:
                return "Use at least three tuning samples and no more than the final confirmation count."
            if self.tuning_seconds > self.stable_seconds:
                return "Tuning stability time must not exceed the final confirmation time."
        return ""


class Bench(Protocol):
    """Adapter methods return True only after the stated operation is confirmed.

    Charger command True means complete API acceptance, not flash persistence.
    load methods require readback confirmation, not just a write return value.
    acquire owns all CAN/load I/O; enter_passive drains work and inhibits all
    application CAN frames, including polls/manual controls. Controller ACK is
    retained so a lone charger can deliver broadcasts. release must never restore
    CAN transmission automatically. Methods must have bounded transport timeouts.
    shutdown ordering is charger shutdown, measured discharge, load input off.
    During standalone tests only load commands are permitted, including abort.
    """
    capabilities: BenchCapabilities
    can_tx_count: int
    def acquire(self, operation: str) -> bool: ...
    def release(self) -> None: ...
    def enter_passive(self) -> bool: ...
    def load_off(self) -> bool: ...
    def configure_load(self, mode: str, setpoint: float) -> bool: ...
    def load_on(self) -> bool: ...
    def set_load_current(self, current: float) -> bool: ...
    def read(self, now: float) -> AutomationReading: ...
    def charger_shutdown(self) -> bool: ...
    def charger_start_baseline(self) -> bool: ...
    def charger_start_calibration(self, voltage_duty: float, current_duty: float) -> bool: ...
    def charger_apply_duties(self, voltage_duty: float, current_duty: float) -> bool: ...
    def charger_save(self, axis: str) -> bool: ...


class UnavailableBench:
    """Honest default: contains no I/O and cannot start an automated run."""
    can_tx_count = 0

    def __init__(self, reason="M8437B USB command map has not been verified."):
        self.capabilities = BenchCapabilities(reason=reason)


class DemoBench:
    """Deterministic illustration. Never accesses USB, CAN or a real load."""
    def __init__(self, recipe: Recipe, *, post_test_can_start=False, test_start_method="external_restart"):
        self.recipe = recipe.copy()
        self.capabilities = BenchCapabilities(True, "", True, True, True, True, True, True, True,
                                               "SIMULATED independent load", termination_ramp=True,
                                               post_test_can_start=post_test_can_start,
                                               test_start_method=test_start_method)
        self.can_tx_count = 0
        self.startup_can_tx_count = 0
        self.test_setup_can_tx_count = 0
        self.cleanup_can_tx_count = 0
        self.test_parameter_restore_result = {"status": "NOT REQUIRED"}
        self.commands: list[tuple] = []
        self._owned = False
        self._passive = False
        self._charger_on = False
        self._standalone = False
        self._load_on = False
        self._mode = "OFF"
        self._setpoint = 0.0
        self._sequence = 0
        self._termination_ramping = False
        self._terminated = False
        self._baseline_normal = False
        self._external_reads = 0
        self.current_duty = self.voltage_duty = 80.0

    def acquire(self, operation):
        if self._owned:
            return False
        self._owned = True
        self._standalone = operation == "test"
        self.commands.append(("acquire", operation))
        return True

    def release(self):
        self._owned = False
        self.commands.append(("release",))

    def enter_passive(self):
        self._passive = True
        self.commands.append(("passive",))
        return True

    def begin_post_test(self, stage="Post-calibration test"):
        if not self._owned or not self._standalone or not self.capabilities.post_test_can_start:
            raise RuntimeError("Demo post-test startup was not selected.")
        if not self.test_setup_can_tx_count:
            self.can_tx_count += 2
            self.test_setup_can_tx_count += 2
            self.commands.append(("test_start_voltage", 0))
            self.test_parameter_restore_result = {"status": "PENDING", "required_voltage": 12}
        self.can_tx_count += 1
        self.startup_can_tx_count += 1
        self.commands.append(("post_test_start", stage, self.capabilities.test_start_method))
        self._passive = True
        self._charger_on = True
        return True

    def finish_test(self):
        if self.capabilities.test_start_method == "external_restart":
            self.load_off()
            self.test_parameter_restore_result = dict(status="PASS", parameters_changed=False,
                restoration_required=False, restored=None, load_off_confirmed=True,
                note="Load OFF confirmed. Switch charger AC OFF before disconnecting; no charger command was sent.")
            return True
        if self.test_parameter_restore_result.get("status") == "PENDING":
            self._charger_on = False
            self.can_tx_count += 3
            self.cleanup_can_tx_count += 3
            self.commands.extend((("test_shutdown",), ("test_start_voltage", 12)))
            self.test_parameter_restore_result = {"status": "PASS", "required_voltage": 12, "readback_voltage": 12}
        self._load_on = False
        return True

    def load_off(self):
        self._load_on = False
        self.commands.append(("load_off",))
        return True

    def configure_load(self, mode, setpoint):
        if self._load_on or mode not in ("CV", "CC"):
            raise RuntimeError("Load must be off before changing mode.")
        self._mode, self._setpoint = mode, setpoint
        self._termination_ramping = False
        self._terminated = False
        self.commands.append(("load_setup", mode, setpoint))
        return True

    def load_on(self):
        self._load_on = True
        self.commands.append(("load_on",))
        return True

    def set_load_current(self, current):
        if not self._load_on or self._mode != "CC":
            raise RuntimeError("Continuous CC input is required for termination ramp.")
        self._setpoint = current
        self._termination_ramping = True
        self.commands.append(("load_cc_step", current))
        return True

    def transition_load_to_cc(self, current):
        if not self._load_on or self._mode != "CV":
            raise RuntimeError("CV input must remain ON for the transition.")
        self._mode, self._setpoint = "CC", current
        self.commands.append(("load_transition_cc", current))
        return True

    def _tx(self, name, count, *args):
        if self._passive:
            raise RuntimeError("CAN transmission prohibited during standalone test.")
        self.can_tx_count += count
        self.commands.append((name, *args))
        return True

    def charger_shutdown(self):
        self._charger_on = False
        self._baseline_normal = False
        return self._tx("shutdown", 1)

    def charger_start_baseline(self):
        if not self._owned or self._standalone or self._load_on:
            raise RuntimeError("Baseline startup requires ownership and load OFF.")
        self._charger_on = self._baseline_normal = True
        return self._tx("baseline_start", 1)

    def charger_start_calibration(self, voltage_duty, current_duty):
        self._baseline_normal = False
        self.voltage_duty, self.current_duty = voltage_duty, current_duty
        self._charger_on = True
        return self._tx("calibration_start_and_three_duty_pairs", 7, voltage_duty, current_duty)

    def charger_apply_duties(self, voltage_duty, current_duty):
        self.voltage_duty, self.current_duty = voltage_duty, current_duty
        return self._tx("three_duty_pairs", 6, voltage_duty, current_duty)

    def charger_save(self, axis):
        return self._tx("save_" + axis, 1)

    def read(self, now):
        self._sequence += 1
        voltage = current = 0.0
        if self._standalone and self.capabilities.test_start_method == "external_restart" and not self._load_on:
            self._external_reads += 1
            if self._external_reads > 1:
                self._charger_on = True  # Simulated operator AC restart; no command.
                voltage = self.recipe.end_voltage + self.recipe.voltage_tolerance * .15
        if self._standalone and self._termination_ramping and self._setpoint <= self.recipe.end_current:
            self._terminated = True
        if self._charger_on and self._baseline_normal:
            # Normal-operation model is independent of the UI starting duties.
            voltage = self._setpoint if self._load_on and self._mode == "CV" else self.recipe.end_voltage - .2
            current = self.recipe.cc_current - .2 if self._load_on else 0.0
        elif self._load_on and (self._charger_on or self._standalone) and not self._terminated:
            if self._mode == "CV":
                voltage = self._setpoint
                current = self.recipe.cc_current + (self.recipe.current_tolerance * .15 if self._standalone else (self.current_duty - 80.6) * .5)
            else:
                current = self._setpoint
                voltage = self.recipe.end_voltage + (self.recipe.voltage_tolerance * .15 if self._standalone else (self.voltage_duty - 80.8) * .5)
        return AutomationReading(voltage, current, voltage * current, now, self._sequence,
                                 self.capabilities.reference_identity, self._mode, self._setpoint, self._load_on)


class AutomationEngine:
    """Independent calibration or standalone test run; call on one worker.

    tick accepts monotonic time for deterministic tests. It must not be called
    concurrently with start/stop. No recipe/configuration writes occur here.
    """
    def __init__(self, recipe: Recipe, unit_serial: str, operator: str, recipe_revision=1,
                 report_dir: Path | None = None, bench: Bench | None = None,
                 settings: AutomationSettings | None = None, *, baseline_required=False, test_program=None):
        if type(baseline_required) is not bool:
            raise ValueError("Baseline selection must be true or false.")
        self.baseline_required = baseline_required
        self.baseline_result = {"status": "NOT RUN", "open_voltage": {}, "current": {}}
        self._baseline_deadline = None
        self.recipe = recipe.copy()
        if test_program is not None and not isinstance(test_program, dict):
            raise ValueError("Test program must be a named mapping.")
        self.test_program = copy.deepcopy(test_program or {})
        self.unit_serial = str(unit_serial).strip()
        self.operator = str(operator).strip()
        self.recipe_revision = recipe_revision
        self.report_dir = Path(report_dir) if report_dir is not None else None
        self.bench = bench if bench is not None else UnavailableBench()
        self._calibration_test = False  # Retired method is rejected before ownership.
        self._baseline_engage_voltage = self._program_number("baseline", "engage_voltage", self.recipe.test_cv_voltage, .1, self.recipe.max_voltage)
        self._baseline_ramp_timeout = self._program_number("baseline", "ramp_timeout_seconds", 60.0, 15.0, 180.0)
        self._baseline_settle_seconds = self._program_number("baseline", "settle_seconds", 3.0, 1.0, 10.0)
        self._minimum_ramp_seconds = self._program_number("baseline", "minimum_ramp_seconds", 18.0, 18.0, 60.0)
        if self._minimum_ramp_seconds + self._baseline_settle_seconds >= self._baseline_ramp_timeout:
            raise ValueError("Current ramp timeout must exceed the minimum ramp observation plus settling time.")
        self._control_hold_seconds = 0.0  # Fixed test procedure has no above-cutoff hold.
        self._test_termination = None
        self._ocv_plateau = None
        self._cutoff_observation_seconds = 10.0
        self._test_cleanup_done = False
        self._test_cleanup_ok = False
        self._test_cleanup_error = ""
        self._external_restart_timeout = self._program_number("testing", "restart_timeout_seconds", 120.0, 30.0, 300.0)
        self.startup_voltage_result = {}
        self.external_restart_evidence = {}
        self._external_restart_deadline = None
        self._external_voltage_deadline = None
        self._external_current_peak = None
        self.simulation = self.bench.capabilities.simulation
        self.settings = settings or (AutomationSettings.demo() if self.simulation else AutomationSettings())
        self.is_running = False
        self.state = "Ready" if self.bench.capabilities.ready else "Setup required"
        self.detail = "Choose Auto calibration or Auto test & report."
        self.phase = "idle"
        self.target = self.tolerance = self.measured = self.error = None
        self.unit = ""
        self.reading: AutomationReading | None = None
        self.results: list[dict] = []
        self.events: list[str] = []
        self.samples: list[dict] = []
        self.report_paths: dict[str, str] = {}
        self.report_error = ""
        self.outcome = "NOT RUN"
        self.operation = ""
        self.run_id = self.started_at = self.finished_at = ""
        self.current_duty = self.settings.current_initial
        self.voltage_duty = self.settings.voltage_initial
        self.trim_directions = {axis: getattr(self.settings, axis + "_direction")
                                for axis in ("current", "voltage")}
        self.direction_detection: dict[str, dict] = {}
        self._direction_probe = None
        self.output_per_duty = {"current": None, "voltage": None}
        self._gain_observations = {"current": 0, "voltage": 0}
        self._refinement_count = 0
        self._refinement_deadline = None
        self._test_response_deadline = None
        self.output_step_estimated = None
        self.tuning_mode = "learning"
        self.stable_required = self.settings.stable_samples
        self.stability_purpose = "idle"
        self.stable_count = 0
        self.progress = 0
        self._now = self._started = self._phase_started = self._not_before = 0.0
        self._last_sequence = -1
        self._window: list[tuple[float, float]] = []
        self._adjustments = self._no_response = self._reversals = 0
        self._pending_response = None
        self._last_error_sign = 0
        self._owned = False
        self._tx_start = 0
        self._saves: set[str] = set()
        self.stop_outcome = "Not requested"
        self.termination_evidence: dict = {}
        self._termination_last_on = None
        self._termination_first_off = None
        self._termination_step_started = 0.0
        self._termination_setpoint = 0.0
        self._termination_control_started = None
        self._termination_control_complete = False
        self._termination_stable_step_started = None
        self._test_context = ""
        self._test_extras = []
        self._active_test_name = ""
        self._active_parameter = ""
        self._expected_load_mode, self._expected_load_setpoint = "OFF", 0.0
        self._soak_started = None
        self._soak_samples = []
        self._trace_start = 0
        self.stage_details = []
        self._active_stage = None
        self.startup_can_tx_count = 0
        self.preflight_result = None
        self._measurement_started = False
        self._run_tx_start = 0
        self.planned_start_count = 0
        self._off_battery_diagnostic = False
        self._charger_clear_after = self._charger_clear_deadline = None
        self._charger_startup_after = None
        self._charger_next_poll = 0.0

    def _program_number(self, section, name, default, low, high):
        values = self.test_program.get(section, {})
        if not isinstance(values, dict):
            raise ValueError(f"Test-program {section} must be a named mapping.")
        value = values.get(name, default)
        if name == "engage_voltage" and value is None:
            value = default
        if not _finite(value) or not low <= value <= high:
            raise ValueError(f"Test-program {section}.{name} must be between {low:g} and {high:g}.")
        return float(value)

    @property
    def test_method(self):
        if self.bench.capabilities.test_start_method == "external_restart":
            return "External AC restart; normal charger operation; zero application CAN commands (controller ACK enabled)"
        if self._calibration_test:
            return "Calibration-mode output checks — normal charging and cutoff unverified"
        return "CAN-started post-calibration test" if self.bench.capabilities.post_test_can_start else "Standalone test"

    @property
    def readiness_reason(self):
        return self.bench.capabilities.reason if not self.bench.capabilities.ready else ""

    def _event(self, text):
        self.events.append(_utc() + "  " + text)

    def _require(self, result, message):
        if result is not True:
            raise RuntimeError(message)

    def _validate_start(self, operation):
        if self.is_running:
            raise ValueError("Finish or stop the current process first.")
        error = self.recipe.validate_calibration() if operation == "calibration" else self.recipe.validate()
        if not error and operation == "test":
            if not self.recipe.termination_test.get("enabled"):
                error = "Enable the end-current cutoff test before running the test sequence."
            elif 5 <= self.recipe.end_current + self.recipe.end_current_tolerance:
                error = "The fixed 5 A cutoff starting current must exceed the required cutoff plus its tolerance."
            elif self.recipe.max_current < 5 or self.recipe.cc_current <= 5 or self.recipe.end_voltage * 5 > self.recipe.max_power:
                error = "The fixed 5 A CC test must fit the variant's current and power limits and be below its full-current target."
        if error:
            raise ValueError(error)
        if not self.unit_serial or not self.operator:
            raise ValueError("Enter charger serial and operator name.")
        if type(self.recipe_revision) is not int or self.recipe_revision < 1:
            raise ValueError("Select a saved recipe revision.")
        cap = self.bench.capabilities
        if not cap.ready:
            raise ValueError(cap.reason or "Load-bank setup is incomplete.")
        if operation == "test" and (cap.test_start_method != "external_restart" or cap.post_test_can_start):
            raise ValueError("Tests require external_restart with no charger commands. Earlier CAN-start test methods are retired.")
        requested_method = self.test_program.get("testing", {}).get("start_method")
        if operation == "test" and requested_method is not None and requested_method != cap.test_start_method:
            raise ValueError("The connected bench does not match the selected test program startup method.")
        if operation == "test" and (not callable(getattr(self.bench, "finish_test", None))
                or not callable(getattr(self.bench, "transition_load_to_cc", None))):
            raise ValueError("This bench must support confirmed input-ON CV-to-CC transition and command-free test cleanup.")
        if not cap.verified_load_protocol or not cap.independent_measurements or not cap.reference_identity:
            raise ValueError("A verified load driver and independent measurement reference are required.")
        if not cap.exclusive_control:
            raise ValueError("Exclusive control of the CAN adapter and load is required.")
        if type(self.bench.can_tx_count) is not int or self.bench.can_tx_count < 0:
            raise ValueError("The fixture must provide a valid cumulative CAN transmit counter.")
        error = self.settings.validate(calibration=operation == "calibration")
        if error:
            raise ValueError(error)
        if operation == "calibration":
            if not cap.calibration_commands:
                raise ValueError("Real calibration commands have not been enabled for this fixture.")
            if self.baseline_required:
                if not callable(getattr(self.bench, "charger_start_baseline", None)):
                    raise ValueError("This bench does not support a baseline normal-operation check.")
                if (not _finite(self._baseline_engage_voltage)
                        or not 0 < self._baseline_engage_voltage < self.recipe.end_voltage
                        or self._baseline_engage_voltage > self.recipe.max_voltage):
                    raise ValueError("Baseline current-check load voltage must be positive, below output voltage and within the bench limit.")
        elif not cap.passive_can or not (cap.standalone_ready or cap.post_test_can_start):
            raise ValueError("Approve standalone startup and disable all CAN transmission before testing.")
        if operation == "test" and self.recipe.termination_test.get("enabled") and not cap.termination_ramp:
            raise ValueError("This load adapter has not verified continuous-input CC stepping for end-current testing.")

    def _start(self, operation, now):
        if operation == "test":
            self.recipe.cc_test_load_voltage = 49.0  # Effective test copy only.
        self._validate_start(operation)
        self._now = time.monotonic() if now is None else now
        if not _finite(self._now):
            raise ValueError("Invalid monotonic time.")
        tx_before_acquire = self.bench.can_tx_count
        self._owned = False
        self.operation = operation
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:8]
        self.started_at, self.finished_at = _utc(), ""
        self._started = self._now
        self._tx_start = tx_before_acquire
        self._trace_start = len(getattr(self.bench,"can_trace",[]))
        self.results, self.events, self.samples = [], [], []
        self.stage_details, self._active_stage = [], None
        self.startup_can_tx_count = 0
        self._test_cleanup_done = self._test_cleanup_ok = False
        self._test_cleanup_error = ""
        self.startup_voltage_result = {}
        self.external_restart_evidence = {}
        self._external_restart_deadline = self._external_voltage_deadline = None
        self._external_current_peak = None
        self._ocv_plateau = None
        self._test_termination = None
        self.preflight_result = None
        self._measurement_started = False
        self.planned_start_count = 0
        self._off_battery_diagnostic = False
        self._charger_clear_after = self._charger_clear_deadline = None
        self._charger_startup_after = None
        self._charger_next_poll = 0.0
        self._run_tx_start = tx_before_acquire
        self.baseline_result = {"status": "NOT RUN", "open_voltage": {}, "current": {}}
        self._baseline_deadline = None
        self.report_paths, self.report_error = {}, ""
        self._saves = set()
        self.termination_evidence = {}
        self._termination_last_on = self._termination_first_off = None
        self._test_context = "core_voltage"
        self._test_extras = []
        if operation == "test" and self.bench.capabilities.test_start_method != "external_restart":
            plan = self.recipe.test_plan
            self._test_extras = [("voltage", point, f"Voltage regulation / CC {point:g} A") for point in plan.get("voltage_regulation_currents", [])]
            self._test_extras += [("current", point, f"Current regulation / CV {point:g} V") for point in plan.get("current_regulation_voltages", [])]
            if plan.get("soak_seconds", 0):
                self._test_extras.append(("soak", plan["soak_current"], "Sustained-load stability"))
        self._last_sequence = -1
        self.current_duty, self.voltage_duty = self.settings.current_initial, self.settings.voltage_initial
        self.trim_directions = {axis: getattr(self.settings, axis + "_direction")
                                for axis in ("current", "voltage")}
        self.direction_detection = {}
        self._direction_probe = None
        self.output_per_duty = {"current": None, "voltage": None}
        self._gain_observations = {"current": 0, "voltage": 0}
        self._test_response_deadline = None
        self.output_step_estimated = None
        self.tuning_mode = "learning"
        self.is_running, self.outcome, self.progress = True, "RUNNING", 0
        self.phase, self.state = "acquiring", "Preparing test bench"
        self.target = self.measured = self.error = self.tolerance = None
        self.reading = None
        self.unit = ""
        self.stable_count = 0
        self.stop_outcome = "Not requested"
        self._event(("SIMULATION ONLY. " if self.simulation else "") + f"Started {operation}.")
        try:
            self._require(self.bench.acquire(operation), "Another operation owns the test bench.")
            self._owned = True
            if operation == "calibration":
                if self.baseline_required:
                    self._begin_baseline()
                else:
                    self._prepare_calibration()
            else:
                self._require(self.bench.enter_passive(), "Could not establish a command-free CAN session with controller ACK enabled.")
                self._measurement_started = True
                self._begin_external_test()
                self._assert_passive()
        except Exception as exc:
            self._fail(str(exc))

    def start_calibration(self, now=None):
        self._start("calibration", now)

    def start_test(self, now=None):
        self._start("test", now)

    def _begin_external_test(self):
        """Arm the load before prompting for AC restart. Never send a charger command."""
        self.bench.trace_phase = "test_arm_external_restart"
        self._require(self.bench.load_off(), "Load OFF was not confirmed before arming the test.")
        self._require(self.bench.configure_load("CV", self.recipe.test_cv_voltage), "CV load preparation was not confirmed.")
        self._settle_after_command()
        self._phase("test_wait_restart", "Ready — restart charger AC",
            "Switch charger AC OFF, then ON. The load remains OFF until open-circuit voltage has settled and passed.")
        self._external_restart_deadline = self._now + self._external_restart_timeout
        self.external_restart_evidence = dict(method="observed_dc_low_to_high", ac_switch_instrumented=False,
            low_observed=None, rising_observed=None, controller_ack_enabled=True, application_can_commands=0)
        self._active_stage = None

    def _tick_external_restart(self, reading):
        if reading.load_on or reading.current > self.settings.shutdown_current:
            raise RuntimeError("Startup open-voltage capture requires load OFF and negligible measured current.")
        if self.phase == "test_wait_restart" and self._now >= self._external_restart_deadline:
            raise RuntimeError("External restart timed out. No qualified fresh output startup was observed; restart AC and start a new test.")
        if self.external_restart_evidence["low_observed"] is None:
            if reading.voltage <= self.settings.shutdown_voltage:
                self.external_restart_evidence["low_observed"] = asdict(reading)
                self.detail = "Output is low. Switch charger AC ON; waiting for a fresh voltage rise."
            else:
                self.detail = "Switch charger AC OFF first. Waiting for output to fall before accepting a fresh restart."
            return
        if reading.voltage <= self.settings.shutdown_voltage and self._external_voltage_deadline is None:
            return
        if self._external_voltage_deadline is None:
            self.external_restart_evidence["rising_observed"] = asdict(reading)
            self._external_voltage_deadline = self._now + 15.0
            self._charger_startup_after = self._now
            self._charger_clear_deadline = self._external_voltage_deadline
            self._phase("test_ocv", "Open-circuit voltage test in progress",
                "Waiting for voltage to build and settle with load OFF.")
            self.target, self.tolerance, self.unit = self.recipe.end_voltage, self.recipe.voltage_tolerance, "V"
            self._active_parameter = "endVoltage"
            self._active_stage = dict(name="Open-circuit voltage", phase=self.phase, before=None, after=None,
                unit="V", status="RUNNING", started_at=_utc(), duration_seconds=0.0, _started=self._now)
            self.stage_details.append(self._active_stage)
        self.measured, self.error = reading.voltage, reading.voltage - self.recipe.end_voltage
        self._window.append((reading.timestamp, reading.voltage))
        while len(self._window) > 3 and self._window[-1][0] - self._window[1][0] >= 1.0:
            self._window.pop(0)
        spread = max(v for _, v in self._window) - min(v for _, v in self._window)
        settled = (len(self._window) >= 3 and self._window[-1][0] - self._window[0][0] >= 1.0
            and spread <= self.tolerance / 2 and abs(self._window_slope()) <= self.tolerance / 4)
        self.stable_count = len(self._window) if settled else 0
        if settled:
            self._ocv_plateau = dict(measured=sum(v for _, v in self._window)/len(self._window),
                timestamp=reading.timestamp, sample_count=len(self._window), span_seconds=self._window[-1][0]-self._window[0][0])
        else:
            self._ocv_plateau = None
        in_band = settled and all(abs(v-self.target) <= self.tolerance for _, v in self._window)
        built = self._now - self._phase_started >= 2.0
        expired = self._now >= self._external_voltage_deadline
        self.detail = f"Open-circuit voltage building / settling: {reading.voltage:.2f} V; target {self.target:g} V ±{self.tolerance:g}. Load remains OFF."
        if not (in_band and built) and not expired:
            return
        plateau = self._ocv_plateau
        mean = plateau["measured"] if plateau else None
        passed = bool(in_band and built and reading.voltage > self.recipe.test_cv_voltage)
        note = ("Fresh open-circuit voltage settled for at least 1 s after the initial 2 s build observation; load remained OFF."
                if passed else "Open-circuit voltage did not settle within target tolerance during the 15 s startup window. Loaded tests were not started.")
        if not passed:
            note += (f" Settled voltage {mean:.3f} V." if mean is not None else " No valid settled voltage plateau was obtained.")
            note += f" Required {self.target-self.tolerance:.3f}–{self.target+self.tolerance:.3f} V; last observed {reading.voltage:.3f} V."
        self._active_stage["before"] = mean
        self._result("Open-circuit voltage", mean, "PASS" if passed else "FAIL", note)
        self.startup_voltage_result = dict(self.results[-1], load_on=False, plateau=copy.deepcopy(plateau))
        if not passed:
            self._finish_test()
            return
        self.external_restart_evidence["engagement_voltage"] = reading.voltage
        self._require(self.bench.load_on(), "Startup load input ON was not confirmed.")
        self._external_restart_deadline = self._external_voltage_deadline = None
        self._test_context = "external_current"
        self._external_measurement_stage("current", "Full current", "CV", self.recipe.test_cv_voltage)
        self._event(f"Open-circuit voltage {mean:.3f} V passed; CV 49 V load engaged. No charger command sent.")

    def _external_measurement_stage(self, axis, name, mode, setpoint):
        self.target = self.recipe.cc_current if axis == "current" else self.recipe.end_voltage
        self.tolerance = self.recipe.current_tolerance if axis == "current" else self.recipe.voltage_tolerance
        self.unit = "A" if axis == "current" else "V"
        self.measured = self.error = None
        self._active_test_name = name
        self._active_parameter = "ccCurrent" if axis == "current" else "endVoltage"
        self._expected_load_mode, self._expected_load_setpoint = mode, setpoint
        self._phase("test_" + axis, name + " test in progress",
            f"Load {mode} {setpoint:g} {'V' if mode == 'CV' else 'A'}; charger remains in normal operation.")
        self._settle_after_command()
        self._active_stage = dict(name=name, phase=self.phase, before=None, after=None, unit=self.unit,
            status="RUNNING", started_at=_utc(), duration_seconds=0.0, _started=self._now)
        self.stage_details.append(self._active_stage)
        self.progress = 20 if axis == "current" else 55

    def _prepare_calibration(self):
        self.bench.trace_phase = "parameter_check_update"
        prepare = getattr(self.bench, "prepare_charger_parameters", None)
        if callable(prepare):
            self.state, self.detail = "Checking charger parameters", "Comparing the charger with the selected variant."
            prepare()
            self.preflight_result = copy.deepcopy(getattr(self.bench, "preflight_result", None))
        self._shutdown_for("current")

    def _begin_baseline(self):
        self.bench.trace_phase = "baseline_setup"
        self.baseline_result["status"] = "RUNNING"
        self.baseline_result["detail"] = "Normal-operation readings before parameter programming or calibration trim changes."
        self._require(self.bench.load_off(), "Load input OFF was not confirmed before the baseline check.")
        # Prepare the demand with the input OFF. At the voltage trigger only
        # input ON remains, so serial mode/setpoint writes do not consume the
        # charger's short normal-start window.
        self._require(self.bench.configure_load("CV", self._baseline_engage_voltage), "Baseline CV load setting was not confirmed.")
        self._require(self.bench.charger_start_baseline(), "Baseline CAN Start was not accepted completely.")
        self._baseline_stage("open_voltage")
        self._charger_startup_after = self._now
        self._charger_clear_deadline = self._baseline_deadline
        self._charger_clear_after = None
        self._charger_next_poll = self._now

    def _baseline_stage(self, point):
        is_voltage = point == "open_voltage"
        name = "Baseline open voltage" if is_voltage else "Baseline current"
        self._phase("baseline_" + point, name + " check",
                    "Measuring unchanged charger output; no parameter or calibration trim writes.")
        self.target = self.recipe.end_voltage if is_voltage else self.recipe.cc_current
        self.tolerance = self.recipe.voltage_tolerance if is_voltage else self.recipe.current_tolerance
        self.unit = "V" if is_voltage else "A"
        self.measured = self.error = None
        self.stable_required, self.stability_purpose = 3, "baseline"
        self._settle_after_command()
        self._baseline_deadline = self._now + (15.0 if is_voltage else self._baseline_ramp_timeout)
        self._active_stage = dict(name=name, phase=self.phase, before=None, after=None,
            unit=self.unit, status="RUNNING", started_at=_utc(), duration_seconds=0.0, _started=self._now)
        self.stage_details.append(self._active_stage)
        self.baseline_result[point] = dict(measured=None, unit=self.unit, status="RUNNING",
            duration_seconds=0.0, started_at=self._active_stage["started_at"], finished_at=None,
            load_mode="OFF" if is_voltage else "CV",
            load_setpoint=None if is_voltage else self._baseline_engage_voltage,
            engage_voltage=self._baseline_engage_voltage,
            detail=("Capturing initial open voltage at the load-engagement threshold." if is_voltage
                    else "Current building; waiting for a stable independent reading."))

    def _check_baseline_deadline(self):
        if self._baseline_deadline is not None and self._now >= self._baseline_deadline:
            observed = (f" Last reading {self.reading.voltage:.2f} V / {self.reading.current:.2f} A."
                        if self.reading is not None else " No fresh independent reading.")
            limit = 15.0 if self.phase == "baseline_open_voltage" else self._baseline_ramp_timeout
            raise RuntimeError(f"Baseline unavailable: no usable {'startup voltage' if self.phase == 'baseline_open_voltage' else 'stable current'} within {limit:g} seconds." + observed
                               + " Unit stopped before parameter programming or calibration.")

    def _tick_baseline(self, reading):
        point = self.phase.removeprefix("baseline_")
        is_voltage = point == "open_voltage"
        if is_voltage:
            if reading.load_on or reading.current > self.settings.shutdown_current:
                raise RuntimeError("Baseline open-voltage check requires load OFF and negligible measured current.")
        elif (not reading.load_on or reading.load_mode != "CV"
              or not math.isclose(reading.load_setpoint, self._baseline_engage_voltage, rel_tol=2e-6, abs_tol=1e-6)):
            raise RuntimeError("Baseline current-check load mode, setpoint or input changed unexpectedly.")
        pending_startup_clear = False
        if self._charger_startup_after is not None:
            word, observed_at = reading.charger_fault_word, reading.charger_fault_observed_at
            if (word == 0 and observed_at is not None
                    and observed_at > max(self._charger_startup_after, self._charger_clear_after or self._charger_startup_after)
                    and 0 <= self._now - observed_at <= 2):
                self._end_startup_diagnostic_window()
                self._window.clear()
            elif self._charger_clear_after is not None:
                self._window.clear()
                self.stable_count = 0
                # This one load-input transition may be needed to clear the
                # isolated startup battery-absent indication. The independent
                # threshold sample has already passed limits/fault validation.
                # Preserve the ORIGINAL diagnostic deadline through the next
                # phase: no current baseline or charger write is permitted
                # until an actual fresh clear diagnostic arrives.
                pending_startup_clear = (is_voltage
                    and reading.voltage > self._baseline_engage_voltage
                    and self._now < self._charger_clear_deadline)
                if not pending_startup_clear:
                    self._request_startup_diagnostics()
                    self.detail = "Baseline waiting for fresh charger diagnostics to clear Battery not connected (0x0020)."
                    return
        self.measured = reading.voltage if is_voltage else reading.current
        self.error = self.measured - self.target
        usable = (reading.voltage > (self._baseline_engage_voltage if is_voltage else min(1.0, self.recipe.end_voltage * .1))
                  and (is_voltage or (not reading.unregulated
                       and reading.current > min(.2, self.recipe.cc_current * .1))))
        if not usable:
            self._window.clear()
            self.stable_count = 0
            self.detail = (f"{'Waiting for voltage above ' + format(self._baseline_engage_voltage, 'g') + ' V' if is_voltage else 'Current building'}: {reading.voltage:.2f} V / {reading.current:.2f} A"
                           + (" · load UNREG" if reading.unregulated and not is_voltage else "") + ".")
            return
        if not is_voltage and reading.timestamp < self._not_before:
            return
        # Later indications are active faults once usable measurement starts.
        if not pending_startup_clear:
            self._end_startup_diagnostic_window()
        self._window.append((reading.timestamp, self.measured))
        while len(self._window) > 3 and self._window[-1][0] - self._window[1][0] >= self._baseline_settle_seconds:
            self._window.pop(0)
        spread = max(value for _, value in self._window) - min(value for _, value in self._window)
        self.stable_count = len(self._window) if spread <= self.tolerance / 2 else 0
        if not is_voltage:
            self.detail = (f"Current building / settling: {reading.current:.2f} A at {reading.voltage:.2f} V · "
                           f"need {self._baseline_settle_seconds:g} s stable.")
            if (self._now - self._phase_started < self._minimum_ramp_seconds
                    or self.stable_count < 3
                    or self._window[-1][0] - self._window[0][0] < self._baseline_settle_seconds
                    or abs(self._window_slope()) > max(.005, self.tolerance / (4 * self._baseline_settle_seconds))):
                return
        mean = sum(value for _, value in self._window) / len(self._window)
        self.measured, self.error = mean, mean - self.target
        detail = ("Initial open-voltage snapshot at load-engagement threshold; not stabilized no-load regulation. "
                  if is_voltage else "Current ramp allowed to settle. ") + "Measured before parameter or trim changes; baseline recorded, not a final acceptance test."
        if pending_startup_clear:
            detail += " Load engagement permitted during the bounded startup 0x0020 indication; fresh clear diagnostic still required."
        duration = round(max(0, self._now - self._active_stage["_started"]), 3)
        self.baseline_result[point].update(measured=mean, status="PASS", duration_seconds=duration,
            sample_count=len(self._window), finished_at=_utc(), detail=detail,
            minimum_ramp_seconds=0 if is_voltage else self._minimum_ramp_seconds,
            settled_slope_per_second=self._window_slope())
        self._active_stage.update(before=mean, after=mean, status="PASS", duration_seconds=duration,
                                  finished_at=_utc(), detail=detail)
        self._event(f"{self._active_stage['name']}: {mean:.3f} {self.unit} recorded before parameter/trim changes.")
        self._baseline_deadline = None
        if is_voltage:
            self._require(self.bench.load_on(), "Baseline load input ON was not confirmed.")
            self._baseline_stage("current")
        else:
            self._active_stage = None
            self._shutdown_for("baseline")

    def _window_slope(self):
        """Least-squares trend in independent samples; a slow ramp is not settled."""
        if len(self._window) < 2:
            return 0.0
        t0 = self._window[0][0]
        mean_t = sum(t - t0 for t, _ in self._window) / len(self._window)
        mean_v = sum(v for _, v in self._window) / len(self._window)
        denominator = sum((t - t0 - mean_t) ** 2 for t, _ in self._window)
        return (sum((t - t0 - mean_t) * (v - mean_v) for t, v in self._window) / denominator
                if denominator else 0.0)

    def _phase(self, phase, state, detail):
        self.phase, self.state, self.detail = phase, state, detail
        self.bench.trace_phase = phase
        self._phase_started = self._now
        self._window.clear()
        self.stable_count = 0
        self._pending_response = None
        self._direction_probe = None
        self._refinement_count = 0
        self._refinement_deadline = None
        self._test_response_deadline = None
        self.output_step_estimated = None
        self.tuning_mode = "learning"
        self.stable_required = self.settings.stable_samples
        self.stability_purpose = "test" if self.operation == "test" else "idle"
        if phase.startswith("cal_"):
            self.output_per_duty[phase[4:]] = None
            self._gain_observations[phase[4:]] = 0
            self.stable_required = self.settings.tuning_samples
            self.stability_purpose = "tuning"
        elif phase.startswith("off_"):
            self.stable_required = 3
            self.stability_purpose = "shutdown"
        self._adjustments = self._no_response = self._reversals = self._last_error_sign = 0
        self._event(state + ": " + detail)

    def _add_stability_sample(self, timestamp, value):
        self._window.append((timestamp,value))
        # Keep enough samples for BOTH minimum count and actual duration. A
        # fixed eight-item window at 250 ms cannot span the default two seconds.
        required_seconds = (max(self.settings.stable_seconds, self._baseline_settle_seconds)
                            if self.operation == "test" and self.phase == "test_current" else self.settings.stable_seconds)
        while (len(self._window) > self.settings.stable_samples
               and self._window[-1][0]-self._window[1][0] >= required_seconds):
            self._window.pop(0)

    def _shutdown_for(self, next_axis):
        self.bench.trace_phase = "off_" + next_axis
        self._require(self.bench.charger_shutdown(), "Shutdown command was not accepted completely.")
        self._off_battery_diagnostic = False
        self._charger_clear_after = self._charger_clear_deadline = None
        self._charger_startup_after = None
        self._charger_next_poll = 0.0
        self._phase("off_" + next_axis, "Checking charger output is off",
                    "Waiting for fresh load-bank readings before changing the load setting.")
        self.target = self.tolerance = self.measured = self.error = None
        self.unit = ""

    def _configure_stage(self, axis, test=False, test_setpoint=None, test_name=None):
        self.bench.trace_phase = ("test_" if test else "cal_") + axis
        self._require(self.bench.load_off(), "Load input OFF was not confirmed.")
        mode = "CV" if axis == "current" else "CC"
        if test:
            setpoint = self.recipe.test_cv_voltage if axis == "current" else self.recipe.test_cc_current
        else:
            setpoint = self.recipe.current_cal_load_voltage if axis == "current" else self.recipe.voltage_cal_load_current
        if test_setpoint is not None:
            setpoint = test_setpoint
        self._require(self.bench.configure_load(mode, setpoint), "Load mode/setpoint readback did not match.")
        self._require(self.bench.load_on(), "Load input ON was not confirmed.")
        if not test:
            self._require(self.bench.charger_start_calibration(self.voltage_duty, self.current_duty),
                          "Calibration start and all three duty pairs were not accepted.")
        self.target = self.recipe.cc_current if axis == "current" else self.recipe.end_voltage
        self.tolerance = self.recipe.current_tolerance if axis == "current" else self.recipe.voltage_tolerance
        self.measured = self.error = None
        self.unit = "A" if axis == "current" else "V"
        self._active_parameter = "ccCurrent" if axis == "current" else "endVoltage"
        self._active_test_name = test_name or ("CC current" if axis == "current" else
            "Output / end voltage (calibration mode)" if self._calibration_test and test else "Normal output / end voltage")
        self._expected_load_mode, self._expected_load_setpoint = mode, setpoint
        self._phase(("test_" if test else "cal_") + axis,
                    (self._active_test_name + " test in progress") if test
                    else ("Current" if axis == "current" else "Voltage") + " calibration in progress",
                    f"Load {mode} {setpoint:g} {'V' if mode == 'CV' else 'A'} · Target {self.target:g} {self.unit} ±{self.tolerance:g}.")
        if test and self.bench.capabilities.post_test_can_start:
            self._start_post_test_stage(self._active_test_name)
        self._settle_after_command()
        if test and self.bench.capabilities.post_test_can_start:
            self._test_response_deadline = self._now + self._baseline_ramp_timeout
        if not test or self.bench.capabilities.post_test_can_start:
            # Startup may briefly report battery-absent even in a fresh frame.
            # The window is anchored to this completed start, never to a later
            # fault or poll. Normal starts do not need diagnostic polling.
            self._charger_startup_after = self._now
            self._charger_clear_deadline = self._now + min(15.0, self.settings.shutdown_timeout, self.settings.stage_timeout)
            self._charger_next_poll = self._now
            if self._off_battery_diagnostic:
                self._charger_clear_after = self._charger_startup_after
                self._request_startup_diagnostics()
        self.progress = (45 if axis == "current" else 15) if test else (15 if axis == "current" else 60)
        self._active_stage = dict(name=self._active_test_name if test else axis.title() + " calibration",
            phase=self.phase, before=None, after=None, unit=self.unit, status="RUNNING",
            started_at=_utc(), duration_seconds=0.0, _started=self._now)
        self.stage_details.append(self._active_stage)

    def _start_post_test_stage(self, name):
        self._measurement_started = False
        count_before = self.bench.can_tx_count
        setup_before = getattr(self.bench, "test_setup_can_tx_count", 0)
        start_before = getattr(self.bench, "startup_can_tx_count", 0)
        self.planned_start_count += 1
        self._require(self.bench.begin_post_test(name), "Test-stage CAN Start and listen-only handoff were not confirmed.")
        sent = self.bench.can_tx_count - count_before
        setup_sent = getattr(self.bench, "test_setup_can_tx_count", 0) - setup_before
        starts_sent = getattr(self.bench, "startup_can_tx_count", 0) - start_before
        if starts_sent != 1 or setup_sent < 0 or sent != starts_sent + setup_sent:
            raise RuntimeError("Test startup must contain only verified start-threshold setup and one normal CAN Start.")
        self.startup_can_tx_count += starts_sent
        self._tx_start = self.bench.can_tx_count
        self._measurement_started = True
        self._event(f"{name}: normal CAN Start accepted with temporary 0 V start threshold; measurement now uses listen-only CAN. Cleanup will restore and verify 12 V.")
        if self._calibration_test:
            self._event("Calibration entry only; no parameter, trim or save commands. Firmware mode behaviour and normal cutoff are unverified.")

    def _settle_after_command(self):
        # Synchronous CAN batches take time. Begin settling after completion,
        # and later require acquisition itself to be after this deadline.
        if not self.simulation:
            self._now = max(self._now, time.monotonic())
        self._not_before = self._now + self.settings.settling_seconds

    def _end_startup_diagnostic_window(self):
        self._charger_startup_after = None
        self._charger_clear_after = self._charger_clear_deadline = None
        self._off_battery_diagnostic = False

    def _startup_diagnostic_timeout(self):
        raise RuntimeError("Battery not connected (0x0020): no fresh clear charger diagnostic within the calibration startup deadline.")

    def _request_startup_diagnostics(self):
        if self._charger_clear_after is None:
            return
        if self._now >= self._charger_clear_deadline:
            self._startup_diagnostic_timeout()
        if self.operation == "test":
            # The test contract forbids polling after its one entry frame.
            # Require a fresh clear broadcast; an old fault ageing out is not
            # evidence that startup succeeded.
            return
        if self._now < self._charger_next_poll:
            return
        refresh = getattr(self.bench, "request_charger_diagnostics", None)
        if callable(refresh):
            self._require(refresh(), "Startup diagnostic requests were not accepted completely.")
            if not self.simulation:
                self._now = max(self._now, time.monotonic())
        # Count from the end of the bounded I/O batch so requests cannot bunch
        # up, and never reset the original startup deadline after I/O.
        self._charger_next_poll = self._now + 1.0
        if self._now >= self._charger_clear_deadline:
            self._startup_diagnostic_timeout()

    def _assert_passive(self):
        if self.operation == "test" and self._measurement_started and self.bench.can_tx_count != self._tx_start:
            raise RuntimeError("CAN transmission detected during the standalone test; result invalid.")

    def _sample(self, *, shutdown_confirmation=None):
        reading = self.bench.read(self._now)
        if not self.simulation:
            # Real bounded instrument I/O may consume much of the freshness
            # budget. Compare acquisition time against the clock AFTER I/O.
            self._now = max(self._now, time.monotonic())
        if not isinstance(reading, AutomationReading):
            raise RuntimeError("Independent load reading is missing.")
        if any(not _finite(v) for v in (reading.voltage, reading.current, reading.power, reading.timestamp, reading.load_setpoint)):
            raise RuntimeError("Load returned a non-finite measurement.")
        if min(reading.voltage, reading.current, reading.power, reading.load_setpoint) < 0:
            raise RuntimeError("Load returned a negative measurement or setpoint.")
        if reading.timestamp > self._now + .05 or self._now - reading.timestamp > self.settings.sample_max_age:
            raise RuntimeError("Load measurements are stale or have an invalid timestamp.")
        if not reading.source or reading.source != self.bench.capabilities.reference_identity:
            raise RuntimeError("Measurement did not come from the approved independent reference.")
        if type(reading.sequence) is not int or reading.sequence < self._last_sequence:
            raise RuntimeError("Load acquisition sequence moved backwards.")
        if reading.fault:
            raise RuntimeError("Load fault: " + reading.fault)
        if type(reading.unregulated) is not bool:
            raise RuntimeError("Load regulation status is invalid.")
        if (reading.voltage > self.recipe.max_voltage or reading.current > self.recipe.max_current
                or max(reading.power, reading.voltage * reading.current) > self.recipe.max_power):
            raise RuntimeError("Measured output exceeded the variant's bench limits.")
        word, observed_at = reading.charger_fault_word, reading.charger_fault_observed_at
        if word is not None and (type(word) is not int or not 0 <= word <= 0xFFFF):
            raise RuntimeError("Charger fault word is invalid.")
        if word is not None and observed_at is None:
            raise RuntimeError("Charger fault observation time is missing.")
        if observed_at is not None and (not _finite(observed_at) or observed_at > self._now + .05):
            raise RuntimeError("Charger fault observation time is invalid.")
        recent = observed_at is not None and 0 <= self._now - observed_at <= 2
        if word and recent:
            external_wait = (self.operation == "test" and self.phase in ("test_wait_restart", "test_ocv")
                and word == BATTERY_NOT_CONNECTED and self._external_restart_deadline is not None
                and self._now < (self._external_voltage_deadline if self.phase == "test_ocv" else self._external_restart_deadline))
            expected_off = (self.operation == "calibration"
                and (shutdown_confirmation is True or (shutdown_confirmation is None and self.phase.startswith("off_")))
                and word == BATTERY_NOT_CONNECTED and reading.voltage <= self.settings.shutdown_voltage
                and reading.current <= self.settings.shutdown_current)
            waiting_for_start = (((self.operation == "calibration" and self.phase.startswith(("cal_", "baseline_")))
                                 or (self.operation == "test" and self.phase == "test_current"))
                and shutdown_confirmation is None
                and self._charger_startup_after is not None and word == BATTERY_NOT_CONNECTED
                and (self._now < self._charger_clear_deadline or self._charger_clear_after is not None))
            if external_wait:
                if reading.voltage > self.settings.shutdown_voltage:
                    if self._charger_startup_after is None:
                        self._charger_startup_after = self._now
                        self._charger_clear_deadline = self._now + 15.0
                    self._charger_clear_after = max(self._charger_clear_after or self._now, observed_at)
            elif expected_off:
                self._off_battery_diagnostic = True
            elif waiting_for_start:
                self._charger_clear_after = max(self._charger_startup_after,
                    self._charger_clear_after or self._charger_startup_after, observed_at)
            elif not waiting_for_start:
                raise RuntimeError(describe_charger_fault(word))
        if reading.sequence == self._last_sequence:
            return None
        self._last_sequence = reading.sequence
        self.reading = reading
        self.samples.append(dict(asdict(reading), elapsed_seconds=round(self._now-self._started, 3),
                                 phase=self.phase, current_duty=self.current_duty, voltage_duty=self.voltage_duty,
                                 derived_power_w=reading.voltage*reading.current))
        return reading

    def tick(self, now=None):
        if not self.is_running:
            return
        next_now = time.monotonic() if now is None else now
        try:
            if not _finite(next_now) or next_now < self._now:
                raise RuntimeError("Invalid or backwards workflow clock.")
            self._now = next_now
            self._assert_passive()
            self._check_baseline_deadline()
            self._check_test_response_deadline()
            if self._charger_clear_after is not None and self._now >= self._charger_clear_deadline:
                self._startup_diagnostic_timeout()
            timeout = self.settings.shutdown_timeout if self.phase.startswith("off_") else self.settings.stage_timeout
            if self.phase in ("test_wait_restart", "test_ocv"):
                timeout = self._external_restart_timeout
            elif self.operation == "test" and self.phase in ("test_current", "test_voltage"):
                timeout = self._baseline_ramp_timeout
            if self.phase == "test_termination":
                timeout = self._test_termination["max_seconds"]
            elif self.phase == "test_soak":
                timeout += self.recipe.test_plan["soak_seconds"]
            if self._now - self._phase_started > timeout:
                if self.phase == "test_termination":
                    self._termination_result("INDETERMINATE" if self._calibration_test else "FAIL",
                                             "No confirmed output shutdown before the end-current test timeout; calibration-mode cutoff is not demonstrated." if self._calibration_test
                                             else "No confirmed output shutdown before the end-current test timeout.")
                    return
                raise RuntimeError("Timed out waiting for " + self.state.lower() + ".")
            reading = self._sample()
            self._assert_passive()
            self._check_baseline_deadline()
            self._check_test_response_deadline()
            if self._charger_clear_after is not None and self._now >= self._charger_clear_deadline:
                self._startup_diagnostic_timeout()
            if self._now - self._phase_started > timeout:
                if self.phase == "test_termination":
                    self._termination_result("INDETERMINATE" if self._calibration_test else "FAIL",
                                             "No confirmed output shutdown before the end-current test timeout; calibration-mode cutoff is not demonstrated." if self._calibration_test
                                             else "No confirmed output shutdown before the end-current test timeout.")
                    return
                raise RuntimeError("Timed out while acquiring independent load measurements.")
            if reading is None:
                return
            if self.phase in ("test_wait_restart", "test_ocv"):
                self._tick_external_restart(reading)
                return
            if self.phase.startswith("baseline_"):
                self._tick_baseline(reading)
                return
            if self.phase == "test_termination":
                self._tick_termination(reading)
                return
            if self.phase == "test_soak":
                self._tick_soak(reading)
                return
            if self.phase.startswith("off_"):
                if reading.voltage <= self.settings.shutdown_voltage and reading.current <= self.settings.shutdown_current:
                    self.stable_count += 1
                else:
                    self.stable_count = 0
                if self.stable_count >= 3:
                    axis = self.phase[4:]
                    if axis == "baseline":
                        self._require(self.bench.load_off(), "Load OFF was not confirmed after the baseline check.")
                        self.baseline_result["status"] = "PASS"
                        self.baseline_result["detail"] = "Both baseline readings recorded; output shutdown confirmed before parameter programming."
                        self._prepare_calibration()
                    elif axis == "finish":
                        self._require(self.bench.load_off(), "Load input OFF was not confirmed.")
                        self.stop_outcome = "Output below approved shutdown thresholds; load OFF confirmed"
                        self._finish("CALIBRATED — PERSISTENCE UNVERIFIED",
                                     "Calibration finished", "Current and voltage save requests were accepted. Verify saved calibration after restart.")
                    else:
                        self._configure_stage(axis)
                return
            axis = "current" if self.phase.endswith("current") else "voltage"
            expected_mode = self._expected_load_mode
            expected_setpoint = self._expected_load_setpoint
            if not reading.load_on or reading.load_mode != expected_mode or not math.isclose(reading.load_setpoint, expected_setpoint, rel_tol=2e-6, abs_tol=1e-6):
                raise RuntimeError("Load mode, input state or setpoint changed unexpectedly.")
            if self._charger_startup_after is not None:
                word, observed_at = reading.charger_fault_word, reading.charger_fault_observed_at
                if self._charger_clear_after is not None and self._now >= self._charger_clear_deadline:
                    self._startup_diagnostic_timeout()
                if (word == 0 and observed_at is not None
                        and observed_at > max(self._charger_startup_after, self._charger_clear_after or self._charger_startup_after)
                        and 0 <= self._now - observed_at <= 2):
                    self._end_startup_diagnostic_window()
                    self._window.clear()
                    self.stable_count = 0
                elif self._charger_clear_after is not None:
                    self._window.clear()
                    self.stable_count = 0
                    self._request_startup_diagnostics()
                    self.detail = "Waiting for fresh charger diagnostics to clear Battery not connected (0x0020); no adjustment or save."
                    return
                elif self._now >= self._charger_clear_deadline:
                    self._end_startup_diagnostic_window()
            self.measured = reading.current if axis == "current" else reading.voltage
            self.error = self.measured - self.target
            if self.operation == "test" and axis == "current":
                self._external_current_peak = max(self._external_current_peak or 0.0, reading.current)
                if reading.current <= min(.2, self.recipe.cc_current * .1) or reading.voltage <= self.settings.shutdown_voltage:
                    self._window.clear()
                    self.stable_count = 0
                    self.detail = f"Waiting for normal current build: {reading.current:.2f} A at {reading.voltage:.2f} V."
                    return
            if self.operation == "test" and axis == "voltage" and abs(reading.current - expected_setpoint) > self.recipe.current_tolerance:
                self._window.clear()
                self.stable_count = 0
                self.detail = f"Waiting for CC load to draw {expected_setpoint:g} A before the end-voltage measurement."
                return
            if self._test_response_deadline is not None:
                expected_current = self.target if axis == "current" else expected_setpoint
                if reading.current < min(.2, expected_current * .1) or reading.voltage < 1:
                    self._window.clear()
                    self.stable_count = 0
                    self.detail = (f"Waiting for load response after CAN Start: {reading.voltage:.2f} V / "
                                   f"{reading.current:.2f} A; load {expected_mode} {expected_setpoint:g}, input ON.")
                    return
                self._test_response_deadline = None
            if self._wait_for_regulation(reading):
                return
            if reading.timestamp < self._not_before:
                return
            # Once tuning/stability measurement begins, later battery-absent
            # indications are active faults, even within the original window.
            if self.operation == "calibration" or self._calibration_test:
                self._end_startup_diagnostic_window()
            self._add_stability_sample(reading.timestamp,self.measured)
            confirming = (self.operation == "calibration" and abs(self.measured - self.target) <= self.tolerance
                          and not self._can_refine_voltage(axis, self.measured))
            if confirming:
                # Only consecutive in-band acquisitions can count toward save.
                last_outside = max((n for n, (_, v) in enumerate(self._window)
                                    if abs(v - self.target) > self.tolerance), default=-1)
                if last_outside >= 0:
                    del self._window[:last_outside + 1]
            elif self.operation == "calibration":
                # A short rolling window is sufficient to decide the next trim.
                # Older final-confirmation samples must not slow that decision.
                while (len(self._window) > self.settings.tuning_samples
                       and self._window[-1][0] - self._window[1][0] >= self.settings.tuning_seconds):
                    self._window.pop(0)
            spread = max(v for _, v in self._window) - min(v for _, v in self._window)
            at_target = all(abs(v - self.target) <= self.tolerance for _, v in self._window)
            # Tune with a short independent window; the save still requires the
            # complete final count AND duration, all inside the target band.
            self.stability_purpose = ("save" if confirming else "tuning") if self.operation == "calibration" else "test"
            self.stable_required = (self.settings.tuning_samples if self.stability_purpose == "tuning"
                                    else self.settings.stable_samples)
            stable_seconds = (self.settings.tuning_seconds if self.stability_purpose == "tuning"
                              else self.settings.stable_seconds)
            stable = (len(self._window) >= self.stable_required
                      and self._window[-1][0] - self._window[0][0] >= stable_seconds
                      and spread <= self.tolerance / 2)
            self.stable_count = len(self._window) if spread <= self.tolerance / 2 else 0
            if not stable:
                if self.operation == "test" and axis == "current":
                    slope = self._window_slope()
                    elapsed = self._now - self._phase_started
                    rising = abs(slope) > max(.005, self.tolerance / (4 * self._baseline_settle_seconds))
                    label = "Current building" if elapsed < self._minimum_ramp_seconds or rising else "Current settling"
                    self.detail = (f"{label}: {reading.current:.2f} A at {reading.voltage:.2f} V · "
                        f"{elapsed:.1f} s observed · trend {slope:+.3f} A/s; waiting for a {self._baseline_settle_seconds:g} s plateau.")
                return
            mean = sum(v for _, v in self._window) / len(self._window)
            self.measured, self.error = mean, mean - self.target
            if self._active_stage is not None and self._active_stage["before"] is None:
                self._active_stage["before"] = mean
            if self.operation == "test":
                if axis == "current" and (self._now - self._phase_started < self._minimum_ramp_seconds
                        or self._window[-1][0] - self._window[0][0] < self._baseline_settle_seconds
                        or abs(self._window_slope()) > max(.005, self.tolerance / (4 * self._baseline_settle_seconds))):
                    self.detail = (f"Full current building / settling: {mean:.2f} A; "
                                   f"observing at least {self._minimum_ramp_seconds:g} s and a settled current trend.")
                    return
                self._test_result(axis, mean)
                return
            if self._pending_response is not None:
                before, expected_sign, duty_change, before_min, before_max = self._pending_response
                change = mean - before
                if abs(change) <= self.tolerance * .05:
                    self._no_response += 1
                elif change * expected_sign < 0:
                    raise RuntimeError("Trim response moved in the wrong direction; check commissioned settings.")
                else:
                    self._no_response = 0
                    sample_min = min(v for _, v in self._window)
                    sample_max = max(v for _, v in self._window)
                    margin = self.tolerance * .05
                    separated = (sample_min > before_max + margin if expected_sign > 0
                                 else sample_max < before_min - margin)
                    if separated:
                        self._learn_output_gain(axis, change, duty_change)
                self._pending_response = None
                if self._no_response >= self.settings.maximum_no_response:
                    raise RuntimeError("No measurable response to repeated duty adjustments.")
            if at_target:
                if self._can_refine_voltage(axis, mean):
                    self._adjust_axis(axis, mean, refine=True)
                    return
                # Refinement can expire while collecting a short tuning window.
                # Expiry never substitutes for the complete save confirmation.
                if (len(self._window) < self.settings.stable_samples
                        or self._window[-1][0] - self._window[0][0] < self.settings.stable_seconds):
                    return
                # A stable target needs no direction assumption or extra probe.
                if self._direction_probe is not None:
                    self._direction_probe.update(status="target_reached", final_mean=mean,
                                                 final_duty=getattr(self, axis + "_duty"))
                    self._direction_probe = None
                    self._event(f"{axis.title()} reached target during direction detection; no further adjustment needed.")
                elif self.trim_directions[axis] == 0:
                    self.direction_detection[axis] = dict(status="not_needed_at_target", direction=0,
                        final_mean=mean, final_duty=getattr(self, axis + "_duty"))
                self._save_axis(axis, mean)
            else:
                if self.trim_directions[axis] == 0 and self._discover_direction(axis, mean):
                    return
                self._adjust_axis(axis, mean)
        except Exception as exc:
            self._fail(str(exc))

    def _learn_output_gain(self, axis, output_change, duty_change):
        """Learn only from separated stable windows around an accepted trim."""
        if not _finite(duty_change) or abs(duty_change) < self.settings.duty_step / 2:
            return
        gain = abs(output_change / duty_change)
        if not _finite(gain) or gain <= 0:
            return
        previous = self.output_per_duty[axis]
        # A higher observed sensitivity calls for smaller commands. Retaining
        # the largest established slope avoids amplifying a weak/noisy reply.
        self.output_per_duty[axis] = max(gain, previous) if previous is not None else gain
        self._gain_observations[axis] += 1

    def _can_refine_voltage(self, axis, measured):
        """At most two small, predicted improvements; never chase measurement noise."""
        if (self.operation != "calibration" or axis != "voltage"
                or self._refinement_count >= 2 or self._reversals
                or (self._refinement_deadline is not None and self._now >= self._refinement_deadline)
                or self._adjustments >= self.settings.maximum_adjustments
                or self.trim_directions[axis] not in (-1, 1)):
            return False
        gain = self.output_per_duty[axis]
        error = self.target - measured
        if gain is None or not min(.025, self.tolerance / 4) < abs(error) <= self.tolerance:
            return False
        step = self.settings.duty_step
        predicted = measured + math.copysign(gain * step, error)
        duty = round(self.voltage_duty + math.copysign(step, error) * self.trim_directions[axis], 1)
        return (self.settings.voltage_min <= duty <= self.settings.voltage_max
                and abs(predicted - self.target) < abs(error) - .001
                and abs(predicted - self.target) <= self.tolerance)

    def _check_test_response_deadline(self):
        if self._test_response_deadline is not None and self._now >= self._test_response_deadline:
            last = (f"Last load reading {self.reading.voltage:.2f} V / {self.reading.current:.2f} A. "
                    if self.reading is not None else "No fresh load reading. ")
            limit = self._baseline_ramp_timeout
            raise RuntimeError(f"No load response within {limit:g} seconds of CAN Start. " + last
                + "Load mode/input were confirmed, but charger output was not established. No automatic CAN restart was sent.")

    def _adjust_axis(self, axis, measured, *, refine=False):
        if self._adjustments >= self.settings.maximum_adjustments:
            raise RuntimeError("Maximum automatic duty adjustments reached.")
        sign = 1 if measured < self.target else -1
        if self._last_error_sign and sign != self._last_error_sign:
            self._reversals += 1
        self._last_error_sign = sign
        if self._reversals > self.settings.maximum_reversals:
            raise RuntimeError("Output oscillates around target without settling.")
        direction = self.trim_directions[axis]
        if direction not in (-1, 1):
            raise RuntimeError("Trim direction has not been established from stable measurements.")
        old_duty = getattr(self, axis + "_duty")
        gain = self.output_per_duty[axis]
        fine_step = getattr(self.settings, axis + "_output_step")
        remaining_error = abs(self.target - measured)
        self.tuning_mode = "learning" if gain is None else "coarse" if remaining_error > 3 * fine_step else "fine"
        requested_change = (remaining_error * .75 if self.tuning_mode == "coarse"
                            else min(fine_step, remaining_error))
        # A/V are measured output units, not CAN duty units. The first small
        # probe establishes the conversion; later steps aim at the requested
        # output change and shrink as the target approaches. Hardware resolution
        # can make the minimum achievable output change larger than requested.
        desired_duty = requested_change / gain if gain is not None else self.settings.duty_step
        step_limit = self.settings.max_duty_step
        # First establish the response twice. Honour tighter station caps and
        # return to normal steps near target or after a target crossing.
        if (axis == "current" and gain is not None and self._gain_observations[axis] >= 2
                and self._reversals == 0 and step_limit >= 1.0
                and remaining_error > max(10 * fine_step, 5 * self.tolerance)):
            step_limit = self.settings.max_current_coarse_step
        if refine:
            step_limit = self.settings.duty_step
            self.tuning_mode = "target refinement"
            self._refinement_count += 1
            if self._refinement_deadline is None:
                self._refinement_deadline = self._now + 8.0
        step_units = max(1, math.floor(min(desired_duty, step_limit) / self.settings.duty_step + 1e-8))
        step = round(step_units * self.settings.duty_step, 1)
        low, high = (getattr(self.settings, axis + suffix) for suffix in ("_min", "_max"))
        value = round(min(high, max(low, old_duty + sign * direction * step)), 1)
        if value == old_duty:
            raise RuntimeError(f"{axis.title()} duty reached the approved adjustment bound.")
        voltage = value if axis == "voltage" else self.voltage_duty
        current = value if axis == "current" else self.current_duty
        self._require(self.bench.charger_apply_duties(voltage, current), "Not all duty adjustment frames were accepted.")
        setattr(self, axis + "_duty", value)
        self._adjustments += 1
        duty_change = value - old_duty
        self._pending_response = (measured, sign, duty_change,
                                  min(v for _, v in self._window), max(v for _, v in self._window))
        self.output_step_estimated = abs(duty_change) * gain if gain is not None else None
        self.stable_required, self.stability_purpose = self.settings.tuning_samples, "tuning"
        estimate = (f"expected change {self.output_step_estimated:.4g} {self.unit}"
                    if self.output_step_estimated is not None else "learning output response")
        self.detail = f"{self.tuning_mode.title()} {axis} adjustment · {estimate}; confirming the measured response."
        self._event(f"{axis.title()} duty {value:.1f}; measured {measured:.4g} {self.unit}; "
                    f"requested output step {requested_change:g} {self.unit}; estimated {self.output_step_estimated}.")
        self._settle_after_command()
        self._window.clear()
        self.stable_count = 0

    def _discover_direction(self, axis, measured):
        """Infer trim polarity from bounded steps and disjoint stable windows.

        Called only after fresh, regulated samples have met both stability
        duration and count. A probe uses the minimum 0.1 step, limits, settlement,
        timeout and adjustment budget as normal tuning. Inconclusive movement
        never assigns a direction and cannot trigger a calibration save.
        """
        if not self.settings.auto_direction:
            raise RuntimeError("Automatic direction detection is not enabled.")
        current_duty = getattr(self, axis + "_duty")
        low, high = (getattr(self.settings, axis + suffix) for suffix in ("_min", "_max"))
        sample_min = min(value for _, value in self._window)
        sample_max = max(value for _, value in self._window)
        if self._direction_probe is None:
            # Prefer increasing duty; use a decreasing probe at the upper bound.
            duty_sign = 1 if round(current_duty + self.settings.duty_step, 1) <= high else -1
            self._direction_probe = dict(axis=axis, status="detecting", attempts=0,
                baseline_duty=current_duty, baseline_mean=measured,
                baseline_min=sample_min, baseline_max=sample_max,
                duty_sign=duty_sign, samples=[])
            self.direction_detection[axis] = self._direction_probe
        else:
            probe = self._direction_probe
            if probe["axis"] != axis:
                raise RuntimeError("Direction detection changed axis unexpectedly.")
            probe["samples"].append(dict(duty=current_duty, mean=measured,
                minimum=sample_min, maximum=sample_max, sample_count=len(self._window),
                duration_seconds=self._window[-1][0] - self._window[0][0]))
            # A difference in means alone can be ordinary measurement noise.
            # Require the full new stable range to clear the baseline range.
            margin = self.tolerance * .05
            response_sign = (1 if sample_min > probe["baseline_max"] + margin else
                             -1 if sample_max < probe["baseline_min"] - margin else 0)
            if response_sign:
                direction = response_sign * probe["duty_sign"]
                self.trim_directions[axis] = direction
                probe.update(status="detected", direction=direction,
                             measured_change=measured - probe["baseline_mean"],
                             duty_change=current_duty - probe["baseline_duty"])
                self._learn_output_gain(axis, probe["measured_change"], probe["duty_change"])
                self._direction_probe = None
                self._no_response = 0
                response = "raises" if direction == 1 else "lowers"
                self.detail = f"Increasing {axis} duty {response} output; automatically tuning to target."
                self._event(f"{axis.title()} direction detected: increasing duty {response} output.")
                return False
        probe = self._direction_probe
        if probe["attempts"] >= self.settings.maximum_no_response:
            probe["status"] = "inconclusive"
            raise RuntimeError(f"Could not determine {axis} trim direction from stable measurements; no calibration save was sent for this stage.")
        if self._adjustments >= self.settings.maximum_adjustments:
            raise RuntimeError("Maximum automatic duty adjustments reached during direction detection.")
        next_duty = round(current_duty + probe["duty_sign"] * self.settings.duty_step, 1)
        if not low <= next_duty <= high:
            probe["status"] = "bound_reached"
            raise RuntimeError(f"{axis.title()} duty bound reached before a clear direction response.")
        voltage = next_duty if axis == "voltage" else self.voltage_duty
        current = next_duty if axis == "current" else self.current_duty
        self._require(self.bench.charger_apply_duties(voltage, current),
                      "Direction-detection adjustment was not completely accepted.")
        setattr(self, axis + "_duty", next_duty)
        self._adjustments += 1
        probe["attempts"] += 1
        self.output_step_estimated = None
        self.tuning_mode = "learning"
        self.stable_required, self.stability_purpose = self.settings.tuning_samples, "tuning"
        self.detail = (f"Detecting {axis} adjustment direction · duty {next_duty:.1f}; "
                       "waiting for stable load readings.")
        self._event(f"{axis.title()} direction probe {probe['attempts']}: duty {next_duty:.1f}; baseline {probe['baseline_mean']:.4g} {self.unit}.")
        self._settle_after_command()
        self._window.clear()
        self.stable_count = 0
        return True

    def _wait_for_regulation(self, reading):
        """Keep UNREG visible, but never tune/save/pass using its samples.

        Input may be enabled before the source starts; the existing stage
        deadline bounds this wait. Protection flags still fail in _sample.
        Shutdown stages use independent low-output evidence instead.
        """
        if not reading.unregulated:
            if self.detail.startswith("Waiting for load regulation (UNREG)"):
                self.detail = "Load regulation restored; checking fresh stable readings."
            return False
        self._window.clear()
        self.stable_count = 0
        if self.operation == "calibration":
            # A loss of regulation invalidates the before/after comparison.
            # Relearn sensitivity after regulated measurements return.
            self._pending_response = None
            if self._direction_probe is not None:
                self._direction_probe["status"] = "interrupted_by_unregulated_load"
                self._direction_probe = None
            axis = "current" if self.phase.endswith("current") else "voltage"
            self.output_per_duty[axis] = None
            self._gain_observations[axis] = 0
            self.output_step_estimated = None
            self.tuning_mode = "learning"
            self.stable_required, self.stability_purpose = self.settings.tuning_samples, "tuning"
        self.detail = "Waiting for load regulation (UNREG); no adjustment or calibration save."
        return True

    def _result(self, name, measured, status, note=""):
        if self._active_stage is not None:
            self._active_stage.update(after=measured, status=status, detail=note,
                duration_seconds=round(max(0, self._now-self._active_stage["_started"]), 3), finished_at=_utc())
        self.results.append(dict(name=name, target=self.target, measured=measured, error=None if measured is None else measured-self.target,
                                 tolerance=self.tolerance, unit=self.unit, status=status, note=note, detail=note,
                                 required=self.target, programmed=self.recipe.charger_values().get(self._active_parameter),
                                 source=self.bench.capabilities.reference_identity,
                                 timestamp=_utc(), sample_count=len(self._window),
                                 before=self._active_stage.get("before") if self._active_stage else None,
                                 duration_seconds=self._active_stage.get("duration_seconds") if self._active_stage else None))

    def _save_axis(self, axis, measured):
        if axis in self._saves:
            raise RuntimeError("Duplicate calibration save prevented.")
        self._saves.add(axis)  # never repeat a save after an ambiguous/failed return
        self._require(self.bench.charger_save(axis), f"{axis.title()} save request was not accepted; do not retry automatically.")
        self._result(axis.title() + " calibration", measured, "SAVE REQUEST ACCEPTED",
                     "Independent readings stable; charger persistence not verified.")
        self.progress = 50 if axis == "current" else 90
        self._shutdown_for("voltage" if axis == "current" else "finish")

    def _test_result(self, axis, measured):
        status = "PASS" if all(abs(v-self.target) <= self.tolerance for _, v in self._window) else "FAIL"
        sample_low, sample_high = min(v for _, v in self._window), max(v for _, v in self._window)
        note = (f"Settled mean {measured:.3f} {self.unit}, samples {sample_low:.3f}–{sample_high:.3f} {self.unit}; required {self.target-self.tolerance:.3f}–{self.target+self.tolerance:.3f} {self.unit}. "
                + ("Passed the measured range." if status == "PASS" else "One or more settled readings are outside the required range."))
        self._result(self._active_test_name, measured, status, note + " No application CAN commands were sent.")
        self.results[-1]["load_mode"] = self._expected_load_mode
        self.results[-1]["load_setpoint"] = self._expected_load_setpoint
        self.results[-1]["test_start_method"] = self.bench.capabilities.test_start_method
        self.results[-1]["normal_operation_verified"] = False if self._calibration_test else None
        if self._test_context == "external_current":
            self.results[-1]["peak_current_a"] = self._external_current_peak
            self.results[-1]["plateau_current_a"] = measured
            setpoint = 5.0
            self._require(self.bench.transition_load_to_cc(setpoint), "Input-ON CV-to-CC transition was not confirmed; test interrupted.")
            self._assert_passive()
            self._test_context = "external_cutoff"
            self._begin_termination()
            return
        if self._test_context == "post_current":
            self._test_context = "post_voltage"
            self._configure_stage("voltage", test=True)
            return
        if self._test_context == "core_voltage":
            self._test_context = "core_current"
            self._configure_stage("current", test=True)
            return
        self._advance_test()

    def _advance_test(self):
        if self._test_extras:
            axis, setpoint, name = self._test_extras.pop(0)
            self._test_context = "extra"
            self._configure_stage("voltage" if axis == "soak" else axis, test=True,
                                  test_setpoint=setpoint, test_name=name)
            if axis == "soak":
                self._phase("test_soak", "Sustained-load test in progress",
                            f"Load CC {setpoint:g} A · Recording DC voltage, current and derived power.")
                self._soak_started = None
                self._soak_samples = []
            return
        if self.recipe.termination_test.get("enabled"):
            self._begin_termination()
            return
        self.target, self.tolerance, self.unit = self.recipe.end_current, self.recipe.end_current_tolerance, "A"
        self._active_parameter = "endCurrent"
        # This pending check has no measurement. Never relabel the last voltage
        # result as amperes in the operator snapshot.
        self.measured = self.error = None
        self.stable_count = 0
        self._window.clear()
        self._result("End current", None, "NOT TESTED", "A validated charge-termination procedure is still required.")
        self._finish_test()

    def _finish_test(self):
        self._assert_passive()
        cleanup_ok = self._cleanup_test()
        failed = any(item["status"] == "FAIL" for item in self.results)
        incomplete = any(item["status"] in ("NOT TESTED", "INDETERMINATE", "ERROR") for item in self.results)
        outcome = "ERROR" if not cleanup_ok else "FAIL" if failed else "INCOMPLETE" if incomplete else "PASS"
        detail = "Load OFF confirmed. Switch charger AC OFF before disconnecting. No charger parameters or calibration values were changed during testing."
        if not cleanup_ok:
            detail = "Test cleanup failed: " + self._test_cleanup_error
        if self._calibration_test:
            detail = "Calibration-mode diagnostic only; saved-trim retention and normal charging/cutoff behaviour are unverified. " + detail
        if incomplete:
            detail = "Some required checks are untested or inconclusive. " + detail
        if failed:
            reasons = [f"{item['name']}: {item.get('detail') or item.get('note') or 'Outside the required range.'}"
                       for item in self.results if item["status"] == "FAIL"]
            detail = " ".join(reasons) + " " + detail
        self._finish(outcome, "Test report ready", detail)

    def _cleanup_test(self):
        """Close the passive interval, then allow only the bench's bounded cleanup."""
        if self._test_cleanup_done:
            return self._test_cleanup_ok
        self._test_cleanup_done = True
        self._test_cleanup_ok = False
        errors = []
        try:
            self._assert_passive()
        except Exception as exc:
            errors.append(str(exc))
        self._measurement_started = False
        self.bench.trace_phase = "test_cleanup"
        try:
            self._require(self.bench.finish_test(), "Command-free test cleanup did not confirm load OFF.")
            restored = getattr(self.bench, "test_parameter_restore_result", {})
            if (restored.get("status") != "PASS" or restored.get("parameters_changed") is not False
                    or restored.get("restoration_required") is not False):
                raise RuntimeError("Test cleanup did not confirm that charger parameters remained unchanged.")
            self.stop_outcome = "Load OFF confirmed; switch charger AC OFF before disconnecting. No charger command sent."
        except Exception as exc:
            errors.append(str(exc))
        if self.bench.can_tx_count != self._run_tx_start:
            errors.append("Application CAN transmissions occurred during the external-restart test; result invalid.")
        self._test_cleanup_ok = not errors
        self._test_cleanup_error = "; ".join(errors)
        if errors:
            self.stop_outcome = "Cleanup unconfirmed: " + self._test_cleanup_error
        self._event(self.stop_outcome)
        return self._test_cleanup_ok

    def _validate_load_state(self, reading, mode, setpoint):
        if not reading.load_on or reading.load_mode != mode or not math.isclose(reading.load_setpoint,setpoint,rel_tol=2e-6,abs_tol=1e-6):
            raise RuntimeError("Load input, mode or setpoint changed; this is not evidence of charger shutdown.")

    def _tick_soak(self, reading):
        self._validate_load_state(reading, "CC", self._expected_load_setpoint)
        self.measured, self.error = reading.voltage, reading.voltage-self.recipe.end_voltage
        if reading.unregulated and self._soak_started is not None:
            raise RuntimeError("Load lost regulation during the sustained-load measurement interval.")
        if self._wait_for_regulation(reading):
            return
        if self._now < self._not_before:
            return
        if self._soak_started is None:
            self._add_stability_sample(reading.timestamp,reading.voltage)
            self.stable_count = len(self._window)
            if len(self._window) < self.settings.stable_samples or self._window[-1][0]-self._window[0][0] < self.settings.stable_seconds:
                return
            self._soak_started = self._now
            self._event("Sustained-load measurement interval started.")
        self._soak_samples.append(reading)
        duration = self._now-self._soak_started
        total = self.recipe.test_plan["soak_seconds"]
        self.detail = f"Load CC {self._expected_load_setpoint:g} A · {min(duration,total):.1f} / {total:g} s observed."
        if duration < total or len(self._soak_samples) < 3:
            return
        volts = [item.voltage for item in self._soak_samples]
        amps = [item.current for item in self._soak_samples]
        watts = [item.voltage*item.current for item in self._soak_samples]
        passed = (all(abs(v-self.recipe.end_voltage) <= self.recipe.voltage_tolerance for v in volts)
                  and all(abs(a-self._expected_load_setpoint) <= self.recipe.current_tolerance for a in amps))
        self._result("Sustained-load stability", sum(volts)/len(volts), "PASS" if passed else "FAIL",
                     "DC voltage/current within recipe tolerances throughout sampled interval; power derived as V × I. No fast-ripple or efficiency claim.")
        self.results[-1].update(duration_seconds=duration, sample_count=len(volts),
                               load_current_a=self._expected_load_setpoint,
                               voltage_min=min(volts), voltage_max=max(volts), voltage_mean=sum(volts)/len(volts),
                               current_min=min(amps), current_max=max(amps), current_mean=sum(amps)/len(amps),
                               derived_power_min_w=min(watts), derived_power_max_w=max(watts), derived_power_mean_w=sum(watts)/len(watts))
        self._advance_test()

    def _begin_termination(self):
        self.bench.trace_phase = "test_termination"
        original = self.recipe.termination_test
        self._test_termination = dict(original, start_current=5.0, minimum_current=.5,
            step_current=.1, dwell_seconds=1.0, confirm_seconds=10.0, max_seconds=180.0,
            floor_observation_seconds=3.0, current_follow_timeout_seconds=10.0)
        t = self._test_termination
        self.target, self.tolerance, self.unit = self.recipe.end_current, self.recipe.end_current_tolerance, "A"
        self._active_parameter = "endCurrent"
        self.measured = self.error = None
        self._phase("test_termination", "End-current shutdown test in progress",
            "CC 5 A → 0.5 A in fixed 0.1 A steps; load stays ON. No separate voltage check or hold.")
        self.progress = 65
        self._termination_setpoint = 5.0
        self._termination_step_started = self._now
        self._termination_last_on = self._termination_first_off = None
        self._termination_control_complete = True
        self._termination_floor_started = None
        self._termination_first_step = True
        self._termination_follow_deadline = self._now + 10.0
        self.termination_evidence = dict(procedure=dict(t), steps=[], shutdown_observed=False,
            test_start_method=self.bench.capabilities.test_start_method, restart_observed=False,
            control_hold_required_seconds=0, control_hold_completed=True,
            adaptive_steps=False, nominal_step_seconds=1.0,
            off_observation_required_seconds=10.0, firmware_termination_delay_verified=False)
        self._active_stage = dict(name="End current / observed shutdown", phase=self.phase, before=None,
            after=None, unit="A", status="RUNNING", started_at=_utc(), duration_seconds=0.0, _started=self._now)
        self.stage_details.append(self._active_stage)
        # Use one fresh independently measured conducting 5 A readback, then
        # issue 4.9 A immediately. Never add an 8-sample/3-second hold here.
        reading = self._sample()
        self._assert_passive()
        if reading is not None:
            self._tick_termination(reading)

    def _tick_termination(self, reading):
        t = self._test_termination
        self._validate_load_state(reading, "CC", self._termination_setpoint)
        self.measured, self.error = reading.current, reading.current-self.target
        low = reading.voltage <= t["off_voltage"] and reading.current <= t["off_current"]
        if low:
            if not self._termination_control_complete:
                self._termination_result("INDETERMINATE",
                    "Output stopped before the above-cutoff control hold completed; this may be a startup timeout, not a current cutoff.")
                return
            if self._termination_last_on is None:
                self._termination_result("FAIL", "Output was already off before a stable conducting-current reference was established.")
                return
            if self._termination_first_off is None:
                self._termination_first_off = dict(asdict(reading), commanded_current_a=self._termination_setpoint)
                self.stable_count = 0
            self.stable_count += 1
            span = reading.timestamp-self._termination_first_off["timestamp"]
            required_off = max(t["confirm_seconds"], self._cutoff_observation_seconds)
            self.detail = f"Output shutdown observed · watching for restart {span:.1f} / {required_off:g} s; load remains ON."
            if span >= required_off and self.stable_count >= 3:
                self.termination_evidence.update(shutdown_observed=True,
                                                pre_off=self._termination_last_on,
                                                first_off=self._termination_first_off,
                                                confirmed_seconds=span)
                upper = self._termination_last_on["measured_current_a"]
                lower = self._termination_first_off["commanded_current_a"]
                if lower > upper:
                    self._termination_result("INDETERMINATE", "Observed conducting current and first-off load step do not form a valid cutoff bracket.")
                    return
                estimate = (lower+upper)/2
                accepted_low, accepted_high = self.target-self.tolerance, self.target+self.tolerance
                status = "PASS" if lower >= accepted_low-1e-9 and upper <= accepted_high+1e-9 else (
                    "FAIL" if upper < accepted_low-1e-9 or lower > accepted_high+1e-9 else "INDETERMINATE")
                self.termination_evidence.update(observed_interval_a=[lower,upper], observed_estimate_a=estimate,
                                                pre_off_measured_current_a=upper,
                                                first_off_load_setpoint_a=lower,
                                                first_off_measured_current_a=self._termination_first_off["current"])
                self._termination_result(status,
                    f"Observed cutoff bracket {lower:.3f}–{upper:.3f} A; required {accepted_low:.3f}–{accepted_high:.3f} A. "
                    + ("Bracket is outside the required range. " if status == "FAIL" else
                       "Bracket overlaps the limit; it is not fully within the required range. " if status == "INDETERMINATE" else "")
                    + "Bracket uses last independently measured conducting current and first-off load demand. Both voltage and current stayed low through the restart observation; zero after shutdown is not the cutoff value.", estimate)
            return
        if self._termination_first_off is not None:
            self.termination_evidence.update(restart_observed=True,
                first_off=self._termination_first_off, pre_off=self._termination_last_on,
                restart=asdict(reading), shutdown_observed=False)
            self._termination_result("FAIL", "Output restarted or oscillated after the first V/I drop while the load remained ON. Relay chatter is not a verified end-current cutoff.")
            return
        if reading.timestamp < self._termination_step_started:
            return
        follows = (not reading.unregulated and reading.voltage > t["off_voltage"]
            and reading.current > t["off_current"]
            and abs(reading.current-self._termination_setpoint) <= self.recipe.current_tolerance)
        if not follows:
            self.detail = f"Waiting for actual current to follow {self._termination_setpoint:g} A; measured {reading.current:.3f} A / {reading.voltage:.2f} V."
            if self._now >= self._termination_follow_deadline:
                self._termination_result("FAIL", "CC demand was not followed within 10 seconds; no confirmed dual voltage/current cutoff was observed.")
            return
        self._termination_last_on = dict(measured_current_a=reading.current, voltage=reading.voltage,
            commanded_current_a=self._termination_setpoint, timestamp=reading.timestamp, sequence=reading.sequence)
        if self._active_stage["before"] is None:
            self._active_stage["before"] = reading.current
        if self._termination_setpoint <= .5 + 1e-9:
            if self._termination_floor_started is None:
                self._termination_floor_started = reading.timestamp
            held = reading.timestamp - self._termination_floor_started
            self.detail = f"At 0.5 A floor; observing cutoff delay {held:.1f} / 3 s without changing the load."
            if held >= 3.0:
                self.termination_evidence["steps"].append(dict(self._termination_last_on))
                self._termination_result("FAIL", "Reached 0.5 A and observed for 3 seconds without actual cutoff; no lower demand or automatic restart was attempted.")
            return
        if not self._termination_first_step and self._now-self._termination_step_started < 1.0:
            return
        self.termination_evidence["steps"].append(dict(self._termination_last_on))
        next_current = max(.5, round(self._termination_setpoint-.1, 10))
        self._require(self.bench.set_load_current(next_current), "Continuous-input CC step was not confirmed.")
        self._assert_passive()
        if not self.simulation:
            self._now = max(self._now, time.monotonic())
        self._termination_setpoint = next_current
        self._termination_step_started = self._now
        self._termination_follow_deadline = self._now + 10.0
        self._termination_first_step = False
        self.stable_count = 0
        self.detail = f"Load demand {next_current:.1f} A; fixed 0.1 A steps, watching actual voltage/current for cutoff."
        self._event(f"End-current demand {next_current:.1f} A; input remains ON.")

    def _termination_result(self, status, detail, estimate=None):
        if self._calibration_test:
            detail += " Calibration-mode observation only; repeatability and normal-mode equivalence have not been established."
        self.measured = estimate
        self.error = None if estimate is None else estimate-self.target
        self._result("End current / observed shutdown", estimate, status, detail)
        self.results[-1].update(copy.deepcopy(self.termination_evidence))
        self.results[-1]["final_load_setpoint_a"] = self._termination_setpoint
        self.results[-1]["sample_count"] = len([s for s in self.samples if s["phase"] == "test_termination"])
        self._finish_test()

    def _stop_outputs(self):
        if not self._owned:
            self.stop_outcome = "Bench acquisition failed; no stop command sent without ownership. Physical output is unconfirmed."
            self._event(self.stop_outcome)
            return
        outcomes = []
        if self.operation == "calibration":
            accepted = False
            try:
                accepted = self.bench.charger_shutdown() is True
                outcomes.append("Shutdown API accepted; physical output unconfirmed" if accepted else "Shutdown API not accepted")
            except Exception as exc:
                outcomes.append("Shutdown failed: " + str(exc))
            # Keep the load connected for discharge unless a fresh independent
            # reading proves output has dropped. Do not call this a safe state.
            try:
                reading = self._sample(shutdown_confirmation=accepted)
                if reading and reading.voltage <= self.settings.shutdown_voltage and reading.current <= self.settings.shutdown_current:
                    outcomes.append("Load OFF confirmed" if self.bench.load_off() is True else "Load OFF unconfirmed")
                else:
                    outcomes.append("Load retained; check physical output")
            except Exception:
                outcomes.append("Load retained; independent output confirmation unavailable")
        else:
            self._cleanup_test()
            return
        self.stop_outcome = "; ".join(outcomes)
        self._event(self.stop_outcome)

    def _fail(self, reason):
        self._event("ERROR: " + reason)
        self._baseline_failed(reason)
        if self.phase == "test_termination":
            self._result("End current / observed shutdown", None, "ERROR", reason)
            self.results[-1].update(copy.deepcopy(self.termination_evidence))
            self.results[-1]["final_load_setpoint_a"] = self._termination_setpoint
        elif (self.operation == "test" and self.phase in ("test_ocv", "test_current")
                and self._active_stage is not None and self._active_stage.get("status") == "RUNNING"):
            if self.reading is not None:
                reason += f" Last valid load reading: {self.reading.voltage:.3f} V / {self.reading.current:.3f} A; no final plateau accepted."
            self._result("Open-circuit voltage" if self.phase == "test_ocv" else "Full current", None, "ERROR", reason)
        self._stop_outputs()
        self._finish("ERROR", "Process stopped", reason)

    def stop(self, reason="Stopped by operator"):
        if not self.is_running:
            return
        self._baseline_failed(reason)
        if self.phase == "test_termination":
            self._result("End current / observed shutdown", None, "ABORTED", reason)
            self.results[-1].update(copy.deepcopy(self.termination_evidence))
            self.results[-1]["final_load_setpoint_a"] = self._termination_setpoint
        self._stop_outputs()
        self._finish("ABORTED", "Process stopped", reason)

    def _baseline_failed(self, reason):
        if self.baseline_result.get("status") != "RUNNING":
            return
        self.baseline_result.update(status="FAIL", detail=reason)
        self._baseline_deadline = None
        for point in ("open_voltage", "current"):
            record = self.baseline_result[point]
            if not record:
                self.baseline_result[point] = dict(measured=None, status="NOT RUN",
                    unit="V" if point == "open_voltage" else "A", duration_seconds=0.0,
                    started_at=None, finished_at=None,
                    load_mode="OFF" if point == "open_voltage" else "CV",
                    load_setpoint=None if point == "open_voltage" else self.recipe.test_cv_voltage,
                    detail="Unit stopped before this baseline measurement.")
            elif record.get("status") == "RUNNING":
                record.update(status="FAIL", detail=reason,
                    finished_at=_utc(),
                    duration_seconds=round(max(0, self._now-self._phase_started), 3))

    def _finish(self, outcome, state, detail):
        if self.operation == "test" and self._owned and not self._cleanup_test():
            if outcome != "ABORTED":
                outcome = "ERROR"
            detail += " Cleanup/restoration unconfirmed: " + self._test_cleanup_error
        if self._active_stage is not None and self._active_stage.get("status") == "RUNNING":
            self._active_stage.update(status=outcome, after=self.measured,
                duration_seconds=round(max(0, self._now-self._active_stage["_started"]), 3), finished_at=_utc(), detail=detail)
        self.preflight_result = copy.deepcopy(getattr(self.bench, "preflight_result", self.preflight_result))
        self.is_running = False
        self.finished_at = _utc()
        self.phase = "complete"
        self.state, self.detail = state, detail
        self.outcome = "SIMULATION — " + outcome if self.simulation else outcome
        self.progress = 100 if outcome not in ("ERROR", "ABORTED") else self.progress
        self._event(self.outcome + ": " + detail)
        if self._owned:
            try:
                self.bench.release()
            except Exception as exc:
                self._event("Bench release failed: " + str(exc))
                self.outcome = "SIMULATION — ERROR" if self.simulation else "ERROR"
            self._owned = False
        if self.operation == "test" and self._measurement_started and self.bench.can_tx_count != self._tx_start:
            self.outcome = "SIMULATION — ERROR" if self.simulation else "ERROR"
            self.detail = "CAN transmission occurred during standalone testing; the test is invalid."
            self._event(self.detail)
        if self.report_dir is not None:
            try:
                self.write_reports(self.report_dir)
            except Exception as exc:
                self.report_error = "Could not save report: " + str(exc)
                self._event(self.report_error)
                self.detail += " " + self.report_error

    def snapshot(self):
        return dict(schema="leanwatts-automation/1", run_id=self.run_id, unit_serial=self.unit_serial,
                    operator=self.operator, recipe=self.recipe.to_dict(), recipe_revision=self.recipe_revision,
                    simulation=self.simulation, mode="SIMULATION ONLY" if self.simulation else "REAL INSTRUMENTS",
                    operation=self.operation, is_running=self.is_running, state=self.state, detail=self.detail,
                    baseline_result=copy.deepcopy(self.baseline_result),
                    startup_voltage_result=copy.deepcopy(self.startup_voltage_result),
                    external_restart_evidence=copy.deepcopy(self.external_restart_evidence),
                    load_transition_events=copy.deepcopy(getattr(self.bench, "load_transition_events", [])),
                    phase=self.phase, outcome=self.outcome, started_at=self.started_at, finished_at=self.finished_at,
                    elapsed_seconds=max(0, round(self._now-self._started, 3)), progress=self.progress,
                    target=self.target, measured=self.measured, error=self.error, tolerance=self.tolerance, unit=self.unit,
                    stable_count=self.stable_count, stable_required=self.stable_required,
                    stage_details=[{key: (round(max(0, self._now-item["_started"]), 3)
                                          if key == "duration_seconds" and item.get("status") == "RUNNING" else value)
                                    for key, value in item.items() if not key.startswith("_")}
                                   for item in self.stage_details],
                    preflight_result=copy.deepcopy(getattr(self.bench, "preflight_result", self.preflight_result)),
                    test_method=self.test_method,
                    test_start_method=self.bench.capabilities.test_start_method,
                    test_program=copy.deepcopy(self.test_program),
                    diagnostic_only=self._calibration_test,
                    normal_operation_verified=bool(self.operation == "test" and not self.simulation
                        and self._test_cleanup_ok and len(self.results) == 3
                        and all(item["status"] == "PASS" for item in self.results)),
                    startup_can_tx_count=self.startup_can_tx_count,
                    test_setup_can_tx_count=getattr(self.bench, "test_setup_can_tx_count", 0),
                    cleanup_can_tx_count=getattr(self.bench, "cleanup_can_tx_count", 0),
                    test_parameter_restore_result=copy.deepcopy(getattr(self.bench, "test_parameter_restore_result", {})),
                    test_cleanup_verified=self._test_cleanup_done and self._test_cleanup_ok,
                    planned_start_count=self.planned_start_count,
                    startup_events=copy.deepcopy(getattr(self.bench, "startup_events", [])),
                    total_can_tx_count=self.bench.can_tx_count-self._run_tx_start,
                    stability_purpose=self.stability_purpose,
                    tuning_mode=self.tuning_mode,
                    output_step_requested=(getattr(self.settings, self.phase[4:] + "_output_step")
                                           if self.phase in ("cal_current", "cal_voltage") else None),
                    output_per_duty=dict(self.output_per_duty), output_step_estimated=self.output_step_estimated,
                    voltage_duty=self.voltage_duty, current_duty=self.current_duty,
                    trim_directions=dict(self.trim_directions),
                    direction_detection=copy.deepcopy(self.direction_detection),
                    reading=asdict(self.reading) if self.reading else None,
                    results=copy.deepcopy(self.results), events=list(self.events),
                    report_paths=dict(self.report_paths), report_error=self.report_error,
                    readiness_reason=self.readiness_reason,
                    reference_identity=self.bench.capabilities.reference_identity,
                    charger_observations=copy.deepcopy(getattr(self.bench,"can_observations",{})),
                    charger_observed_at=copy.deepcopy(getattr(self.bench,"_can_observed_at",{})),
                    can_tx_count=(self.bench.can_tx_count-self._run_tx_start-self.startup_can_tx_count
                                  -getattr(self.bench, "test_setup_can_tx_count", 0)-getattr(self.bench, "cleanup_can_tx_count", 0)
                                  if self.operation == "test" and self.bench.capabilities.post_test_can_start
                                  else self.bench.can_tx_count-self._tx_start),
                    programmed_values=self.recipe.charger_values(),
                    termination_evidence=copy.deepcopy(self.termination_evidence),
                    persistence_verified=False,
                    end_current_shutdown_observed=bool(self.termination_evidence.get("shutdown_observed")),
                    end_current_termination_verified=bool(not self.simulation and self._test_cleanup_ok and self.termination_evidence.get("shutdown_observed")
                        and any(item["name"] == "End current / observed shutdown" and item["status"] == "PASS" for item in self.results)),
                    stop_outcome=self.stop_outcome)

    def write_reports(self, folder: Path):
        if not self.run_id or self.is_running:
            raise ValueError("Finish or stop a run before writing its report.")
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        paths = {suffix: folder / (self.run_id + "." + suffix) for suffix in ("json", "csv", "html")}
        completed_paths = {key: str(path.resolve()) for key, path in paths.items()}
        staged = {key: path.with_suffix(path.suffix + ".tmp") for key, path in paths.items()}
        self.report_paths = {}
        record = self.snapshot()
        # JSON is the report-set commit record. Publish it last, and expose no
        # successful paths on the engine until every companion file exists.
        record["report_paths"] = completed_paths
        record["report_error"] = ""
        record["samples"] = copy.deepcopy(self.samples)
        record["automation_settings"] = asdict(self.settings)
        trace = getattr(self.bench,"can_trace",[])[self._trace_start:]
        record["hardware_evidence"] = dict(
            capabilities=asdict(self.bench.capabilities),
            can_trace=copy.deepcopy(trace[-10000:]), can_trace_total=len(trace),
            can_trace_truncated=len(trace)>10000,
            last_charger_observations=copy.deepcopy(getattr(self.bench,"can_observations",{})),
            charger_observed_at=copy.deepcopy(getattr(self.bench,"_can_observed_at",{})),
            load_probe=copy.deepcopy(getattr(self.bench,"probe_result",None)),
            commissioning={key:copy.deepcopy(getattr(self.bench,key,None)) for key in (
                "protocol_approved","cc_mode_code","cv_mode_code","standalone_ready",
                "exclusive_confirmed","termination_ramp_approved")})
        staged["json"].write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        with staged["csv"].open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            def row(values):
                # Operator/serial/instrument fields are text, never spreadsheet formulas.
                writer.writerow([("'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value)
                                 for value in values])
            row(["Mode", record["mode"], "Outcome", self.outcome])
            row(["Run", self.run_id, "Serial", self.unit_serial, "Operator", self.operator])
            row(["Variant", self.recipe.name, "Revision", self.recipe_revision])
            row(["Test method", self.test_method, "Normal-mode cutoff verified", record["end_current_termination_verified"]])
            row(["Name", "Required", "Programmed", "Observed", "Error", "Tolerance", "Unit", "Status", "Note", "Cutoff lower A", "Cutoff upper A"])
            for result in self.results:
                interval = result.get("observed_interval_a", [None,None])
                row([result.get(key) for key in ("name", "target", "programmed", "measured", "error", "tolerance", "unit", "status", "detail")] + interval)
            row([])
            keys = ("elapsed_seconds", "phase", "voltage", "current", "power", "derived_power_w", "source", "sequence", "load_mode", "load_setpoint", "load_on", "current_duty", "voltage_duty", "fault")
            row(keys)
            for sample in self.samples:
                row([sample.get(key) for key in keys])
        escape = lambda value: html.escape(str(value), quote=True)
        rows = "".join("<tr>" + "".join("<td>" + escape(item.get(key, "—") if item.get(key) is not None else "—") + "</td>"
                                                for key in ("name", "target", "programmed", "measured", "error", "tolerance", "unit", "status")) + "</tr>" for item in self.results)
        result_details = "".join("<p><strong>" + escape(item["name"]) + ":</strong> " + escape(item.get("detail", ""))
                                 + (" Cutoff bracket: " + escape(item["observed_interval_a"]) + " A." if "observed_interval_a" in item else "")
                                 + (" Voltage range: " + escape([item["voltage_min"], item["voltage_max"]]) + " V; current range: "
                                    + escape([item["current_min"], item["current_max"]]) + " A; derived power range: "
                                    + escape([item["derived_power_min_w"], item["derived_power_max_w"]]) + " W." if "derived_power_min_w" in item else "")
                                 + "</p>" for item in self.results)
        termination_note = "Output shutdown observed; cutoff inferred from a measured-current/load-step bracket." if self.termination_evidence.get("shutdown_observed") else "End-current shutdown has not been confirmed."
        mode = "SIMULATION ONLY — not a physical test report" if self.simulation else "Physical instrument run"
        document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><title>Charger report {escape(self.run_id)}</title>
<style>body{{font:15px system-ui,sans-serif;color:#18151e;max-width:1050px;margin:40px auto;padding:0 24px}}h1{{font-size:28px}}.banner{{padding:14px;background:#fff0cc;border:1px solid #e2ba55}}table{{border-collapse:collapse;width:100%;margin:22px 0}}td,th{{border-bottom:1px solid #ccc;text-align:left;padding:9px}}small{{color:#59515f}}@media print{{body{{margin:10mm}}}}</style>
<h1>{'Calibration record' if self.operation == 'calibration' else 'Calibration-mode diagnostic report' if self._calibration_test else 'Standalone test report'}</h1>
<p class="banner"><strong>{escape(mode)}</strong><br>{escape(self.outcome)}</p>
<p>{escape(self.test_method) if self.operation == 'test' else ''}</p>
<p>Serial: <strong>{escape(self.unit_serial)}</strong> · Operator: {escape(self.operator)}<br>
Variant: {escape(self.recipe.name)} · Revision {self.recipe_revision}<br>Run: {escape(self.run_id)}<br>
Started: {escape(self.started_at)} · Finished: {escape(self.finished_at)}<br>Reference: {escape(self.bench.capabilities.reference_identity)}</p>
<table><thead><tr><th>Check</th><th>Required</th><th>Programmed*</th><th>Observed</th><th>Error</th><th>± tolerance</th><th>Unit</th><th>Result</th></tr></thead><tbody>{rows}</tbody></table>
<p><small>*Programmed values are the saved recipe's intended charger settings, not independently confirmed firmware readbacks. Normal output-voltage testing does not test the overvoltage-protection trip point.</small></p>{result_details}
<p>{escape(self.detail)}</p><p>{escape(self.stop_outcome)}</p>
<p>CAN transmissions during run: {record['can_tx_count']}. Calibration memory persistence: unverified. {escape(termination_note)}</p>
<p><small>JSON and CSV companion files contain the sampled measurements and run evidence. An accepted save request alone does not establish storage after a power cycle.</small></p></html>"""
        staged["html"].write_text(document, encoding="utf-8")
        for key in ("csv", "html", "json"):
            os.replace(staged[key], paths[key])
        self.report_paths = completed_paths
        self.report_error = ""
        return dict(self.report_paths)
