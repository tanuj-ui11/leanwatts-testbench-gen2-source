"""1.1.9 regressions: delayed ramp, normal tests, restoration and relay chatter.

No hardware. Captured baseline points are from cycle-20261112T104122Z-5fe36921;
load-readings.csv SHA256 d40e7e102bc347b159b7eb35b6ef334673a243af027b3e4339beb9909e412a9c.
The synthetic continuation explicitly tests an unobserved later ramp, not a
claim that this occurred in the supplied trace.
"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from leanwatts_can.automation import AutomationEngine, AutomationSettings, DemoBench
from leanwatts_can.test_programs import DEFAULT_FILENAME, ensure_programs, list_programs
from leanwatts_can.tests.test_baseline import BaselineBench
from leanwatts_can.tests.test_production_sessions import production_recipe
from leanwatts_can.tests.test_recipe_automation import ramp_recipe
from leanwatts_can.production_flow import ProductionFlow
from leanwatts_can.protocol import poll
from leanwatts_can.tests import test_bench_trace_program as bench_fixtures
from leanwatts_can.bench_session import BenchSessionError


CAPTURED_CURRENT = [(0, 2.4534), (.516, 3.4107), (.984, 4.3621), (1.516, 5.3501),
    (2.016, 6.3018), (2.531, 7.2868), (3.031, 8.2319), (3.516, 9.1914),
    (4.031, 10.1787), (4.562, 11.1151), (5.031, 12.0754), (5.547, 13.0643),
    (6.047, 14.0079), (6.531, 14.9828), (7.047, 15.9519), (7.547, 16.7171),
    (8.047, 17.8388), (8.531, 18.4931), (9.047, 18.5146), (9.531, 18.5136),
    (10.031, 18.5134), (10.547, 18.5127), (11.031, 18.5124), (11.562, 18.512)]


class NormalTestPipelineTests(unittest.TestCase):
    def advance(self, engine, until=None, limit=2000):
        for _ in range(limit):
            engine.tick(engine._now + .25)
            if not engine.is_running or (until and until()):
                return
        self.fail("Bounded workflow did not finish")


    def test_captured_early_plateau_waits_for_later_settled_baseline(self):
        recipe = production_recipe()
        bench = BaselineBench(recipe)
        engine = AutomationEngine(recipe, "UNIT", "Tester", bench=bench,
            settings=AutomationSettings.demo(), baseline_required=True)
        engine.start_calibration(now=0)
        engine.tick(.25)  # Immediate threshold-triggered load ON.
        current = [0.0]
        bench.transform = lambda r, b: replace(r, current=current[0], power=r.voltage*current[0])
        for timestamp, value in CAPTURED_CURRENT:
            current[0] = value
            engine.tick(.5 + timestamp)
            self.assertEqual(engine.baseline_result["current"]["status"], "RUNNING")
        self.assertFalse(any(c[0] == "prepare_parameters" for c in bench.commands))
        # A later rise is synthetic evidence that the early plateau cannot win.
        while engine._now < 17:
            current[0] = min(19.5, 18.512 + (engine._now - 12) * .2)
            engine.tick(engine._now + .25)
        current[0] = 19.5
        self.advance(engine, lambda: engine.baseline_result["current"]["status"] == "PASS")
        self.assertAlmostEqual(engine.baseline_result["current"]["measured"], 19.5, delta=.01)
        self.assertGreaterEqual(engine.baseline_result["current"]["duration_seconds"], 19.5)
        engine.stop()

    def test_slow_rising_current_is_not_a_settled_baseline(self):
        recipe = production_recipe()
        bench = BaselineBench(recipe)
        engine = AutomationEngine(recipe, "UNIT", "Tester", bench=bench,
            settings=AutomationSettings.demo(), baseline_required=True)
        engine.start_calibration(now=0)
        engine.tick(.25)
        bench.transform = lambda r, b: replace(r, current=10 + .01*r.timestamp,
                                               power=r.voltage*(10 + .01*r.timestamp))
        self.advance(engine)
        self.assertEqual(engine.baseline_result["current"]["status"], "FAIL")
        self.assertFalse(any(c[0] == "prepare_parameters" for c in bench.commands))





    def test_retired_program_is_preserved_but_cannot_run(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "01-full-cycle-calibration-mode.json")
            raw = json.dumps(dict(schema=1, name="Old", description="Old edited station file",
                                  workflow="full_cycle", testing=dict(start_method="calibration_start_only")))
            path.write_text(raw, encoding="utf-8")
            ensure_programs(folder)
            entries = list_programs(folder)
            self.assertEqual(path.read_text(encoding="utf-8"), raw)
            self.assertIn("retired", entries[0]["error"])
            self.assertIsNone(entries[0]["program"])
            self.assertEqual(entries[1]["name"], DEFAULT_FILENAME)
            self.assertEqual(entries[1]["program"]["testing"]["start_method"], "external_restart")

    def test_direct_retired_bench_rejected_before_ownership(self):
        recipe = ramp_recipe()
        bench = DemoBench(recipe, post_test_can_start=True, test_start_method="calibration_start_only")
        engine = AutomationEngine(recipe, "UNIT", "Tester", bench=bench)
        with self.assertRaisesRegex(ValueError, "retired"):
            engine.start_test(now=0)
        self.assertEqual(bench.commands, [])





if __name__ == "__main__":
    unittest.main()
