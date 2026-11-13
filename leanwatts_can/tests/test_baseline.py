"""Pre-calibration baseline checks with deterministic instruments only."""
import unittest
from dataclasses import replace
from unittest.mock import patch

from leanwatts_can.automation import AutomationEngine, AutomationSettings, DemoBench
from leanwatts_can.bench_session import BenchSession, BenchSessionError
from leanwatts_can.protocol import command
from leanwatts_can.tests.test_bench_session import FakeCan, FakeEvent, FakeLoad, Station
from leanwatts_can.tests.test_production_sessions import production_recipe


class BaselineBench(DemoBench):
    def __init__(self, recipe):
        super().__init__(recipe)
        self.transform = lambda reading, bench: reading
        self.preflight_result = None
        self.polls = 0

    def prepare_charger_parameters(self):
        self.commands.append(("prepare_parameters",))
        self.preflight_result = {"status": "PASS"}

    def request_charger_diagnostics(self):
        self.polls += 1
        self.commands.append(("diagnostic_poll",))
        return True

    def read(self, now):
        return self.transform(super().read(now), self)


class BaselineTests(unittest.TestCase):
    def make(self, transform=None, **options):
        r = production_recipe()
        b = BaselineBench(r)
        if transform is not None:
            b.transform = transform
        e = AutomationEngine(r, "UNIT", "Tester", bench=b,
            settings=AutomationSettings.demo(), baseline_required=options.pop("required", True), **options)
        e.start_calibration(now=0)
        return e, b

    def advance(self, e, stop_when=None, limit=600):
        for _ in range(limit):
            e.tick(e._now + .25)
            if not e.is_running or (stop_when is not None and stop_when(e)):
                return
        self.fail("Workflow did not reach the requested state")

    def assert_before_programming(self, b):
        forbidden = {"prepare_parameters", "calibration_start_and_three_duty_pairs", "three_duty_pairs", "save_current", "save_voltage"}
        self.assertFalse(any(c[0] in forbidden for c in b.commands), b.commands)

    def test_baseline_reads_original_output_before_parameters_and_any_duty_frames(self):
        e, b = self.make()
        self.assertEqual(e.phase, "baseline_open_voltage")
        self.assert_before_programming(b)
        self.advance(e)
        self.assertIn("CALIBRATED", e.outcome, e.detail)
        baseline = e.snapshot()["baseline_result"]
        self.assertEqual(baseline["status"], "PASS")
        self.assertAlmostEqual(baseline["open_voltage"]["measured"], 58.2)
        self.assertAlmostEqual(baseline["current"]["measured"], 14.8)
        self.assertEqual(baseline["current"]["load_setpoint"], 49)
        self.assertEqual(baseline["open_voltage"]["load_mode"], "OFF")
        names = [c[0] for c in b.commands]
        self.assertEqual(names.count("baseline_start"), 1)
        self.assertLess(names.index("baseline_start"), names.index("prepare_parameters"))
        self.assertLess(names.index("shutdown"), names.index("prepare_parameters"))
        self.assertLess(names.index("prepare_parameters"), names.index("calibration_start_and_three_duty_pairs"))
        self.assertEqual([r["name"] for r in e.results], ["Current calibration", "Voltage calibration"])
        self.assertEqual([s["name"] for s in e.stage_details[:2]], ["Baseline open voltage", "Baseline current"])
        self.assertGreaterEqual(len([s for s in e.samples if s["phase"] == "off_baseline"]), 3)

    def test_baseline_optional_default_keeps_previous_sequence(self):
        e, b = self.make(required=False)
        self.advance(e)
        self.assertEqual(e.snapshot()["baseline_result"]["status"], "NOT RUN")
        self.assertFalse(any(c[0] == "baseline_start" for c in b.commands))
        self.assertEqual(e.results[0]["name"], "Current calibration")

    def test_zero_output_stops_without_programming_or_calibration(self):
        e, b = self.make(lambda r, b: replace(r, voltage=0, current=0, power=0))
        self.advance(e)
        self.assertIn("Baseline unavailable", e.detail)
        self.assertLessEqual(e._now, 15.25)
        self.assertEqual(e.baseline_result["status"], "FAIL")
        self.assertIsNone(e.baseline_result["open_voltage"]["measured"])
        self.assertEqual(e.baseline_result["current"]["status"], "NOT RUN")
        self.assert_before_programming(b)
        self.assertFalse(b._owned)
        self.assertFalse(b._charger_on)

    def test_voltage_present_but_zero_current_is_not_a_valid_baseline(self):
        e, b = self.make(lambda r, b: replace(r, current=0, power=0) if b._load_on and b._baseline_normal else r)
        self.advance(e)
        self.assertEqual(e.baseline_result["open_voltage"]["status"], "PASS")
        self.assertEqual(e.baseline_result["current"]["status"], "FAIL")
        self.assertIsNone(e.baseline_result["current"]["measured"])
        self.assert_before_programming(b)

    def test_open_voltage_can_ignore_unreg_but_loaded_current_cannot(self):
        e, b = self.make(lambda r, b: replace(r, unregulated=True) if b._baseline_normal else r)
        self.advance(e)
        self.assertEqual(e.baseline_result["open_voltage"]["status"], "PASS")
        self.assertEqual(e.baseline_result["current"]["status"], "FAIL")
        self.assert_before_programming(b)

    def test_baseline_records_out_of_target_values_without_acceptance_claim(self):
        def transformed(r, b):
            if b._baseline_normal:
                return replace(r, voltage=49 if r.load_on else 55, current=10 if r.load_on else 0,
                               power=490 if r.load_on else 0)
            return r
        e, b = self.make(transformed)
        self.advance(e, lambda e: e.baseline_result["status"] == "PASS")
        self.assertEqual(e.baseline_result["open_voltage"]["measured"], 55)
        self.assertEqual(e.baseline_result["current"]["measured"], 10)
        self.assertIn("not a final acceptance", e.baseline_result["current"]["detail"])
        e.stop()

    def test_initial_open_voltage_input_or_current_violation_stops(self):
        for change in ({"load_on": True}, {"current": .2}):
            with self.subTest(change=change):
                e, b = self.make(lambda r, b: replace(r, **change) if b._baseline_normal else r)
                e.tick(.25)
                self.assertFalse(e.is_running)
                self.assertEqual(e.baseline_result["status"], "FAIL")
                self.assert_before_programming(b)

    def test_fault_limit_stale_and_bad_reference_still_abort(self):
        cases = ({"voltage": 66}, {"current": 21}, {"power": 1201},
                 {"timestamp": -10}, {"source": "different reference"}, {"fault": "OTP"},
                 {"charger_fault_word": 0x21, "charger_fault_observed_at": .25})
        for change in cases:
            with self.subTest(change=change):
                e, b = self.make(lambda r, b: replace(r, **change) if b._baseline_normal else r)
                e.tick(.25)
                self.assertFalse(e.is_running, change)
                self.assertEqual(e.baseline_result["status"], "FAIL")
                self.assert_before_programming(b)

    def test_duplicate_sequences_never_satisfy_baseline_and_deadline_still_expires(self):
        e, b = self.make(lambda r, b: replace(r, sequence=1))
        self.advance(e)
        self.assertIn("Baseline unavailable", e.detail)
        self.assertEqual(len([s for s in e.samples if s["phase"] == "baseline_open_voltage"]), 1)
        self.assert_before_programming(b)

    def test_battery_absent_waits_for_fresh_clear_and_never_uses_ageout(self):
        def word(r, b):
            if not b._baseline_normal:
                return r
            return replace(r, charger_fault_word=0x20, charger_fault_observed_at=.25)
        e, b = self.make(word)
        for n in range(1, 12):
            e.tick(n * .25)
        self.assertEqual(e.stable_count, 0)
        self.assertGreater(b.polls, 0)
        self.assert_before_programming(b)
        b.transform = lambda r, b: replace(r, charger_fault_word=0, charger_fault_observed_at=r.timestamp)
        self.advance(e, lambda e: e.baseline_result["status"] == "PASS")
        self.assertEqual(e.baseline_result["status"], "PASS")
        e.stop()
        e, b = self.make(word)
        self.advance(e)
        self.assertEqual(e.baseline_result["status"], "FAIL")
        self.assertLessEqual(e._now, 15.25)
        self.assert_before_programming(b)

    def test_late_battery_fault_after_measurement_starts_is_not_hidden(self):
        e, b = self.make()
        e.tick(.25)
        b.transform = lambda r, b: replace(r, charger_fault_word=0x20, charger_fault_observed_at=r.timestamp)
        e.tick(.5)
        self.assertFalse(e.is_running)
        self.assertIn("0x0020", e.detail)
        self.assert_before_programming(b)

    def test_cancelled_baseline_shuts_down_and_never_programs(self):
        e, b = self.make()
        e.tick(.25)
        e.stop()
        self.assertIn("ABORTED", e.outcome)
        self.assertEqual(e.baseline_result["status"], "FAIL")
        self.assertFalse(b._charger_on)
        self.assertFalse(b._owned)
        self.assert_before_programming(b)

    def test_unconfirmed_baseline_shutdown_blocks_parameter_preparation(self):
        e, b = self.make()
        original = b.charger_shutdown
        def retained():
            accepted = original()
            b._charger_on = b._baseline_normal = True
            return accepted
        b.charger_shutdown = retained
        self.advance(e)
        self.assertEqual(e.baseline_result["current"]["status"], "PASS")
        self.assertEqual(e.baseline_result["status"], "FAIL")
        self.assert_before_programming(b)


class BaselineAdapterTests(unittest.TestCase):
    def make(self, operation="calibration"):
        s = Station()
        b = BenchSession(lambda: FakeLoad(s), monitor_factory=lambda: FakeCan(s),
            settings=AutomationSettings.demo(), protocol_approved=True, cc_mode_code=10, cv_mode_code=20,
            exclusive_confirmed=True, standalone_ready=True, clock=lambda: s.now, cancellation_event=FakeEvent())
        b.probe()
        b.acquire(operation)
        self.addCleanup(b.release)
        return s, b

    def test_baseline_start_is_one_normal_start_no_trims_and_keeps_shutdown_control(self):
        s, b = self.make()
        b.load_off()
        b.charger_start_baseline()
        self.assertEqual([c[1] for c in s.calls if c[0] == "can.send"], [command("workStart")])
        self.assertFalse(b._passive)
        self.assertFalse(b._started)
        with self.assertRaises(BenchSessionError):
            b.charger_apply_duties(80, 80)
        with self.assertRaises(BenchSessionError):
            b.charger_save("current")
        with self.assertRaises(BenchSessionError):
            b.charger_start_baseline()
        b.request_charger_diagnostics()
        b.charger_shutdown()
        self.assertEqual([c[1] for c in s.calls if c[0] == "can.send"][-1], command("shutdown"))

    def test_baseline_start_requires_off_input_and_calibration_ownership(self):
        s, b = self.make()
        s.input_on = True
        with self.assertRaises(BenchSessionError):
            b.charger_start_baseline()
        self.assertEqual([c for c in s.calls if c[0] == "can.send"], [])
        b.release()
        s, b = self.make("test")
        with self.assertRaises(BenchSessionError):
            b.charger_start_baseline()
        self.assertEqual([c for c in s.calls if c[0] == "can.send"], [])


if __name__ == "__main__":
    unittest.main()
