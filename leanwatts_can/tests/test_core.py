import csv
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from leanwatts_can.core import CalibrationEngine, Recipe


def profile():
    return Recipe(
        name="TEST ONLY / 58.4 V 15 A", cc_current=15, end_voltage=58.4,
        end_current=0.5, current_tolerance=0.05, voltage_tolerance=0.10,
        end_current_tolerance=0.05, current_cal_load_voltage=48,
        voltage_cal_load_current=1, max_voltage=60, max_current=16, max_power=1000,
    )


class CalibrationCoreTests(unittest.TestCase):
    def until(self, engine, predicate, limit=240):
        ticks = 0
        while not predicate() and ticks < limit:
            engine.tick()
            ticks += 1
        self.assertTrue(predicate(), engine.state + ": " + engine.phase_detail)
        return ticks

    def ticks(self, engine, count):
        for _ in range(count):
            engine.tick()

    def save_manual_current(self, engine):
        engine.start_manual_current()
        self.until(engine, lambda: engine.state == "Manual current", 25)
        engine.adjust_current(engine.recipe.cc_current * 0.04)
        self.until(engine, lambda: engine.can_save_current, 30)
        engine.save_current()
        self.until(engine, lambda: not engine.is_running, 10)

    def completed_engine(self):
        engine = CalibrationEngine(profile(), "TEST-001", "Test operator")
        engine.start_auto()
        self.until(engine, lambda: not engine.is_running)
        self.assertEqual(engine.state, "Simulation complete", engine.phase_detail)
        return engine

    def test_reject_nonfinite_zero_and_wrong_type_values(self):
        for field, value in (("cc_current", math.nan), ("max_power", math.inf),
                             ("end_current_tolerance", 0), ("cc_current", "15"),
                             ("max_current", True), ("max_power", 10 ** 400)):
            with self.subTest(field=field, value=value):
                p = profile()
                setattr(p, field, value)
                self.assertTrue(p.validate())
                with self.assertRaises(ValueError):
                    CalibrationEngine(p)

    def test_lower_voltage_variants_need_appropriate_cv_load(self):
        p = profile()
        p.end_voltage = 29.6
        self.assertTrue(p.validate())
        p.current_cal_load_voltage = 24
        self.assertEqual(p.validate(), "")

    def test_targets_and_tolerances_fit_output_limits(self):
        for field, value in (("max_voltage", 58.45), ("max_current", 15.01), ("max_power", 880)):
            p = profile()
            setattr(p, field, value)
            self.assertTrue(p.validate(), field)

    def test_profile_snapshot_is_isolated_during_run(self):
        p = profile()
        engine = CalibrationEngine(p)
        p.cc_current = 100
        exposed = engine.recipe
        exposed.cc_current = 200
        self.assertEqual(engine.recipe.cc_current, 15)

    def test_optional_device_fields_validate_precision_range_and_keys(self):
        p = profile()
        p.device_settings["ratedVoltage"] = 48.1
        self.assertEqual(p.validate(), "")
        for settings in ({"ratedVoltage": 48.15}, {"ratedVoltage": 3276.8},
                         {"blindCharge": 256}, {"unknown": 1}, {"temperatureLimit": math.nan},
                         {"adjustCurrentMin": 2, "adjustCurrentMax": 1}):
            with self.subTest(settings=settings):
                p.device_settings = settings
                self.assertTrue(p.validate())

    def test_device_settings_json_round_trip_and_deep_copy(self):
        p = profile()
        p.device_settings["ratedVoltage"] = 48
        duplicate = p.copy()
        duplicate.device_settings["ratedVoltage"] = 72
        self.assertEqual(p.device_settings["ratedVoltage"], 48)
        engine = CalibrationEngine(p)
        p.device_settings["ratedVoltage"] = 96
        exposed = engine.recipe
        exposed.device_settings["ratedVoltage"] = 120
        self.assertEqual(engine.recipe.device_settings["ratedVoltage"], 48)
        restored = Recipe.from_dict(json.loads(json.dumps(duplicate.to_dict())))
        self.assertEqual(restored.device_settings["ratedVoltage"], 72)
        self.assertEqual(restored.validate(), "")
        p.device_settings = None
        self.assertEqual(p.validate(), "")
        self.assertEqual(p.copy().device_settings, {})
        self.assertEqual(Recipe.from_dict(p.to_dict()).device_settings, {})

    def test_auto_completes_with_three_simulated_checks_and_outputs_off(self):
        engine = CalibrationEngine(profile())
        engine.start_auto()
        self.assertFalse(engine.current_saved or engine.voltage_saved)
        self.assertEqual(engine.checks, [])
        count = self.until(engine, lambda: not engine.is_running)
        self.assertLessEqual(count, 100, "Demo should finish within 25 seconds at 250 ms/tick")
        self.assertEqual(engine.state, "Simulation complete", engine.phase_detail)
        self.assertTrue(engine.current_saved and engine.voltage_saved)
        self.assertEqual(len(engine.checks), 3)
        self.assertTrue(all(check.status == "SIMULATED IN TOLERANCE" for check in engine.checks))
        self.assertFalse(engine.reading.charger_on or engine.reading.load_on)
        self.assertEqual(engine.reading.power, 0)

    def test_manual_save_requires_correct_phase_tolerance_and_stability(self):
        engine = CalibrationEngine(profile())
        with self.assertRaises(ValueError):
            engine.save_current()
        engine.start_manual_current()
        self.until(engine, lambda: engine.state == "Manual current", 25)
        self.ticks(engine, 10)
        with self.assertRaises(ValueError):
            engine.save_current()
        engine.adjust_current(0.6)
        with self.assertRaises(ValueError):
            engine.save_current()
        self.until(engine, lambda: engine.can_save_current, 30)
        engine.save_current()
        self.until(engine, lambda: not engine.is_running, 10)
        self.assertTrue(engine.current_saved)
        self.assertFalse(engine.voltage_saved)
        self.assertFalse(engine.reading.charger_on or engine.reading.load_on)

    def test_manual_voltage_and_cross_check_share_session(self):
        engine = CalibrationEngine(profile())
        with self.assertRaises(ValueError):
            engine.start_manual_voltage()
        with self.assertRaises(ValueError):
            engine.start_cross_check()
        self.save_manual_current(engine)
        session = engine.run_id
        with self.assertRaises(ValueError):
            engine.start_cross_check()
        engine.start_manual_voltage()
        self.until(engine, lambda: engine.state == "Manual voltage", 15)
        self.assertEqual(engine.reading.load_mode, "CC")
        self.assertEqual(engine.reading.load_setpoint, 1)
        engine.adjust_voltage(58.4 * 0.03)
        self.until(engine, lambda: engine.can_save_voltage, 30)
        engine.save_voltage()
        self.until(engine, lambda: not engine.is_running, 10)
        engine.start_cross_check()
        self.assertEqual(engine.run_id, session)
        self.assertEqual(engine.checks, [])
        self.until(engine, lambda: not engine.is_running, 40)
        self.assertEqual(engine.state, "Simulation complete")
        self.assertEqual(len(engine.checks), 3)

    def test_trim_controls_bounded_and_unavailable_during_auto(self):
        engine = CalibrationEngine(profile())
        engine.start_auto()
        with self.assertRaises(ValueError):
            engine.adjust_current(0.1)
        engine.abort()
        engine.start_manual_current()
        self.until(engine, lambda: engine.state == "Manual current", 25)
        with self.assertRaises(ValueError):
            engine.adjust_current(math.nan)
        with self.assertRaises(ValueError):
            engine.adjust_current(100)
        self.assertEqual(engine.current_trim, 0)

    def test_abort_immediate_idempotent_and_blocks_further_progress(self):
        engine = CalibrationEngine(profile())
        engine.start_auto()
        self.until(engine, lambda: engine.reading.charger_on, 30)
        engine.abort()
        engine.abort()
        self.ticks(engine, 100)
        self.assertFalse(engine.is_running or engine.reading.charger_on or engine.reading.load_on)
        self.assertFalse(engine.current_saved)
        self.assertEqual(engine.checks, [])
        with self.assertRaises(ValueError):
            engine.save_current()

    def test_new_run_invalidates_results_and_old_log(self):
        engine = self.completed_engine()
        old_session = engine.run_id
        engine.log.append("OLD RUN SENTINEL")
        engine.start_manual_current()
        self.assertFalse(engine.current_saved or engine.voltage_saved)
        self.assertEqual(engine.checks, [])
        self.assertNotEqual(engine.run_id, old_session)
        self.assertNotIn("OLD RUN SENTINEL", engine.log)

    def test_overlimit_reading_faults_before_next_sample(self):
        engine = CalibrationEngine(profile())
        engine.start_auto()
        engine.reading.current = 17
        engine.tick()
        self.assertEqual(engine.state, "Fault - stopped")
        self.assertFalse(engine.is_running or engine.reading.load_on)
        self.assertEqual(engine.reading.current, 0)

    def test_nonfinite_telemetry_cannot_become_a_result(self):
        engine = CalibrationEngine(profile())
        engine.start_auto()
        engine.reading.voltage = math.nan
        engine.tick()
        self.assertEqual(engine.state, "Fault - stopped")
        self.assertEqual(engine.checks, [])

    def test_manual_operation_has_bounded_timeout(self):
        engine = CalibrationEngine(profile())
        engine.start_manual_current()
        self.ticks(engine, 3602)
        self.assertEqual(engine.state, "Fault - stopped")
        self.assertFalse(engine.is_running)

    def test_stalled_tick_loop_stops_outputs(self):
        engine = CalibrationEngine(profile())
        with patch("leanwatts_can.core.time.monotonic", return_value=1000):
            engine.start_auto()
        with patch("leanwatts_can.core.time.monotonic", return_value=1003.1):
            engine.tick()
        self.assertEqual(engine.state, "Fault - stopped")
        self.assertFalse(engine.is_running or engine.reading.load_on or engine.reading.charger_on)

    def test_csv_report_identifies_simulated_and_unverified_results(self):
        engine = self.completed_engine()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.csv"
            engine.export_report(target)
            text = target.read_text(encoding="utf-8-sig")
        self.assertIn("SIMULATION ONLY - NOT A HARDWARE CALIBRATION CERTIFICATE", text)
        self.assertIn("End-of-charge termination", text)
        self.assertIn("NOT TESTED", text)
        self.assertNotIn("PASS", text)
        self.assertIn("Power limit", text)
        self.assertIn("Current tolerance", text)
        self.assertIn("TEST-001", text)

    def test_json_snapshot_is_isolated_and_identifies_operator_and_unit(self):
        engine = self.completed_engine()
        snapshot = json.loads(json.dumps(engine.snapshot(), allow_nan=False))
        self.assertTrue(snapshot["simulation"])
        self.assertEqual(snapshot["unit_serial"], "TEST-001")
        self.assertEqual(snapshot["operator"], "Test operator")
        self.assertEqual(len(snapshot["checks"]), 3)
        self.assertEqual(snapshot["state"], "Simulation complete")
        snapshot["log"].append("not real")
        snapshot["recipe"]["cc_current"] = 99
        self.assertNotIn("not real", engine.log)
        self.assertEqual(engine.recipe.cc_current, 15)

    def test_csv_user_identifiers_are_inert_in_spreadsheets(self):
        p = profile()
        p.name = '=HYPERLINK("https://example.invalid", "variant")'
        engine = CalibrationEngine(p, "=1+1", "@SUM(1,1)")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.csv"
            engine.export_report(target)
            rows = list(csv.reader(io.StringIO(target.read_text(encoding="utf-8-sig"))))
        fields = {row[1]: row[2] for row in rows}
        self.assertTrue(fields["Variant"].startswith("'="))
        self.assertEqual(fields["Unit serial"], "'=1+1")
        self.assertEqual(fields["Operator"], "'@SUM(1,1)")


if __name__ == "__main__":
    unittest.main()
