"""External AC restart and continuous-load production tests; no hardware."""
from dataclasses import replace
import unittest

from leanwatts_can.automation import AutomationEngine, AutomationSettings, DemoBench
from leanwatts_can.tests.test_baseline import BaselineBench
from leanwatts_can.tests.test_production_sessions import production_recipe
from leanwatts_can.tests.test_recipe_automation import ramp_recipe
from leanwatts_can.production_flow import ProductionFlow
from leanwatts_can.test_programs import validate_program


class TransformBench(DemoBench):
    def __init__(self, recipe):
        super().__init__(recipe)
        self.transform = lambda r: r

    def read(self, now):
        return self.transform(super().read(now))


class ExternalRestartTests(unittest.TestCase):
    def make(self, transform=None, bench_type=TransformBench):
        recipe = ramp_recipe()
        recipe.termination_test["start_current"] = 5
        recipe.termination_test["max_seconds"] = 180
        bench = bench_type(recipe)
        if transform:
            bench.transform = transform
        engine = AutomationEngine(recipe, "UNIT", "Tester", bench=bench, settings=AutomationSettings.demo())
        engine.start_test(now=0)
        return engine, bench

    def advance(self, engine, until=None, limit=2000):
        for _ in range(limit):
            engine.tick(engine._now + .25)
            if not engine.is_running or (until and until()):
                return
        self.fail("Bounded external-restart test did not finish")

    def test_three_acceptance_tests_one_continuous_load_interval_zero_commands(self):
        engine, bench = self.make()
        self.assertEqual(engine.phase, "test_wait_restart")
        self.assertFalse(bench._load_on)
        self.advance(engine)
        self.assertEqual(engine.outcome, "SIMULATION — PASS", engine.detail)
        self.assertEqual([r["name"] for r in engine.results],
                         ["Open-circuit voltage", "Full current", "End current / observed shutdown"])
        names = [c[0] for c in bench.commands]
        on = names.index("load_on")
        off = names.index("load_off", on)
        self.assertNotIn("load_off", names[on:off])
        self.assertEqual(names.count("load_on"), 1)
        self.assertEqual(names.count("load_off"), 2)  # Preparation and final cleanup only.
        self.assertIn(("load_transition_cc", 5), bench.commands)
        self.assertFalse(any("start" in n or "save" in n or "shutdown" in n or "duty" in n for n in names))
        snap = engine.snapshot()
        self.assertTrue(snap["test_cleanup_verified"])
        self.assertEqual(snap["total_can_tx_count"], 0)
        for key in ("startup_can_tx_count", "test_setup_can_tx_count", "cleanup_can_tx_count", "planned_start_count"):
            self.assertEqual(snap[key], 0)
        self.assertFalse(snap["test_parameter_restore_result"]["parameters_changed"])
        self.assertFalse(snap["test_parameter_restore_result"]["restoration_required"])
        self.assertGreaterEqual(engine.results[1]["duration_seconds"], 18)
        self.assertGreaterEqual(engine.results[-1]["confirmed_seconds"], 10)
        self.assertEqual(engine.startup_voltage_result["status"], "PASS")

    def test_ocv_builds_and_settles_before_any_load_engagement(self):
        engine, bench = self.make()
        bench.transform = lambda r: replace(r, voltage=0 if r.sequence == 1 else 50 if r.timestamp < 5 else 58.4, current=0, power=0) if not r.load_on else r
        for n in range(1, 20):
            engine.tick(n * .25)
            self.assertFalse(bench._load_on)
            self.assertEqual(engine.results, [])
        self.advance(engine, lambda: engine.phase == "test_current")
        self.assertTrue(bench._load_on)
        self.assertEqual(engine.results[0]["name"], "Open-circuit voltage")
        self.assertEqual(engine.results[0]["status"], "PASS")
        self.assertAlmostEqual(engine.results[0]["measured"], 58.4)
        self.assertGreaterEqual(engine._now, 6)
        self.advance(engine)
        self.assertEqual(len(engine.results), 3)

    def test_already_high_output_requires_fresh_low_before_engagement(self):
        engine, bench = self.make(lambda r: replace(r, voltage=58.4, current=0, power=0))
        self.advance(engine)
        self.assertIn("ERROR", engine.outcome)
        self.assertGreaterEqual(engine._now, 120)
        self.assertFalse(any(c[0] == "load_on" for c in bench.commands))
        self.assertEqual(engine.results, [])
        self.assertEqual(bench.can_tx_count, 0)

    def test_operator_wait_is_separate_from_fifteen_second_voltage_rise_deadline(self):
        engine, bench = self.make(lambda r: replace(r, voltage=0 if r.timestamp < 70 else 10, current=0, power=0))
        self.advance(engine)
        self.assertIn("15 s startup window", engine.detail)
        self.assertGreaterEqual(engine._now, 85)
        self.assertLess(engine._now, 86)
        self.assertFalse(any(c[0] == "load_on" for c in bench.commands))

    def test_current_ramp_waits_through_early_plateau_and_records_peak_separately(self):
        engine, bench = self.make()
        def build(r):
            if r.load_on and r.load_mode == "CV":
                current = 3 if r.timestamp < 8 else min(15.015, 3 + (r.timestamp-8)*1.5)
                return replace(r, current=current, power=current*r.voltage)
            return r
        bench.transform = build
        self.advance(engine)
        self.assertEqual(engine.results[0]["status"], "PASS")
        self.assertGreaterEqual(engine.results[1]["duration_seconds"], 18)
        self.assertAlmostEqual(engine.results[1]["plateau_current_a"], 15.015, delta=.005)
        self.assertAlmostEqual(engine.results[1]["peak_current_a"], 15.015)

    def test_transition_input_loss_cannot_be_called_cutoff(self):
        class DropsInput(TransformBench):
            def transition_load_to_cc(self, current):
                super().transition_load_to_cc(current)
                self._load_on = False
                return True
        engine, bench = self.make(bench_type=DropsInput)
        self.advance(engine)
        self.assertIn("ERROR", engine.outcome)
        self.assertFalse(engine.termination_evidence.get("shutdown_observed", False))
        self.assertEqual([r["name"] for r in engine.results], ["Open-circuit voltage", "Full current", "End current / observed shutdown"])
        self.assertEqual(bench.can_tx_count, 0)

    def test_cutoff_waits_for_actual_cc_current_and_fails_unfollowed_demand(self):
        engine, bench = self.make(lambda r: replace(r, current=0, power=0) if r.load_mode == "CC" else r)
        self.advance(engine)
        self.assertEqual(engine.outcome, "SIMULATION — FAIL")
        self.assertEqual(engine.results[-1]["status"], "FAIL")
        self.assertIn("not followed", engine.results[-1]["detail"])
        self.assertFalse(engine.termination_evidence["shutdown_observed"])

    def test_relay_restart_during_off_observation_fails(self):
        engine, bench = self.make()
        low_at = [None]
        def chatter(r):
            if bench._terminated:
                low_at[0] = r.timestamp if low_at[0] is None else low_at[0]
                if r.timestamp - low_at[0] >= 3:
                    return replace(r, voltage=58.4, current=bench._setpoint, power=58.4*bench._setpoint)
            return r
        bench.transform = chatter
        self.advance(engine)
        self.assertEqual(engine.results[-1]["status"], "FAIL")
        self.assertTrue(engine.termination_evidence["restart_observed"])

    def test_stop_never_writes_charger_and_cleans_load(self):
        engine, bench = self.make()
        self.advance(engine, lambda: engine.phase == "test_current")
        engine.stop()
        self.assertIn("ABORTED", engine.outcome)
        self.assertEqual(bench.can_tx_count, 0)
        self.assertFalse(bench._load_on)
        self.assertTrue(engine.snapshot()["test_cleanup_verified"])
        self.assertIn("AC OFF", engine.stop_outcome)

    def test_unexpected_application_transmit_invalidates_test(self):
        engine, bench = self.make()
        bench.can_tx_count += 1
        engine.tick(.25)
        self.assertIn("ERROR", engine.outcome)
        self.assertFalse(engine.snapshot()["test_cleanup_verified"])

    def test_startup_battery_indication_can_clear_with_load_without_polling(self):
        engine, bench = self.make()
        bench.transform = lambda r: replace(r, charger_fault_word=0 if r.load_on else 0x20,
                                            charger_fault_observed_at=r.timestamp)
        self.advance(engine)
        self.assertEqual(engine.outcome, "SIMULATION — PASS", engine.detail)
        self.assertEqual(bench.can_tx_count, 0)

    def test_baseline_voltage_to_current_has_no_intermediate_input_off(self):
        recipe = production_recipe()
        bench = BaselineBench(recipe)
        engine = AutomationEngine(recipe, "UNIT", "Tester", bench=bench,
            settings=AutomationSettings.demo(), baseline_required=True)
        engine.start_calibration(now=0)
        self.advance(engine, lambda: engine.baseline_result["current"].get("status") == "PASS")
        names = [c[0] for c in bench.commands]
        on = names.index("load_on")
        self.assertNotIn("load_off", names[on:])
        self.assertEqual(names[-1], "shutdown")  # Baseline has finished; programming boundary follows.
        engine.stop()

    def test_retired_methods_cannot_execute_from_program_files(self):
        for method in ("normal", "calibration_start_only"):
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, "retired"):
                validate_program(dict(schema=1, name="Old program", description="Preserved station program",
                    workflow="test_only", testing=dict(start_method=method)))

    def test_current_waiting_strategy_handles_long_ramp_and_seventeen_second_plateau(self):
        for baseline in (False, True):
            for delayed_plateau in (False, True):
                with self.subTest(baseline=baseline, delayed_plateau=delayed_plateau):
                    if baseline:
                        recipe = production_recipe()
                        bench = BaselineBench(recipe)
                        engine = AutomationEngine(recipe, "UNIT", "Tester", bench=bench,
                            settings=AutomationSettings.demo(), baseline_required=True)
                        engine.start_calibration(now=0)
                        self.advance(engine, lambda: engine.phase == "baseline_current")
                    else:
                        engine, bench = self.make()
                        self.advance(engine, lambda: engine.phase == "test_current")
                    started = engine._phase_started
                    def transformed(r):
                        if r.load_on and r.load_mode == "CV":
                            elapsed = r.timestamp - started
                            current = (8 if elapsed < 17.5 else min(15.015, 8 + elapsed - 17.5)) if delayed_plateau else min(15.015, 15.015 * elapsed / 30)
                            return replace(r, current=current, power=r.voltage*current)
                        return r
                    bench.transform = (lambda r, b: transformed(r)) if baseline else transformed
                    for _ in range(72):
                        engine.tick(engine._now + .25)
                        if baseline:
                            self.assertEqual(engine.baseline_result["current"]["status"], "RUNNING")
                        else:
                            self.assertEqual([r["name"] for r in engine.results], ["Open-circuit voltage"])
                    self.advance(engine, lambda: engine.baseline_result["current"].get("status") == "PASS" if baseline
                                 else any(r["name"] == "Full current" for r in engine.results))
                    record = engine.baseline_result["current"] if baseline else engine.results[1]
                    self.assertGreaterEqual(record["duration_seconds"], 27 if delayed_plateau else 32.5)
                    self.assertAlmostEqual(record["measured"], 15.015, delta=.01)
                    engine.stop()

    def test_stop_cleanup_failure_is_prominent_in_flow_summary(self):
        class CleanupFailure(DemoBench):
            def finish_test(self):
                self.test_parameter_restore_result = dict(status="HOLD", parameters_changed=False,
                    restoration_required=False, restored=None, error="Load OFF readback failed")
                return False
        recipe = production_recipe()
        bench = CleanupFailure(recipe)
        bench.capabilities = replace(bench.capabilities, simulation=False)
        flow = ProductionFlow(recipe, "WO-S00430-1-001", "Tester", 1, None,
            lambda: (bench, AutomationSettings.demo()), run_calibration=False)
        flow.start()
        self.assertTrue(flow.is_running, flow.detail)
        flow.stop()
        self.assertEqual(flow.unit_result["outcome"], "STOPPED")
        self.assertIn("HOLD", flow.unit_result["detail"])
        self.assertIn("Load OFF readback failed", flow.unit_result["detail"])
        row = next(r for r in flow.unit_result["stage_details"] if r["name"] == "Test cleanup")
        self.assertEqual(row["status"], "HOLD")


if __name__ == "__main__":
    unittest.main()
