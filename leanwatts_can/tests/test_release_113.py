"""Regression checks with isolated records and deterministic instruments only."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from leanwatts_can.automation import AutomationEngine, AutomationSettings, DemoBench
from leanwatts_can.production_flow import ProductionFlow
from leanwatts_can.session_store import SessionStore
from leanwatts_can.tests.test_production_sessions import production_recipe


class CurveBench(DemoBench):
    def read(self, now):
        r = super().read(now)
        if self._charger_on and self._load_on:
            if self._mode == "CV":
                r = replace(r, current=23.06 + (self.current_duty - 80) * .28)
            else:
                r = replace(r, voltage=55.2 + (self.voltage_duty - 80) * .65)
            r = replace(r, power=r.voltage * r.current)
        return r


class Release113Tests(unittest.TestCase):
    def run_curve(self, cap):
        r = production_recipe()
        r.cc_current, r.end_voltage = 20, 57.3
        r.max_current, r.max_power = 30, 2000
        settings = replace(AutomationSettings.demo(), current_min=60, current_max=85,
            voltage_min=60, voltage_max=85, current_direction=0, voltage_direction=0,
            auto_direction=True, max_current_coarse_step=cap, stable_samples=8,
            stable_seconds=2, tuning_seconds=.6, settling_seconds=.6)
        engine = AutomationEngine(r, "SERIAL", "TEST", bench=CurveBench(r), settings=settings)
        engine.start_calibration(now=0)
        for n in range(1, 1000):
            engine.tick(n * .25)
            if not engine.is_running:
                break
        self.assertIn("CALIBRATED", engine.outcome, engine.detail)
        return engine

    def test_current_uses_fewer_adjustments_and_voltage_moves_closer_to_573(self):
        slow, fast = self.run_curve(1), self.run_curve(3)
        count = lambda e: sum("Current duty" in item for item in e.events)
        self.assertLess(count(fast), count(slow))
        self.assertLess(fast.snapshot()["elapsed_seconds"], slow.snapshot()["elapsed_seconds"])
        final = next(x for x in fast.results if x["name"] == "Voltage calibration")
        self.assertLess(abs(final["measured"] - 57.3), .05)
        self.assertTrue(any("target refinement" in x.lower() for x in fast.events) or fast._refinement_count <= 2)
        self.assertEqual(sum(c[0] == "save_voltage" for c in fast.bench.commands), 1)
        last = 80
        for name, *args in fast.bench.commands:
            if name == "three_duty_pairs":
                self.assertLessEqual(abs(args[1] - last), 3.000001)
                self.assertTrue(60 <= args[1] <= 85)
                last = args[1]

    def test_refinement_has_small_command_budget_and_cannot_save_short_window(self):
        e = self.run_curve(3)
        voltage = [s for s in e.samples if s["phase"] == "cal_voltage"]
        final_duty = voltage[-1]["voltage_duty"]
        confirm = [s for s in voltage if s["voltage_duty"] == final_duty]
        self.assertGreaterEqual(len(confirm), 8)
        self.assertGreaterEqual(confirm[-1]["timestamp"] - confirm[0]["timestamp"], 2)
        e.operation, e.target, e.tolerance = "calibration", 57.3, .1
        e.voltage_duty = 81
        e.trim_directions["voltage"] = 1
        e.output_per_duty["voltage"] = .65
        e._refinement_count, e._refinement_deadline, e._reversals = 0, None, 0
        self.assertTrue(e._can_refine_voltage("voltage", 57.21))
        self.assertFalse(e._can_refine_voltage("voltage", 57.28))
        e._refinement_count = 2
        self.assertFalse(e._can_refine_voltage("voltage", 57.21))
        e._refinement_count, e._refinement_deadline = 0, e._now
        self.assertFalse(e._can_refine_voltage("voltage", 57.21))

    def test_large_current_steps_need_two_responses_and_honour_station_caps(self):
        e = self.run_curve(3)
        e.operation, e.target, e.tolerance, e.unit = "calibration", 20, .1, "A"
        e.current_duty, e._adjustments, e._reversals, e._last_error_sign = 80, 0, 0, 0
        e.trim_directions["current"], e.output_per_duty["current"] = 1, .3
        e._window = [(0, 23), (1, 23), (2, 23)]
        e._gain_observations["current"] = 1
        e._adjust_axis("current", 23)
        self.assertEqual(e.current_duty, 79)
        e._window = [(3, 22.7), (4, 22.7), (5, 22.7)]
        e._gain_observations["current"] = 2
        e._adjust_axis("current", 22.7)
        self.assertEqual(e.current_duty, 76)
        e.settings = replace(e.settings, max_duty_step=.5)
        e._window = [(6, 22), (7, 22), (8, 22)]
        e._adjust_axis("current", 22)
        self.assertEqual(e.current_duty, 75.5)

    def test_no_output_test_times_out_without_any_can_start(self):
        # 1.1.9+: tests start by operator AC restart; the app never sends CAN Start.
        r = production_recipe()
        b = DemoBench(r)
        original = b.read
        b.read = lambda now: replace(original(now), voltage=0, current=0, power=0, unregulated=True)
        e = AutomationEngine(r, "S", "TEST", bench=b, settings=AutomationSettings.demo())
        e.start_test(now=0)
        for n in range(1, 1000):
            e.tick(n * .25)
            if not e.is_running:
                break
        self.assertFalse(e.is_running)
        self.assertIn("External restart timed out", e.detail)
        self.assertEqual(e.snapshot()["startup_can_tx_count"], 0)
        self.assertEqual(e.snapshot()["total_can_tx_count"], 0)
        self.assertEqual(e.results, [])
        self.assertFalse(b._load_on)

    def test_calibration_only_never_creates_test_and_persists_not_run(self):
        r = production_recipe()
        r.termination_test = {"enabled": False}
        b = DemoBench(r)
        b.capabilities = replace(b.capabilities, simulation=False)
        b.prepare_charger_parameters = lambda: None
        b.preflight_result = {"status": "PASS"}
        test_factory = Mock(side_effect=AssertionError("Post-test must not open"))
        clock = [100.0]
        flow = ProductionFlow(r, "S", "Tester", 1, lambda: (b, AutomationSettings.demo()),
                              test_factory, run_tests=False, serial_override=True)
        with patch("time.monotonic", lambda: clock[0]):
            flow.start()
            for _ in range(400):
                clock[0] += .25
                flow.tick()
                if not flow.is_running:
                    break
        result = flow.unit_result
        self.assertEqual(result["outcome"], "CALIBRATED", result)
        self.assertEqual(result["test_result"], "NOT RUN")
        test_factory.assert_not_called()
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(folder)
            session = store.start(dict(id="A", name="Tester", role="operator", enabled=True))
            attempt = store.begin_unit("S", r, 1, run_tests=False, serial_override=True)
            with self.assertRaises(ValueError):
                store.finish_unit(attempt, dict(result, outcome="PASS", test_result="PASS"))
            store.finish_unit(attempt, result)
            self.assertEqual(SessionStore(folder).active()["units"][0]["outcome"], "CALIBRATED")
            for ext in ("html", "csv"):
                text = Path(store.report_paths(session["session_id"])[ext]).read_text(encoding="utf-8-sig")
                self.assertIn("CALIBRATED", text)
                self.assertIn("NOT RUN", text)


if __name__ == "__main__":
    unittest.main()
