"""Session-local starting trims; no hardware, account data or GUI access."""
import copy
import unittest
from dataclasses import asdict, replace

from leanwatts_can.automation import AutomationSettings
from leanwatts_can.session_seed import apply_session_seed, select_session_seed
from leanwatts_can.tests.test_production_sessions import production_recipe


def completed_unit(serial="FIRST", current=69.5, voltage=81.1, *, recipe=None, revision=1):
    recipe = recipe or production_recipe()
    return dict(attempt_id="attempt-" + serial, sequence=1, serial=serial,
        recipe=recipe.to_dict(), recipe_revision=revision, recipe_name=recipe.name,
        started_at="2026-11-12T03:00:00+00:00", finished_at="2026-11-12T03:01:00+00:00",
        outcome="CALIBRATED", calibration_result="PASS", test_result="NOT RUN",
        simulation=False, is_running=False, run_tests=False,
        calibration=dict(operation="calibration", is_running=False, simulation=False,
            outcome="CALIBRATED — PERSISTENCE UNVERIFIED", current_duty=current,
            voltage_duty=voltage, preflight_result={"status": "PASS"},
            stop_outcome="Output below approved shutdown thresholds; load OFF confirmed",
            results=[{"name": "Current calibration", "status": "SAVE REQUEST ACCEPTED"},
                     {"name": "Voltage calibration", "status": "SAVE REQUEST ACCEPTED"}]))


def session(*units):
    return dict(schema="leanwatts-session/1", session_id="session-one", status="OPEN", units=list(units))


class SessionSeedTests(unittest.TestCase):
    def setUp(self):
        self.recipe = production_recipe()
        self.settings = replace(AutomationSettings.demo(), current_min=60, current_max=85,
                                voltage_min=60, voltage_max=85, auto_direction=True,
                                current_direction=0, voltage_direction=0)

    def seed(self, *units):
        return select_session_seed(session(*units), self.recipe, 1)

    def test_first_charger_keeps_standard_start_and_settings_are_immutable(self):
        before = asdict(self.settings)
        self.assertIsNone(self.seed())
        result, metadata = apply_session_seed(self.settings, None)
        self.assertEqual(asdict(result), before)
        self.assertEqual(asdict(self.settings), before)
        self.assertEqual(metadata["method"], "configured")
        self.assertEqual(metadata["current_initial"], 80)
        self.assertEqual(metadata["voltage_initial"], 80)

    def test_first_successful_charger_is_anchor_not_latest_or_recursive_offset(self):
        first, later = completed_unit(), completed_unit("LATER", 71, 82)
        seed = self.seed(first, later)
        result, metadata = apply_session_seed(self.settings, seed)
        self.assertEqual(result.current_initial, 71.5)
        self.assertEqual(result.voltage_initial, 83.1)
        self.assertEqual(metadata["source_serial"], "FIRST")
        self.assertEqual(metadata["method"], "session_reference")
        self.assertEqual(metadata["offset"], 2)
        self.assertEqual(self.settings.current_initial, 80)
        self.assertEqual(result.current_direction, 0)
        self.assertEqual(result.voltage_direction, 0)
        self.assertTrue(result.auto_direction)
        self.assertEqual(result.validate(), "")

    def test_seed_requires_same_recipe_and_revision(self):
        changed = self.recipe.copy()
        changed.programmed_end_current = 1.7
        other = completed_unit("CHANGED", recipe=changed)
        old_revision = completed_unit("OLD", revision=2)
        self.assertIsNone(self.seed(other, old_revision))
        valid = completed_unit()
        self.assertEqual(apply_session_seed(self.settings, self.seed(other, valid))[1]["source_serial"], "FIRST")

    def test_new_and_closed_sessions_do_not_carry_previous_anchor(self):
        self.assertIsNotNone(self.seed(completed_unit()))
        self.assertIsNone(select_session_seed(None, self.recipe, 1))
        self.assertIsNone(select_session_seed(session(), self.recipe, 1))
        closed = session(completed_unit())
        closed["status"] = "CLOSED"
        self.assertIsNone(select_session_seed(closed, self.recipe, 1))

    def test_active_session_reloaded_from_data_preserves_anchor(self):
        data = session(completed_unit())
        selected = select_session_seed(copy.deepcopy(data), self.recipe.copy(), 1)
        result, metadata = apply_session_seed(self.settings, selected)
        self.assertEqual(result.current_initial, 71.5)
        self.assertEqual(metadata["source_serial"], "FIRST")

    def test_failed_interrupted_running_and_simulated_units_never_supply_seed(self):
        for outcome in ("FAIL", "STOPPED", "ABORTED", "INTERRUPTED", "RUNNING", "SIMULATION"):
            with self.subTest(outcome=outcome):
                unit = completed_unit()
                unit["outcome"] = outcome
                self.assertIsNone(self.seed(unit))
        for scope in ("unit", "calibration"):
            unit = completed_unit()
            (unit if scope == "unit" else unit["calibration"])["simulation"] = True
            self.assertIsNone(self.seed(unit))
            unit = completed_unit()
            (unit if scope == "unit" else unit["calibration"])["is_running"] = True
            self.assertIsNone(self.seed(unit))

    def test_first_failure_uses_first_later_success_and_full_test_pass_is_eligible(self):
        failed = completed_unit("FAILED")
        failed["outcome"] = "FAIL"
        good = completed_unit("GOOD")
        good.update(outcome="PASS", run_tests=True, test_result="PASS")
        _, metadata = apply_session_seed(self.settings, self.seed(failed, good))
        self.assertEqual(metadata["source_serial"], "GOOD")

    def test_complete_saves_parameter_check_and_shutdown_are_required(self):
        mutations = (
            lambda u: u.update(calibration_result="FAIL"),
            lambda u: u["calibration"].update(results=[]),
            lambda u: u["calibration"]["results"].pop(),
            lambda u: u["calibration"]["results"][0].update(status="FAIL"),
            lambda u: u["calibration"].update(preflight_result={"status": "FAIL"}),
            lambda u: u["calibration"].update(stop_outcome="Output unconfirmed"),
            lambda u: u["calibration"].update(outcome="ABORTED"),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                unit = completed_unit()
                mutation(unit)
                self.assertIsNone(self.seed(unit))

    def test_final_duties_come_from_calibration_not_final_test_snapshot(self):
        unit = completed_unit()
        unit.update(current_duty=80, voltage_duty=80, test={"current_duty": 80, "voltage_duty": 80})
        result, _ = apply_session_seed(self.settings, self.seed(unit))
        self.assertEqual((result.current_initial, result.voltage_initial), (71.5, 83.1))

    def test_bad_duty_records_are_ignored(self):
        for bad in (None, "69.5", True, float("nan"), float("inf"), -1, 1000.1, 69.51):
            for axis in ("current", "voltage"):
                with self.subTest(bad=bad, axis=axis):
                    unit = completed_unit()
                    unit["calibration"][axis + "_duty"] = bad
                    self.assertIsNone(self.seed(unit))

    def test_offset_is_clamped_without_widening_bounds_or_other_settings(self):
        seed = self.seed(completed_unit(current=84, voltage=84.9))
        result, metadata = apply_session_seed(self.settings, seed)
        self.assertEqual(result.current_initial, 85)
        self.assertEqual(result.voltage_initial, 85)
        self.assertEqual(metadata["current_initial"], 85)
        self.assertEqual(metadata["voltage_initial"], 85)
        self.assertIn("current", metadata["clamped"])
        self.assertIn("voltage", metadata["clamped"])
        before, after = asdict(self.settings), asdict(result)
        for key in before:
            if key not in ("current_initial", "voltage_initial"):
                self.assertEqual(after[key], before[key], key)
        self.assertEqual(self.settings.current_initial, 80)

    def test_changed_bounds_reject_an_outside_reference_and_use_configured_start(self):
        seed = self.seed(completed_unit())
        tighter = replace(self.settings, current_min=75)
        result, metadata = apply_session_seed(tighter, seed)
        self.assertEqual(asdict(result), asdict(tighter))
        self.assertEqual(metadata["method"], "configured")

    def test_records_and_selected_seed_remain_unchanged_when_applied(self):
        unit = completed_unit()
        original_unit = copy.deepcopy(unit)
        seed = self.seed(unit)
        original_seed = copy.deepcopy(seed)
        result, metadata = apply_session_seed(self.settings, seed)
        self.assertEqual(unit, original_unit)
        self.assertEqual(seed, original_seed)
        metadata["source_serial"] = "changed display"
        self.assertEqual(seed, original_seed)
        self.assertEqual(result.current_initial, 71.5)

    def test_malformed_prior_records_cannot_block_a_later_good_anchor(self):
        for value in (None, [], "invalid", {}, {"outcome": "CALIBRATED"}):
            with self.subTest(value=value):
                selected = self.seed(value, completed_unit())
                self.assertIsNotNone(selected)
        for malformed in (None, [], "invalid"):
            unit = completed_unit()
            unit["calibration"] = malformed
            self.assertIsNone(self.seed(unit))


if __name__ == "__main__":
    unittest.main()
