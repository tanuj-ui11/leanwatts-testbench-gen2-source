"""Exercise reference starts through real workflow code with fake instruments."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from leanwatts_can.automation import AutomationSettings
from leanwatts_can.production_flow import ProductionFlow
from leanwatts_can.session_seed import select_session_seed
from leanwatts_can.session_store import SessionStore
from leanwatts_can.tests.test_production_sessions import production_recipe
from leanwatts_can.tests.test_release_113 import CurveBench


class SessionSeedFlowTests(unittest.TestCase):
    def test_first_then_second_physical_workflow_uses_fixed_reference_and_records_it(self):
        recipe = production_recipe()
        recipe.cc_current, recipe.end_voltage = 20, 57.3
        recipe.max_current, recipe.max_power = 30, 2000
        settings = replace(AutomationSettings.demo(), current_min=60, current_max=85,
            voltage_min=60, voltage_max=85, auto_direction=True,
            current_direction=0, voltage_direction=0)
        operator = dict(id="OP", name="Operator", role="operator", enabled=True)
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(folder)
            store.start(operator)
            starts = []
            for index in range(3):
                serial = f"UNIT-{index}"
                seed = select_session_seed(store.active(), recipe, 1)
                attempt = store.begin_unit(serial, recipe, 1, run_tests=False, serial_override=True)
                bench = CurveBench(recipe)
                bench.settings = settings
                bench.capabilities = replace(bench.capabilities, simulation=False)
                bench.preflight_result = dict(status="PASS")
                bench.prepare_charger_parameters = lambda: None
                flow = ProductionFlow(recipe, serial, operator, 1, lambda: (bench, settings),
                    None, run_tests=False, starting_point=seed, serial_override=True)
                clock = [100.0]
                with patch("time.monotonic", lambda: clock[0]):
                    flow.start()
                    for _ in range(1000):
                        clock[0] += .25
                        flow.tick()
                        if not flow.is_running:
                            break
                result = flow.unit_result
                self.assertEqual(result["outcome"], "CALIBRATED", result)
                point = result["starting_point"]
                starts.append((point["current_initial"], point["voltage_initial"]))
                sent = next(c for c in bench.commands if c[0] == "calibration_start_and_three_duty_pairs")
                self.assertEqual(sent[1:], (point["voltage_initial"], point["current_initial"]))
                self.assertEqual(bench.settings.current_initial, point["current_initial"])
                self.assertEqual(settings.current_initial, 80)
                store.finish_unit(attempt, result)
                if index == 0:
                    self.assertEqual(point["method"], "configured")
                    reference = result["calibration"]
                else:
                    self.assertEqual(point["method"], "session_reference")
                    self.assertEqual(point["source_serial"], "UNIT-0")
                    self.assertEqual(point["current_initial"], min(settings.current_max, round(reference["current_duty"] + 2, 1)))
                    self.assertEqual(point["voltage_initial"], min(settings.voltage_max, round(reference["voltage_duty"] + 2, 1)))
            self.assertEqual(starts[1], starts[2])
            self.assertNotEqual(starts[0], starts[1])
            paths = store.active()["report_paths"]
            for suffix in ("csv", "html"):
                self.assertIn("Session reference UNIT-0 +2", Path(paths[suffix]).read_text(encoding="utf-8-sig"))
            reloaded = SessionStore(folder)
            self.assertEqual(select_session_seed(reloaded.active(), recipe, 1)["source_serial"], "UNIT-0")
            reloaded.close()
            reloaded.start(operator)
            self.assertIsNone(select_session_seed(reloaded.active(), recipe, 1))


if __name__ == "__main__":
    unittest.main()
