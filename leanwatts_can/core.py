"""Deterministic calibration workflow simulator. This module performs no hardware I/O.

Trim values below are engineering-unit model corrections, not recovered CAN
register values. A completed run is never a physical calibration certificate.
"""

from __future__ import annotations

import copy
import csv
import math
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEVICE_SETTING_SCALES = {
    "batteryType": 1,
    "ratedVoltage": 10,
    "startVoltage": 10,
    "temperatureLimit": 1,
    "maxChargeMinutes": 1,
    "derating110": 1,
    "derating220": 1,
    "blindCharge": 1,
    "rechargeVoltage": 10,
    "adjustCurrentMin": 10,
    "adjustCurrentMax": 10,
    "adjustVoltageMin": 10,
    "adjustVoltageMax": 10,
}


def _finite(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Recipe:
    name: str = "New variant"
    cc_current: float = 0.0
    end_voltage: float = 0.0
    end_current: float = 0.0
    current_tolerance: float = 0.0
    voltage_tolerance: float = 0.0
    end_current_tolerance: float = 0.0
    current_cal_load_voltage: float = 48.0
    voltage_cal_load_current: float = 1.0
    max_voltage: float = 0.0
    max_current: float = 0.0
    max_power: float = 0.0
    device_settings: dict[str, float] = field(default_factory=dict)
    # Required physical acceptance targets above are independent of values sent
    # to charger firmware. None preserves the previous manual-send mapping.
    programmed_cc_current: float | None = None
    programmed_end_voltage: float | None = None
    programmed_end_current: float | None = None
    cc_test_load_voltage: float | None = None
    voltage_test_load_current: float | None = None
    termination_test: dict[str, Any] = field(default_factory=lambda: {"enabled": False})
    test_plan: dict[str, Any] = field(default_factory=dict)

    def charger_values(self) -> dict[str, float]:
        return {
            "ccCurrent": self.cc_current if self.programmed_cc_current is None else self.programmed_cc_current,
            "endVoltage": self.end_voltage if self.programmed_end_voltage is None else self.programmed_end_voltage,
            "endCurrent": self.end_current if self.programmed_end_current is None else self.programmed_end_current,
        }

    @property
    def test_cv_voltage(self) -> float:
        return self.current_cal_load_voltage if self.cc_test_load_voltage is None else self.cc_test_load_voltage

    @property
    def test_cc_current(self) -> float:
        return self.voltage_cal_load_current if self.voltage_test_load_current is None else self.voltage_test_load_current

    def validate_calibration(self) -> str:
        """Only settings used by current/voltage calibration; no test-plan gate."""
        if not isinstance(self.name, str) or not self.name.strip():
            return "Enter a variant name."
        fields = (self.cc_current, self.end_voltage, self.current_tolerance,
                  self.voltage_tolerance, self.current_cal_load_voltage,
                  self.voltage_cal_load_current, self.max_voltage,
                  self.max_current, self.max_power)
        if any(not _finite(value) or value <= 0 for value in fields):
            return "Enter positive current/voltage targets, tolerances, load settings and bench limits."
        if self.current_cal_load_voltage >= self.end_voltage:
            return "Current-calibration load voltage must be below the required output voltage."
        if self.voltage_cal_load_current >= self.cc_current:
            return "Voltage-calibration load current must be below the required full current."
        if self.current_tolerance >= self.cc_current or self.voltage_tolerance >= self.end_voltage:
            return "Calibration tolerances must be smaller than their targets."
        if self.end_voltage + self.voltage_tolerance > self.max_voltage:
            return "Voltage target and tolerance exceed the bench voltage limit."
        if self.cc_current + self.current_tolerance > self.max_current:
            return "Current target and tolerance exceed the bench current limit."
        if (self.end_voltage + self.voltage_tolerance) * (self.cc_current + self.current_tolerance) > self.max_power:
            return "The possible charger output exceeds the bench power limit."
        return ""

    def validate(self) -> str:
        if not isinstance(self.name, str) or not self.name.strip():
            return "Enter a variant name."
        values = (
            self.cc_current, self.end_voltage, self.end_current,
            self.current_tolerance, self.voltage_tolerance, self.end_current_tolerance,
            self.current_cal_load_voltage, self.voltage_cal_load_current,
            self.max_voltage, self.max_current, self.max_power,
        )
        if any(not _finite(value) or value <= 0 for value in values):
            return "All targets, tolerances and limits must be finite numbers greater than zero."
        if self.end_current >= self.cc_current:
            return "End current must be lower than CC current."
        if self.current_cal_load_voltage >= self.end_voltage:
            return "Current-calibration load voltage must be below end voltage; 48 V is not suitable for every variant."
        if self.voltage_cal_load_current >= self.cc_current:
            return "Voltage-calibration load current must be below CC current."
        if (self.current_tolerance >= self.cc_current or self.voltage_tolerance >= self.end_voltage
                or self.end_current_tolerance >= self.end_current):
            return "Each tolerance must be smaller than its target."
        if self.end_voltage + self.voltage_tolerance > self.max_voltage:
            return "End-voltage target and tolerance exceed the voltage limit."
        if self.cc_current + self.current_tolerance > self.max_current:
            return "CC-current target and tolerance exceed the current limit."
        if (self.end_voltage + self.voltage_tolerance) * (self.cc_current + self.current_tolerance) > self.max_power:
            return "The possible charger output exceeds the entered power limit."
        for key, value in (("ccCurrent", self.programmed_cc_current), ("endVoltage", self.programmed_end_voltage),
                           ("endCurrent", self.programmed_end_current)):
            if value is None:
                continue
            if not _finite(value) or value <= 0 or value > 3276.7:
                return f"Programmed {key} must be finite, positive and within the CAN range."
            if abs(value * 10 - round(value * 10)) > 1e-7:
                return f"Programmed {key} must use 0.1 increments."
        programmed = self.charger_values()
        if programmed["endCurrent"] >= programmed["ccCurrent"]:
            return "Programmed end current must be below programmed CC current."
        if programmed["endVoltage"] > self.max_voltage or programmed["ccCurrent"] > self.max_current:
            return "Programmed voltage/current must remain within the bench limits."
        if programmed["endVoltage"] * programmed["ccCurrent"] > self.max_power:
            return "Programmed voltage × current exceeds the bench power limit."
        if not _finite(self.test_cv_voltage) or not 0 < self.test_cv_voltage < self.end_voltage:
            return "Full-current test load voltage must be positive and below required end voltage."
        if not _finite(self.test_cc_current) or not 0 < self.test_cc_current < self.cc_current:
            return "Normal-voltage test load current must be positive and below required CC current."
        if self.termination_test is not None:
            if not isinstance(self.termination_test, dict):
                return "End-current test settings must be a named mapping."
            allowed = {"enabled", "start_current", "minimum_current", "step_current", "dwell_seconds",
                       "off_voltage", "off_current", "confirm_seconds", "max_seconds"}
            if set(self.termination_test) - allowed:
                return "Unknown end-current test setting."
            if type(self.termination_test.get("enabled", False)) is not bool:
                return "End-current test enabled must be true or false."
            enabled = self.termination_test.get("enabled", False)
            for name in allowed - {"enabled"}:
                value = self.termination_test.get(name)
                if (enabled or value is not None) and (not _finite(value) or value <= 0):
                    return f"End-current test {name} must be finite and greater than zero."
            if enabled:
                t = self.termination_test
                if not t["minimum_current"] < self.end_current - self.end_current_tolerance < self.end_current + self.end_current_tolerance < t["start_current"]:
                    return "End-current ramp must start above and finish below the required cutoff tolerance band."
                if t["start_current"] >= self.cc_current or t["start_current"] > self.max_current:
                    return "End-current ramp start must be below CC current and within bench current limit."
                if t["start_current"] * (self.end_voltage + self.voltage_tolerance) > self.max_power:
                    return "End-current ramp start exceeds the bench power limit."
                if t["step_current"] > t["start_current"] - t["minimum_current"]:
                    return "End-current step is larger than the ramp range."
                if t["off_current"] >= t["minimum_current"] or t["off_voltage"] >= self.end_voltage-self.voltage_tolerance:
                    return "Shutdown thresholds must be below conducting current and voltage."
                if t["max_seconds"] <= t["dwell_seconds"] + t["confirm_seconds"]:
                    return "End-current test timeout must allow a dwell and shutdown confirmation."
        if self.test_plan is not None:
            if not isinstance(self.test_plan, dict):
                return "Optional DC-load tests must be a named mapping."
            if set(self.test_plan) - {"voltage_regulation_currents", "current_regulation_voltages", "soak_seconds", "soak_current"}:
                return "Unknown optional DC-load test setting."
            for key, limit in (("voltage_regulation_currents", self.cc_current), ("current_regulation_voltages", self.end_voltage)):
                points = self.test_plan.get(key, [])
                if not isinstance(points, list) or len(points) > 50:
                    return "Use a list of at most 50 points for each regulation test."
                if any(not _finite(value) or not 0 < value < limit for value in points):
                    return f"Every {key} point must be positive and below its output limit."
            seconds = self.test_plan.get("soak_seconds", 0)
            if not _finite(seconds) or not 0 <= seconds <= 3600:
                return "Sustained-load duration must be between 0 and 3600 seconds."
            if seconds:
                current = self.test_plan.get("soak_current")
                if not _finite(current) or not 0 < current < self.cc_current:
                    return "Sustained-load current must be positive and below CC current."
            elif "soak_current" in self.test_plan and (not _finite(self.test_plan["soak_current"]) or self.test_plan["soak_current"] <= 0):
                return "Sustained-load current must be finite and positive when provided."
        if self.device_settings is None:
            return ""
        if not isinstance(self.device_settings, dict):
            return "Optional device settings must be a named mapping."
        for key, value in self.device_settings.items():
            if key not in DEVICE_SETTING_SCALES:
                return f"Unknown device setting: {key}."
            scale = DEVICE_SETTING_SCALES[key]
            maximum = 255 if key == "blindCharge" else 32767
            if not _finite(value) or value < 0 or value * scale > maximum:
                return f"Device setting {key} must be between 0 and {maximum / scale:g}."
            if abs(value * scale - round(value * scale)) > 1e-7:
                return f"Device setting {key} must use increments of {1 / scale:g}."
        for stem, label in (("adjustCurrent", "current"), ("adjustVoltage", "voltage")):
            if stem + "Min" in self.device_settings and stem + "Max" in self.device_settings:
                if self.device_settings[stem + "Min"] > self.device_settings[stem + "Max"]:
                    return f"Adjustable-{label} minimum must not exceed its maximum."
        return ""

    def validate_production(self) -> str:
        """Complete per-charger setup, calibration and the selected post-tests."""
        error = self.validate()
        if error:
            return error
        from .protocol import encode_all_config
        values = dict(self.device_settings or {})
        values.update(self.charger_values())
        try:
            encode_all_config(values)
        except ValueError as exc:
            return "Complete all charger parameter values for the initial check: " + str(exc)
        if not self.termination_test.get("enabled"):
            return "Enable the end-current cutoff test for the production sequence."
        if self.test_cc_current <= self.end_current + self.end_current_tolerance:
            return "Voltage-test load current must exceed the required cutoff current plus its tolerance."
        return ""

    def validate_parameter_calibration(self) -> str:
        """Validate parameter programming and calibration without a post-test gate."""
        error = self.validate_calibration()
        if error:
            return error
        if self.device_settings is not None and not isinstance(self.device_settings, dict):
            return "Optional device settings must be a named mapping."
        from .protocol import encode_all_config
        values = dict(self.device_settings or {})
        values.update(self.charger_values())
        try:
            encode_all_config(values)
        except ValueError as exc:
            return "Complete all charger parameter values for the initial check: " + str(exc)
        programmed = self.charger_values()
        if any(value <= 0 for value in programmed.values()):
            return "Programmed current, voltage and end current must be positive."
        if programmed["endCurrent"] >= programmed["ccCurrent"]:
            return "Programmed end current must be below programmed CC current."
        if programmed["endVoltage"] > self.max_voltage or programmed["ccCurrent"] > self.max_current:
            return "Programmed voltage/current must remain within the bench limits."
        if programmed["endVoltage"] * programmed["ccCurrent"] > self.max_power:
            return "Programmed voltage × current exceeds the bench power limit."
        for stem, label in (("adjustCurrent", "current"), ("adjustVoltage", "voltage")):
            if values[stem + "Min"] > values[stem + "Max"]:
                return f"Adjustable-{label} minimum must not exceed its maximum."
        return ""

    def copy(self) -> Recipe:
        result = copy.deepcopy(self)
        if result.device_settings is None:
            result.device_settings = {}
        if result.termination_test is None:
            result.termination_test = {"enabled": False}
        if result.test_plan is None:
            result.test_plan = {}
        return result

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.copy())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Recipe:
        if not isinstance(data, dict):
            raise ValueError("A variant profile must be a JSON object.")
        names = {item.name for item in fields(cls)}
        recipe = cls(**{key: copy.deepcopy(value) for key, value in data.items() if key in names})
        if recipe.device_settings is None:
            recipe.device_settings = {}
        if recipe.termination_test is None:
            recipe.termination_test = {"enabled": False}
        if recipe.test_plan is None:
            recipe.test_plan = {}
        return recipe


@dataclass
class Reading:
    voltage: float = 0.0
    current: float = 0.0
    power: float = 0.0
    load_mode: str = "OFF"
    load_setpoint: float = 0.0
    charger_on: bool = False
    load_on: bool = False


@dataclass
class CheckResult:
    name: str
    target: float
    measured: float
    tolerance: float
    unit: str
    status: str


class CalibrationEngine:
    """250 ms tick-driven model supporting auto and guided manual calibration."""

    def __init__(self, recipe: Recipe, unit_serial: str = "", operator: str = ""):
        if not isinstance(recipe, Recipe):
            raise ValueError("Select a variant profile.")
        error = recipe.validate()
        if error:
            raise ValueError(error)
        self._recipe = recipe.copy()
        self.unit_serial = str(unit_serial)
        self.operator = str(operator)
        self.reading = Reading()
        self.state = "Ready"
        self.phase_detail = "Simulation only. No CAN or load-bank connection is made."
        self.mode = "Simulation"
        self.is_running = False
        self.current_saved = False
        self.voltage_saved = False
        self.current_trim = 0.0
        self.voltage_trim = 0.0
        self.log: list[str] = []
        self.checks: list[CheckResult] = []
        self.run_id = "NOT STARTED"
        self.started_at = ""
        self.finished_at = ""
        self._stage = "idle"
        self._automatic = False
        self._ticks_in_stage = 0
        self._total_ticks = 0
        self._stable_ticks = 0
        self._previous_measurement = math.nan
        self._last_tick = 0.0
        self._run_started = 0.0
        self._add_log("Simulation model created. No physical charger or load bank is controlled.")

    @property
    def recipe(self) -> Recipe:
        return self._recipe.copy()

    @property
    def can_save_current(self) -> bool:
        return self._stage == "current" and self._stable_ticks >= 4 and self._within_current()

    @property
    def can_save_voltage(self) -> bool:
        return self._stage == "voltage" and self._stable_ticks >= 4 and self._within_voltage()

    def _add_log(self, message: str) -> None:
        self.log.append(datetime.now().strftime("%H:%M:%S") + "  " + message)

    def _set_stage(self, stage: str, title: str, detail: str) -> None:
        self._stage = stage
        self._ticks_in_stage = 0
        self._stable_ticks = 0
        self._previous_measurement = math.nan
        self.state = title
        self.phase_detail = detail
        self._add_log(title + ": " + detail)

    def _begin(self, automatic: bool, voltage_only: bool = False, cross_check: bool = False) -> None:
        if self.is_running:
            raise ValueError("Stop or finish the active operation first.")
        error = self._recipe.validate()
        if error:
            raise ValueError(error)
        self._automatic = automatic
        self._total_ticks = 0
        self._last_tick = self._run_started = time.monotonic()
        # Voltage and cross-check continue the same full calibration session.
        if not voltage_only and not cross_check:
            self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:8]
            self.started_at = _utc_now()
            self.log.clear()
            self._add_log("SIMULATION ONLY. No physical charger or load bank is controlled.")
        self.finished_at = ""
        self.checks.clear()
        self._safe_off()
        self.is_running = True
        self.mode = "Simulated cross-check" if cross_check else "Automatic simulation" if automatic else "Manual simulation"
        self._add_log(f"Begin {self.mode}; variant {self._recipe.name}; unit {self.unit_serial or '(not entered)'}. ")

    def start_auto(self) -> None:
        self._begin(True)
        self.current_saved = self.voltage_saved = False
        self.current_trim = self.voltage_trim = 0.0
        self._set_stage("connect", "Open device", "Simulate opening the USB-to-CAN adapter.")

    def start_manual_current(self) -> None:
        self._begin(False)
        self.current_saved = self.voltage_saved = False
        self.current_trim = self.voltage_trim = 0.0
        self._set_stage("connect", "Open device", "Simulate opening the USB-to-CAN adapter.")

    def start_manual_voltage(self) -> None:
        if not self.current_saved:
            raise ValueError("Complete and save current calibration first.")
        self._begin(False, voltage_only=True)
        self.voltage_saved = False
        self.voltage_trim = 0.0
        self._set_stage("load_off", "Switch load mode", "Charger and load are off before changing to CC mode.")

    def adjust_current(self, delta: float) -> None:
        if self._automatic or self._stage != "current":
            raise ValueError("Current trim is available only during manual current calibration.")
        if not _finite(delta):
            raise ValueError("Enter a finite adjustment.")
        candidate = self.current_trim + delta
        if abs(candidate) > max(1.0, self._recipe.cc_current * 0.15):
            raise ValueError("The simulated current trim limit has been reached.")
        self.current_trim = candidate
        self._stable_ticks = 0
        self._add_log(f"Current trim changed to {self.current_trim:.6g} A (simulated engineering-unit correction).")

    def adjust_voltage(self, delta: float) -> None:
        if self._automatic or self._stage != "voltage":
            raise ValueError("Voltage trim is available only during manual voltage calibration.")
        if not _finite(delta):
            raise ValueError("Enter a finite adjustment.")
        candidate = self.voltage_trim + delta
        if abs(candidate) > max(2.0, self._recipe.end_voltage * 0.15):
            raise ValueError("The simulated voltage trim limit has been reached.")
        self.voltage_trim = candidate
        self._stable_ticks = 0
        self._add_log(f"Voltage trim changed to {self.voltage_trim:.6g} V (simulated engineering-unit correction).")

    def save_current(self) -> None:
        if not self.can_save_current:
            raise ValueError("Current must remain stable and within tolerance for four samples before saving.")
        self.current_saved = True
        self._set_stage("save_current", "Save full-current calibration", "Simulated save only; no charger memory is written.")

    def save_voltage(self) -> None:
        if not self.can_save_voltage:
            raise ValueError("Voltage must remain stable and within tolerance for four samples before saving.")
        self.voltage_saved = True
        self._set_stage("save_voltage", "Save voltage calibration", "Simulated save only; no charger memory is written.")

    def start_cross_check(self) -> None:
        if not self.current_saved or not self.voltage_saved:
            raise ValueError("Save both current and voltage calibrations before cross-checking.")
        self._begin(False, cross_check=True)
        self._begin_check_cc()

    def abort(self) -> None:
        self._safe_off()
        self.is_running = False
        self.finished_at = _utc_now()
        self._set_stage("aborted", "Stopped", "Simulated charger output and load input are off.")

    def _safe_off(self) -> None:
        self.reading.charger_on = self.reading.load_on = False
        self.reading.voltage = self.reading.current = self.reading.power = 0.0
        self.reading.load_mode = "OFF"
        self.reading.load_setpoint = 0.0

    def _fail(self, error: str) -> None:
        self._safe_off()
        self.is_running = False
        self.finished_at = _utc_now()
        self._set_stage("fault", "Fault - stopped", error + " Simulated outputs are off.")

    def _within_current(self) -> bool:
        return abs(self.reading.current - self._recipe.cc_current) <= self._recipe.current_tolerance

    def _within_voltage(self) -> bool:
        return abs(self.reading.voltage - self._recipe.end_voltage) <= self._recipe.voltage_tolerance

    def _update_reading(self) -> None:
        if not self.reading.charger_on or not self.reading.load_on:
            self.reading.voltage = self.reading.current = self.reading.power = 0.0
            return
        if self._stage == "check_end":
            # Illustrative model, not a validated termination threshold test.
            target_voltage = self._recipe.end_voltage
            target_current = self._recipe.end_current
        elif self.reading.load_mode == "CV":
            target_voltage = self.reading.load_setpoint
            target_current = max(0.0, self._recipe.cc_current * 0.96 + self.current_trim)
        else:
            target_voltage = max(0.0, self._recipe.end_voltage * 0.97 + self.voltage_trim)
            target_current = self.reading.load_setpoint
        self.reading.voltage += (target_voltage - self.reading.voltage) * 0.75
        self.reading.current += (target_current - self.reading.current) * 0.75
        self.reading.power = self.reading.voltage * self.reading.current

    def _enforce_limits(self) -> bool:
        values = (self.reading.voltage, self.reading.current, self.reading.power)
        if not all(_finite(value) for value in values):
            self._fail("Non-finite simulated telemetry.")
            return False
        if (min(values) < 0 or self.reading.voltage > self._recipe.max_voltage
                or self.reading.current > self._recipe.max_current or self.reading.power > self._recipe.max_power):
            self._fail("A configured voltage, current or power limit was exceeded.")
            return False
        return True

    def _track_stability(self, measurement: float, target: float, tolerance: float) -> None:
        if (abs(measurement - target) <= tolerance and _finite(self._previous_measurement)
                and abs(measurement - self._previous_measurement) <= tolerance * 0.20):
            self._stable_ticks += 1
        else:
            self._stable_ticks = 0
        self._previous_measurement = measurement

    def tick(self) -> None:
        if not self.is_running:
            return
        now = time.monotonic()
        if now - self._last_tick > 3:
            self._fail("The simulation update loop stalled for more than three seconds.")
            return
        self._last_tick = now
        self._ticks_in_stage += 1
        self._total_ticks += 1
        if ((self._automatic and self._total_ticks > 240)
                or (not self._automatic and self._total_ticks > 3600) or now - self._run_started > 900):
            self._fail("Calibration timed out before completion.")
            return
        # Validate before and after modelling; an invalid prior sample cannot be hidden.
        if not self._enforce_limits():
            return
        self._update_reading()
        if not self._enforce_limits():
            return
        stage, ticks, p = self._stage, self._ticks_in_stage, self._recipe

        if stage == "connect" and ticks >= 2:
            self._set_stage("can_start", "Start CAN", "Simulated CAN connection ready.")
        elif stage == "can_start" and ticks >= 2:
            self.reading.load_mode = "CV"
            self.reading.load_setpoint = p.current_cal_load_voltage
            self.reading.load_on = True
            self._set_stage("load_cv", "Load bank ON - CV", f"Hold {p.current_cal_load_voltage:g} V for current calibration.")
        elif stage == "load_cv" and ticks >= 2:
            self.reading.charger_on = False
            self._set_stage("shutdown", "Shut down charger", "Simulated stop before writing the variant settings.")
        elif stage == "shutdown" and ticks >= 2:
            self._set_stage("send", "Send variant settings", "Simulate sending the saved profile to the charger.")
        elif stage == "send" and ticks >= 2:
            self._set_stage("readback", "Verify settings readback", "Simulated readback matches every entered setting.")
        elif stage == "readback" and ticks >= 3:
            self.reading.charger_on = True
            self._set_stage("start_current", "Start current calibration", "Wait for simulated output to settle.")
        elif stage == "start_current" and ticks >= 3:
            self._set_stage("current", "Auto current trim" if self._automatic else "Manual current",
                            "Keep the load in CV. Adjust charger current trim until the independent load reading matches the target.")
        elif stage == "current":
            self._track_stability(self.reading.current, p.cc_current, p.current_tolerance)
            if self._automatic and self.can_save_current:
                self.save_current()
            elif self._automatic and not self._within_current():
                self.current_trim += (p.cc_current - self.reading.current) * 0.55
                if abs(self.current_trim) > max(1.0, p.cc_current * 0.15):
                    self._fail("Automatic current trim exceeded its bound.")
        elif stage == "save_current" and ticks >= 2:
            self._safe_off()
            if self._automatic:
                self._set_stage("load_off", "Switch load mode", "Charger and load are off before changing to CC mode.")
            else:
                self._finish("Current saved (simulation)", "Continue with manual voltage calibration.")
        elif stage == "load_off" and ticks >= 2:
            self.reading.load_mode = "CC"
            self.reading.load_setpoint = p.voltage_cal_load_current
            self.reading.load_on = True
            self._set_stage("load_cc", "Load bank ON - CC", f"Hold {p.voltage_cal_load_current:g} A for voltage calibration.")
        elif stage == "load_cc" and ticks >= 2:
            self.reading.charger_on = True
            self._set_stage("start_voltage", "Start voltage calibration", "Wait for simulated output to settle.")
        elif stage == "start_voltage" and ticks >= 3:
            self._set_stage("voltage", "Auto voltage trim" if self._automatic else "Manual voltage",
                            "Keep the load in CC. Adjust charger voltage trim until the independent load reading matches the target.")
        elif stage == "voltage":
            self._track_stability(self.reading.voltage, p.end_voltage, p.voltage_tolerance)
            if self._automatic and self.can_save_voltage:
                self.save_voltage()
            elif self._automatic and not self._within_voltage():
                self.voltage_trim += (p.end_voltage - self.reading.voltage) * 0.55
                if abs(self.voltage_trim) > max(2.0, p.end_voltage * 0.15):
                    self._fail("Automatic voltage trim exceeded its bound.")
        elif stage == "save_voltage" and ticks >= 2:
            self._safe_off()
            if self._automatic:
                self._begin_check_cc()
            else:
                self._finish("Voltage saved (simulation)", "Both calibrations are saved in the model. Run the simulated cross-check.")
        elif stage == "check_cc" and ticks >= 8:
            if self._add_check("CC current", p.cc_current, self.reading.current, p.current_tolerance, "A"):
                self._safe_off()
                self.reading.load_mode = "CC"
                self.reading.load_setpoint = p.voltage_cal_load_current
                self.reading.load_on = self.reading.charger_on = True
                self._set_stage("check_voltage", "Cross-check end voltage", "Simulated voltage measurement under the profile's low-current load.")
        elif stage == "check_voltage" and ticks >= 8:
            if self._add_check("End voltage", p.end_voltage, self.reading.voltage, p.voltage_tolerance, "V"):
                self._safe_off()
                self.reading.load_mode = "MODEL"
                self.reading.load_setpoint = p.end_current
                self.reading.load_on = self.reading.charger_on = True
                self._set_stage("check_end", "Cross-check end current (model)",
                                "Illustrative model only. Actual charge termination and operation without CAN remain unverified.")
        elif stage == "check_end" and ticks >= 8:
            if self._add_check("End current (illustrative model)", p.end_current, self.reading.current, p.end_current_tolerance, "A"):
                self._finish("Simulation complete", "Three simulated checks recorded. No physical calibration or charge-termination test was performed.")

    def _begin_check_cc(self) -> None:
        if not self.current_saved or not self.voltage_saved:
            self._fail("Both calibration saves are required for cross-checking.")
            return
        self._safe_off()
        self.reading.load_mode = "CV"
        self.reading.load_setpoint = self._recipe.current_cal_load_voltage
        self.reading.load_on = self.reading.charger_on = True
        self._set_stage("check_cc", "Cross-check CC current", "Simulated current measurement at the profile's CV load setting.")

    def _add_check(self, name: str, target: float, measured: float, tolerance: float, unit: str) -> bool:
        in_tolerance = _finite(measured) and abs(measured - target) <= tolerance
        self.checks.append(CheckResult(name, target, measured, tolerance, unit,
                                       "SIMULATED IN TOLERANCE" if in_tolerance else "SIMULATED OUT OF TOLERANCE"))
        self._add_log(f"{name}: {measured:.6g} {unit}; target {target:.6g} +/- {tolerance:.6g}. SIMULATED ONLY.")
        if not in_tolerance:
            self._fail(name + " was outside tolerance in the simulation.")
        return in_tolerance

    def _finish(self, title: str, detail: str) -> None:
        self._safe_off()
        self.is_running = False
        self.finished_at = _utc_now()
        self._set_stage("done", title, detail)

    def snapshot(self) -> dict[str, Any]:
        """Return an isolated, JSON-ready snapshot for history/export."""
        return {
            "schema_version": 1,
            "simulation": True,
            "validity": "SIMULATION ONLY - NOT A HARDWARE CALIBRATION CERTIFICATE",
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exported_at": _utc_now(),
            "unit_serial": self.unit_serial,
            "operator": self.operator,
            "mode": self.mode,
            "state": self.state,
            "phase_detail": self.phase_detail,
            "is_running": self.is_running,
            "recipe": self._recipe.to_dict(),
            "reading": asdict(self.reading),
            "current_saved": self.current_saved,
            "voltage_saved": self.voltage_saved,
            "current_trim": self.current_trim,
            "voltage_trim": self.voltage_trim,
            "trim_units": {"current": "A (model correction)", "voltage": "V (model correction)"},
            "checks": [asdict(check) for check in self.checks],
            "not_tested": ["Physical calibration", "End-of-charge termination", "Operation without CAN commands"],
            "acceptance_requirements": {"standalone_without_can": True, "status": "NOT TESTED - REQUIRES HARDWARE"},
            "log": list(self.log),
        }

    @staticmethod
    def _csv_cell(value: Any) -> str:
        text = str(value)
        # Excel treats even quoted text beginning with these characters as a formula.
        # Preserve actual numeric fields but make user-entered identifiers inert.
        if isinstance(value, str) and text.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + text
        return text

    def export_report(self, path: str | Path) -> None:
        if not str(path).strip():
            raise ValueError("Choose a report file.")
        p = self._recipe
        rows: list[tuple[Any, ...]] = [
            ("WARNING", "Validity", "SIMULATION ONLY - NOT A HARDWARE CALIBRATION CERTIFICATE", "", "NO HARDWARE TEST"),
            ("Run", "ID", self.run_id, "", self.state),
            ("Run", "Started UTC", self.started_at, "", self.mode),
            ("Run", "Exported UTC", _utc_now(), "", self.mode),
            ("Run", "Unit serial", self.unit_serial, "", "SIMULATION"),
            ("Run", "Operator", self.operator, "", "SIMULATION"),
            ("Profile", "Variant", p.name, "", "SIMULATED"),
        ]
        for name, value, unit, status in (
            ("CC current", p.cc_current, "A", "Target"),
            ("End voltage", p.end_voltage, "V", "Target"),
            ("End current", p.end_current, "A", "Target"),
            ("Current tolerance", p.current_tolerance, "A", "Absolute tolerance"),
            ("Voltage tolerance", p.voltage_tolerance, "V", "Absolute tolerance"),
            ("End-current tolerance", p.end_current_tolerance, "A", "Absolute tolerance"),
            ("Current-calibration CV load", p.current_cal_load_voltage, "V", "Load setting"),
            ("Voltage-calibration CC load", p.voltage_cal_load_current, "A", "Load setting"),
            ("Voltage limit", p.max_voltage, "V", "Limit"),
            ("Current limit", p.max_current, "A", "Limit"),
            ("Power limit", p.max_power, "W", "Limit"),
        ):
            rows.append(("Profile", name, value, unit, status))
        for key, value in p.device_settings.items():
            rows.append(("Device setting", key, value, "", "Stored only; engineering validation required"))
        rows.extend((
            ("Calibration", "Current saved", self.current_saved, "", "SIMULATED ONLY"),
            ("Calibration", "Voltage saved", self.voltage_saved, "", "SIMULATED ONLY"),
            ("Calibration", "Current trim", self.current_trim, "A", "Model correction; not a charger register value"),
            ("Calibration", "Voltage trim", self.voltage_trim, "V", "Model correction; not a charger register value"),
        ))
        for check in self.checks:
            rows.append(("Check", check.name, check.measured, check.unit, check.status))
            rows.append(("Check target", check.name, check.target, check.unit, f"+/- {check.tolerance:g}"))
        rows.extend((
            ("Unverified", "End-of-charge termination", "Requires a verified physical test procedure", "", "NOT TESTED"),
            ("Unverified", "Operation without CAN commands", "Variant behavior must be confirmed", "", "NOT TESTED"),
        ))
        rows.extend(("Event", "Log", entry, "", "SIMULATION") for entry in self.log)
        with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("Category", "Item", "Value", "Unit", "Status"))
            writer.writerows(tuple(self._csv_cell(value) for value in row) for row in rows)
        self._add_log(f"Simulation report exported to {Path(path).name}.")
