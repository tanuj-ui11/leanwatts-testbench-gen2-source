"""Derive the load-test plan from a variant; no hardware access or target edits."""
from __future__ import annotations

import math

from .core import Recipe


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def apply_automatic_settings(recipe: Recipe) -> Recipe:
    """Return a copy with derived settings. Main values and hard limits are unchanged.

    Existing production validation remains authoritative. Incomplete or incompatible
    targets stay drafts; this function never raises a hard limit to make them fit.
    The nominal 48 V fixture uses 48 V calibration / 49 V full-current testing.
    """
    result = recipe.copy()
    current = recipe.cc_current if _positive(recipe.cc_current) else 0.0
    voltage = recipe.end_voltage if _positive(recipe.end_voltage) else 0.0
    cutoff = recipe.end_current if _positive(recipe.end_current) else 0.0
    result.current_tolerance = round(min(.1, current * .05), 6)
    result.voltage_tolerance = round(min(.1, voltage * .01), 6)
    result.end_current_tolerance = round(min(.1, cutoff * .1), 6)
    result.max_power = (min(3700.0, recipe.max_voltage * recipe.max_current)
                        if _positive(recipe.max_voltage) and _positive(recipe.max_current) else 0.0)

    nominal = (recipe.device_settings or {}).get("ratedVoltage", 0)
    if voltage:
        base = nominal if _positive(nominal) and nominal < voltage * .95 else voltage * .82
        result.current_cal_load_voltage = round(base, 4)
        result.cc_test_load_voltage = round(base + min(1.0, (voltage - base) / 2), 4)
    else:
        result.current_cal_load_voltage = result.cc_test_load_voltage = 0.0
    result.voltage_cal_load_current = round(min(1.0, current * .2), 4)

    # Both the required cutoff band and programmed cutoff must be below a test
    # current used to keep the charger operating. Never change either cutoff.
    programmed_cutoff = recipe.charger_values()["endCurrent"]
    lower = max(cutoff + result.end_current_tolerance,
                programmed_cutoff if _positive(programmed_cutoff) else 0.0)
    room = current - lower
    if room > 0:
        preferred = max(1.0, float(math.ceil(lower + .1)))
        result.voltage_test_load_current = round(min(preferred, lower + room / 2), 4)
        ramp_start = min(5.0, current * .8)
        if ramp_start <= lower:
            ramp_start = lower + room / 2
    else:
        # Positive finite placeholders preserve the draft; validate_production
        # rejects impossible cutoff/full-current relationships before any I/O.
        result.voltage_test_load_current = round(current / 2, 4)
        ramp_start = current / 2

    minimum = round(min(.1, cutoff * .25), 4) if cutoff else .1
    step = round(min(.1, result.end_current_tolerance, max(0, ramp_start - minimum) / 4), 4)
    if step <= 0:
        step = .01
    points = max(1, math.ceil(max(0, ramp_start - minimum) / step) + 1)
    result.termination_test = dict(enabled=True, start_current=round(ramp_start, 4),
        minimum_current=minimum, step_current=step, dwell_seconds=3.0,
        off_voltage=round(min(5.0, voltage * .1), 4),
        off_current=round(min(.05, minimum / 2), 5),
        confirm_seconds=3.0, max_seconds=float(min(3600, max(120, points * 5 + 30))))
    return result


def automatic_settings_summary(recipe: Recipe) -> str:
    result = apply_automatic_settings(recipe)
    if not all(_positive(value) for value in (result.cc_current, result.end_voltage, result.end_current)):
        return "Enter the main current, voltage and cutoff values. Test points and the cutoff ramp are calculated automatically."
    ramp = result.termination_test
    return (f"Automatic: current calibration at {result.current_cal_load_voltage:g} V; "
            f"voltage calibration at {result.voltage_cal_load_current:g} A.\n"
            f"Tests: full current at {result.test_cv_voltage:g} V; end voltage at {result.test_cc_current:g} A; "
            f"cutoff ramp {ramp['start_current']:g} → {ramp['minimum_current']:g} A "
            f"in {ramp['step_current']:g} A steps.\n"
            f"Allowed error: current ±{result.current_tolerance:g} A, voltage ±{result.voltage_tolerance:g} V, "
            f"cutoff ±{result.end_current_tolerance:g} A. Power ceiling {result.max_power:g} W.")
