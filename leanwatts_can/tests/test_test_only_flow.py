"""Test-only workflow never updates charger parameters or calibration."""
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from leanwatts_can.automation import AutomationSettings, DemoBench
from leanwatts_can.production_flow import ProductionFlow, validate_test_recipe
from leanwatts_can.tests.test_production_sessions import production_recipe


class TestOnlyFlowTests(unittest.TestCase):
    def make(self, simulation=False, post_test_can_start=False):
        recipe = production_recipe()
        recipe.device_settings = {}
        # Tests use external_restart: the operator restarts AC and the bench
        # sends zero application CAN commands (controller ACK stays enabled).
        bench = DemoBench(recipe, post_test_can_start=post_test_can_start)
        bench.capabilities = replace(bench.capabilities, simulation=simulation)
        forbidden = Mock(side_effect=AssertionError("Calibration factory must not run"))
        settings = replace(AutomationSettings.demo(), commissioned=False, current_min=0, current_max=0,
                           voltage_min=0, voltage_max=0, current_direction=0, voltage_direction=0)
        flow = ProductionFlow(recipe, "TEST-ONLY", "Operator", 1, forbidden,
            lambda: (bench, settings), run_calibration=False, run_tests=True,
            starting_point={"method": "session_reference", "current_duty": 70, "voltage_duty": 81}, serial_override=True)
        return flow, bench, forbidden

    def finish(self, flow):
        clock = [100.0]
        with patch("time.monotonic", lambda: clock[0]):
            flow.start()
            for _ in range(3000):
                clock[0] += .25
                flow.tick()
                if not flow.is_running:
                    return
        self.fail("Test-only sequence did not terminate")

    def test_real_test_only_can_pass_without_calibration_or_complete_parameter_map(self):
        flow, bench, forbidden = self.make()
        self.assertEqual(validate_test_recipe(flow.recipe), "")
        self.assertTrue(flow.recipe.validate_production())
        self.finish(flow)
        result = flow.unit_result
        self.assertEqual(result["outcome"], "PASS", result)
        self.assertEqual(result["calibration_result"], "NOT RUN")
        self.assertEqual(result["test_result"], "PASS")
        self.assertEqual(result["workflow_mode"], "test_only")
        self.assertFalse(result["run_calibration"])
        self.assertIsNone(result["starting_point"])
        self.assertIsNone(result["baseline_result"])
        forbidden.assert_not_called()
        names = [c[0] for c in bench.commands]
        self.assertFalse(any("save" in x or "duty" in x or "config" in x or "calibration" in x for x in names))
        self.assertEqual(result["test_start_method"], "external_restart")
        self.assertEqual(result["test"]["startup_can_tx_count"], 0)
        self.assertEqual(result["test"]["can_tx_count"], 0)
        self.assertEqual(bench.can_tx_count, 0)
        self.assertFalse(any(c[0] in ("post_test_start", "test_start_voltage", "test_shutdown", "shutdown")
                             for c in bench.commands))
        self.assertEqual([s["status"] for s in result["stage_details"] if s["name"] in
                         ("Charger parameters", "Current calibration", "Voltage calibration")], ["NOT RUN"] * 3)

    def test_simulation_is_never_a_production_pass(self):
        flow, _, forbidden = self.make(simulation=True)
        self.finish(flow)
        self.assertEqual(flow.unit_result["outcome"], "SIMULATION")
        self.assertEqual(flow.unit_result["calibration_result"], "NOT RUN")
        forbidden.assert_not_called()

    def test_retired_can_start_test_connection_is_rejected(self):
        flow, bench, forbidden = self.make(post_test_can_start=True)
        flow.start()
        self.assertEqual(flow.unit_result["outcome"], "FAIL")
        self.assertIn("command-free", flow.unit_result["detail"])
        self.assertEqual(flow.unit_result["test_result"], "NOT RUN")
        self.assertEqual(bench.commands, [])
        self.assertEqual(bench.can_tx_count, 0)
        forbidden.assert_not_called()

    def test_connection_failure_keeps_calibration_not_run(self):
        flow, _, forbidden = self.make()
        flow.test_factory = Mock(side_effect=RuntimeError("Load connection failed"))
        flow.start()
        self.assertEqual(flow.unit_result["outcome"], "FAIL")
        self.assertEqual(flow.unit_result["calibration_result"], "NOT RUN")
        self.assertEqual(flow.unit_result["test_result"], "NOT RUN")
        forbidden.assert_not_called()

    def test_tests_require_cutoff_and_no_empty_workflow(self):
        flow, _, _ = self.make()
        flow.recipe.termination_test = {"enabled": False}
        with self.assertRaisesRegex(ValueError, "cutoff"):
            flow.start()
        with self.assertRaises(ValueError):
            ProductionFlow(production_recipe(), "S", "O", 1, None, None,
                           run_calibration=False, run_tests=False, serial_override=True)

    def test_stop_never_calls_calibration_shutdown(self):
        flow, bench, forbidden = self.make()
        flow.start()
        flow.stop()
        self.assertEqual(flow.unit_result["outcome"], "STOPPED")
        self.assertEqual(flow.unit_result["calibration_result"], "NOT RUN")
        self.assertFalse(bench._load_on)
        self.assertFalse(any(c[0] == "shutdown" for c in bench.commands))
        forbidden.assert_not_called()


if __name__ == "__main__":
    unittest.main()
