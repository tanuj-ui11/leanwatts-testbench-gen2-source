"""Offline evidence for early-load baseline and start-only diagnostic tests."""
import unittest
from dataclasses import replace

from leanwatts_can.automation import AutomationEngine, AutomationSettings, DemoBench
from leanwatts_can.tests.test_baseline import BaselineBench
from leanwatts_can.tests.test_production_sessions import production_recipe
from leanwatts_can.tests.test_recipe_automation import ramp_recipe


class DiagnosticBench(DemoBench):
    def __init__(self, recipe):
        super().__init__(recipe, test_start_method="external_restart")
        self.transform = lambda reading, bench: reading

    def read(self, now):
        return self.transform(super().read(now), self)


class ExperimentalAutomationTests(unittest.TestCase):
    def advance(self, engine, until=None, limit=2000):
        for _ in range(limit):
            engine.tick(engine._now + .25)
            if not engine.is_running or (until is not None and until()):
                return
        self.fail("Diagnostic workflow did not reach its bounded result.")

    def baseline(self):
        recipe = production_recipe()
        bench = BaselineBench(recipe)
        engine = AutomationEngine(recipe, "UNIT", "Tester", bench=bench,
                                  settings=AutomationSettings.demo(), baseline_required=True)
        engine.start_calibration(now=0)
        return engine, bench

    def diagnostic(self, transform=None):
        recipe = ramp_recipe()
        recipe.termination_test["max_seconds"] = 120
        bench = DiagnosticBench(recipe)
        if transform:
            bench.transform = transform
        program = {"testing": {"start_method": "external_restart", "procedure": "ocv_current_fixed_cutoff_v1"}}
        engine = AutomationEngine(recipe, "UNIT", "Tester", bench=bench,
            settings=AutomationSettings.demo(), test_program=program)
        engine.start_test(now=0)
        return engine, bench

    def test_cv_is_prepared_off_and_first_above_threshold_sample_engages_load(self):
        engine, bench = self.baseline()
        names = [item[0] for item in bench.commands]
        self.assertLess(names.index("load_setup"), names.index("baseline_start"))
        self.assertFalse(bench._load_on)
        bench.transform = lambda r, b: replace(r, voltage=49, power=0) if not b._load_on else r
        engine.tick(.25)
        self.assertEqual(engine.phase, "baseline_open_voltage")
        self.assertFalse(bench._load_on)
        bench.transform = lambda r, b: replace(r, voltage=49.1, power=0) if not b._load_on else r
        engine.tick(.5)
        self.assertEqual(engine.phase, "baseline_current")
        self.assertTrue(bench._load_on)
        self.assertEqual(engine.baseline_result["open_voltage"]["sample_count"], 1)
        self.assertEqual(engine.baseline_result["open_voltage"]["measured"], 49.1)
        self.assertIn("not stabilized", engine.baseline_result["open_voltage"]["detail"])
        engine.stop()

    def test_current_ramp_and_short_early_plateau_do_not_become_baseline(self):
        engine, bench = self.baseline()
        def delayed(r, b):
            if b._baseline_normal and b._load_on:
                current = 1.0 if r.timestamp < 5 else min(14.8, r.timestamp - 4)
                return replace(r, current=current, power=current*r.voltage)
            return r
        bench.transform = delayed
        self.advance(engine, lambda: engine.baseline_result["current"].get("status") == "PASS")
        self.assertEqual(engine.baseline_result["current"]["measured"], 14.8)
        self.assertGreater(engine._now, 20)
        engine.stop()

    def test_threshold_engagement_can_clear_startup_battery_fault_without_deadlocking(self):
        engine, bench = self.baseline()
        def clears_only_with_load(r, b):
            if b._baseline_normal:
                return replace(r, charger_fault_word=0 if b._load_on else 0x20,
                               charger_fault_observed_at=r.timestamp)
            return r
        bench.transform = clears_only_with_load
        original_deadline = engine._charger_clear_deadline
        engine.tick(.25)
        self.assertTrue(bench._load_on)
        self.assertEqual(engine.phase, "baseline_current")
        self.assertIsNotNone(engine._charger_clear_after)
        self.assertEqual(engine._charger_clear_deadline, original_deadline)
        self.assertEqual(engine.baseline_result["current"]["status"], "RUNNING")
        self.assertFalse(any(c[0] == "prepare_parameters" for c in bench.commands))
        self.advance(engine, lambda: engine.baseline_result["status"] == "PASS")
        self.assertEqual(engine.baseline_result["status"], "PASS", engine.detail)
        self.assertEqual(engine.baseline_result["current"]["status"], "PASS")
        engine.stop()

    def test_threshold_engagement_never_ages_out_pending_battery_fault_or_extends_deadline(self):
        for clear_word, observed_at in ((0x20, .25), (0, 0), (0, .1)):
            with self.subTest(word=clear_word, observed_at=observed_at):
                engine, bench = self.baseline()
                bench.transform = lambda r, b: replace(r, charger_fault_word=0x20,
                    charger_fault_observed_at=r.timestamp) if b._baseline_normal else r
                engine.tick(.25)
                self.assertTrue(bench._load_on)
                bench.transform = lambda r, b: replace(r, charger_fault_word=clear_word,
                    charger_fault_observed_at=observed_at) if b._baseline_normal else r
                self.advance(engine)
                self.assertEqual(engine.baseline_result["status"], "FAIL")
                self.assertIsNone(engine.baseline_result["current"]["measured"])
                self.assertLessEqual(engine._now, 16)
                forbidden = {"prepare_parameters", "three_duty_pairs", "save_current", "save_voltage",
                             "calibration_start_and_three_duty_pairs"}
                self.assertFalse(any(c[0] in forbidden for c in bench.commands))

    def test_other_startup_fault_never_enables_load_despite_voltage_threshold(self):
        engine, bench = self.baseline()
        bench.transform = lambda r, b: replace(r, charger_fault_word=0x21,
            charger_fault_observed_at=r.timestamp) if b._baseline_normal else r
        engine.tick(.25)
        self.assertFalse(engine.is_running)
        self.assertFalse(any(c[0] == "load_on" for c in bench.commands))
        self.assertEqual(engine.baseline_result["status"], "FAIL")

    def test_program_trigger_outside_variant_limits_is_rejected_before_io(self):
        recipe = production_recipe()
        bench = BaselineBench(recipe)
        engine = AutomationEngine(recipe, "UNIT", "Tester", bench=bench, baseline_required=True,
            test_program={"baseline": {"engage_voltage": recipe.end_voltage}})
        with self.assertRaisesRegex(ValueError, "below output voltage"):
            engine.start_calibration(now=0)
        self.assertEqual(bench.commands, [])

    def test_normal_cutoff_requires_hold_and_verified_cleanup(self):
        engine, bench = self.diagnostic()
        self.advance(engine)
        self.assertEqual(engine.outcome, "SIMULATION — PASS", engine.detail)
        result = engine.results[-1]
        self.assertTrue(result["control_hold_completed"])
        self.assertEqual(result["control_hold_required_seconds"], 0)
        self.assertFalse(result["adaptive_steps"])
        self.assertEqual(result["status"], "PASS")
        self.assertAlmostEqual(result["observed_interval_a"][0], 1.1)
        self.assertAlmostEqual(result["observed_interval_a"][1], 1.2)
        self.assertFalse(engine.snapshot()["end_current_termination_verified"])
        self.assertFalse(engine.snapshot()["normal_operation_verified"])
        self.assertFalse(engine.snapshot()["diagnostic_only"])
        self.assertTrue(engine.snapshot()["test_cleanup_verified"])
        self.assertTrue(all(c[2] == "external_restart" for c in bench.commands if c[0] == "post_test_start"))
        forbidden = {"three_duty_pairs", "save_current", "save_voltage", "calibration_start_and_three_duty_pairs"}
        self.assertFalse(any(c[0] in forbidden for c in bench.commands))
        self.assertEqual(engine.snapshot()["can_tx_count"], 0)

    def test_premature_shutdown_above_cutoff_cannot_pass(self):
        engine, bench = self.diagnostic()
        self.advance(engine, lambda: engine.phase == "test_termination")
        started = engine._now
        bench.transform = lambda r, b: replace(r, voltage=0, current=0, power=0) if r.timestamp-started > 10 else r
        self.advance(engine)
        self.assertEqual(engine.results[-1]["status"], "FAIL")
        self.assertTrue(engine.termination_evidence["shutdown_observed"])
        self.assertTrue(engine.termination_evidence["control_hold_completed"])
        self.assertIn("outside the required range", engine.results[-1]["detail"])

    def test_no_cutoff_fails_normal_operation_check(self):
        def remains_on(r, b):
            if b._termination_ramping:
                return replace(r, voltage=b.recipe.end_voltage, current=b._setpoint,
                               power=b.recipe.end_voltage*b._setpoint)
            return r
        engine, _ = self.diagnostic(remains_on)
        self.advance(engine)
        self.assertEqual(engine.results[-1]["status"], "FAIL")
        self.assertEqual(engine.outcome, "SIMULATION — FAIL")
        self.assertIn("without actual cutoff", engine.results[-1]["detail"])

    def test_no_separate_loaded_voltage_hold_blocks_fixed_ramp(self):
        engine, bench = self.diagnostic()
        self.advance(engine, lambda: engine.phase == "test_termination")
        bench.transform = lambda r, b: replace(r, voltage=50, power=50*r.current) if r.current > 0 else r
        self.advance(engine)
        steps = [c for c in bench.commands if c[0] == "load_cc_step"]
        self.assertGreater(len(steps), 1)
        self.assertTrue(engine.termination_evidence["control_hold_completed"])
        self.assertEqual(engine.results[-1]["status"], "PASS")

    def test_isolated_startup_battery_fault_waits_for_fresh_clear_without_polling(self):
        def transient(r, b):
            return replace(r, charger_fault_word=0x20 if r.timestamp <= 2 else 0,
                           charger_fault_observed_at=r.timestamp)
        engine, bench = self.diagnostic(transient)
        self.advance(engine)
        self.assertEqual(engine.outcome, "SIMULATION — PASS", engine.detail)
        self.assertFalse(any(c[0] == "diagnostic_poll" for c in bench.commands))

    def test_persistent_or_other_startup_faults_are_not_ignored(self):
        for word in (0x20, 0x21):
            with self.subTest(word=word):
                engine, bench = self.diagnostic(lambda r, b: replace(r, charger_fault_word=word,
                    charger_fault_observed_at=r.timestamp))
                self.advance(engine)
                self.assertIn("ERROR", engine.outcome)
                self.assertLessEqual(engine._now, 16)
                self.assertFalse(engine.termination_evidence.get("shutdown_observed"))

    def test_stop_restores_threshold_without_trim_or_save(self):
        engine, bench = self.diagnostic()
        self.advance(engine, lambda: engine.phase == "test_current")
        start_commands = list(bench.commands)
        sent = bench.can_tx_count
        self.assertTrue(bench._charger_on)
        engine.stop()
        self.assertFalse(bench._load_on)
        self.assertEqual(bench.can_tx_count, sent + bench.cleanup_can_tx_count)
        self.assertEqual(bench.test_parameter_restore_result["status"], "PASS")
        self.assertTrue(bench._charger_on)
        self.assertEqual([c for c in bench.commands if c[0] == "post_test_start"],
                         [c for c in start_commands if c[0] == "post_test_start"])
        self.assertFalse(any(c[0] in {"shutdown", "three_duty_pairs", "save_current", "save_voltage"}
                             for c in bench.commands))
        self.assertIn("AC OFF", engine.stop_outcome)
        self.assertIn("ABORTED", engine.outcome)


if __name__ == "__main__":
    unittest.main()
