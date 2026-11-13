"""Full-program orchestration and UI ownership; real transports are forbidden."""
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from leanwatts_can.automation import AutomationSettings, DemoBench
from leanwatts_can.gui import CalibrationWindow
from leanwatts_can.production_flow import ProductionFlow
from leanwatts_can.recipe_page import atomic_json
from leanwatts_can.test_programs import load_program
from leanwatts_can.tests.test_baseline import BaselineBench
from leanwatts_can.tests.test_production_sessions import production_recipe


def program_entry():
    return load_program(Path(__file__).parents[1] / "test_programs/04-ocv-current-fixed-cutoff.json")


class ProgramFlowTests(unittest.TestCase):
    def test_full_cycle_keeps_calibration_saves_separate_from_normal_test(self):
        recipe = production_recipe()
        calibration = BaselineBench(recipe)
        calibration.capabilities = replace(calibration.capabilities, simulation=False)
        test = DemoBench(recipe, test_start_method="external_restart")
        test.capabilities = replace(test.capabilities, simulation=False)
        settings = AutomationSettings.demo()
        flow = ProductionFlow(recipe, "WO-S00430-1-001", "Tester", 1,
            lambda: (calibration, settings), lambda: (test, settings), test_program=program_entry()["program"])
        clock = [100.0]
        with patch("time.monotonic", lambda: clock[0]):
            flow.start()
            for _ in range(4000):
                clock[0] += .25
                flow.tick()
                if not flow.is_running:
                    break
        result = flow.unit_result
        self.assertIsNotNone(result)
        self.assertEqual(result["outcome"], "PASS", result["detail"])
        self.assertEqual(result["calibration_result"], "PASS")
        self.assertEqual(result["test_result"], "PASS")
        self.assertTrue(result["normal_operation_verified"])
        self.assertFalse(result["persistence_verified"])
        self.assertEqual(result["baseline_result"]["open_voltage"]["sample_count"], 1)
        self.assertIn("prepare_parameters", [item[0] for item in calibration.commands])
        self.assertIn("save_current", [item[0] for item in calibration.commands])
        self.assertIn("save_voltage", [item[0] for item in calibration.commands])
        forbidden = {"save_current", "save_voltage", "prepare_parameters", "three_duty_pairs", "calibration_start_and_three_duty_pairs"}
        self.assertFalse(any(item[0] in forbidden for item in test.commands))
        self.assertTrue(all(item[2] == "external_restart" for item in test.commands if item[0] == "post_test_start"))
        self.assertEqual(result["test"]["can_tx_count"], 0)
        self.assertTrue(result["test"]["termination_evidence"]["control_hold_completed"])

    def test_wrong_method_stops_before_test_commands(self):
        recipe = production_recipe()
        program = program_entry()["program"]
        program["workflow"] = "test_only"
        bench = DemoBench(recipe, post_test_can_start=True, test_start_method="calibration_start_only")
        flow = ProductionFlow(recipe, "WO-S00430-1-001", "Tester", 1, None,
            lambda: (bench, AutomationSettings.demo()), run_calibration=False, test_program=program)
        flow.start()
        self.assertEqual(flow.unit_result["outcome"], "FAIL")
        self.assertIn("command-free", flow.unit_result["detail"])
        self.assertEqual(bench.commands, [])

    def test_program_workflow_mismatch_cannot_open_instruments(self):
        factory = Mock(side_effect=AssertionError("No instruments"))
        flow = ProductionFlow(production_recipe(), "WO-S00430-1-001", "Tester", 1,
            factory, factory, run_calibration=False, test_program=program_entry()["program"])
        with self.assertRaisesRegex(ValueError, "does not match"):
            flow.start()
        factory.assert_not_called()


class ProgramControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        atomic_json(self.folder / "profiles.json", [dict(recipe=production_recipe().to_dict(), revision=1)])
        self.guards = [patch("leanwatts_can.maynuo.MaynuoLoad.open", side_effect=AssertionError("No hardware")),
                       patch("leanwatts_can.can_monitor.MonitorClient.open", side_effect=AssertionError("No hardware"))]
        for guard in self.guards:
            guard.start()
        self.window = CalibrationWindow(self.folder)
        self.window.legacy_page.mode_select.setCurrentIndex(2)

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        for guard in self.guards:
            guard.stop()
        self.tmp.cleanup()

    def payload(self):
        entry = program_entry()
        return dict(program=entry["program"], program_path=str(entry["path"]), program_sha256=entry["sha256"],
                    program_source=entry["source"], serial="WO-S00430-1-001", serial_override=False)

    def login(self):
        page = self.window.operator_page
        page.accounts.upsert_operator("TEST", "Tester", "password", role="admin")
        operator = page.accounts.authenticate("TEST", "password")
        self.window.test_controller._operator = operator
        self.window.test_page.variant.setCurrentIndex(0)
        return page

    def test_requires_operator_without_touching_hardware(self):
        self.window.test_controller.start(self.payload())
        self.assertFalse(self.window.test_controller.busy)
        self.assertIn("Sign in", self.window.test_page.status.text())
        self.assertFalse((self.folder / "test_runs").exists())
        self.assertEqual(self.window.tabs.count(), 8)

    def test_connection_failure_exports_zip_and_does_not_create_production_pass(self):
        page = self.login()
        self.window._operator_live_reason = lambda process, **kwargs: ""
        failure = Mock(side_effect=RuntimeError("Fixture connection unavailable"))
        self.window._operator_live_factory = lambda *args, **kwargs: failure
        controller = self.window.test_controller
        controller.start(self.payload())
        self.assertTrue(controller.busy)
        self.assertFalse(self.window.tabs.isTabEnabled(self.window.tabs.indexOf(page)))
        page.serial.setText("WO-S00430-1-001")
        self.assertIn("test program", page.start_guard())
        deadline = time.monotonic() + 5
        while controller.busy and time.monotonic() < deadline:
            self.app.processEvents()
            controller.refresh()
            time.sleep(.01)
        self.assertFalse(controller.busy)
        self.assertTrue(self.window.tabs.isTabEnabled(self.window.tabs.indexOf(page)))
        self.assertIsNone(page.store.active())
        bundle = Path(controller.snapshot["result_bundle"])
        self.assertTrue(bundle.is_file())
        with zipfile.ZipFile(bundle) as archive:
            self.assertIn("can-trace.csv", archive.namelist())
            self.assertIn("load-readings.csv", archive.namelist())
            self.assertEqual(archive.read("program.json").decode("utf-8"), self.payload()["program_source"])
            result = json.loads(archive.read("result.json"))
            self.assertEqual(result["outcome"], "FAIL")
            self.assertFalse(result["production_acceptance"])
        self.assertTrue(self.window.test_page.copy_button.isEnabled())

    def test_tab_selection_is_independent_and_freezes_its_own_recipe(self):
        self.login()
        other = production_recipe()
        other.name = "Independent test variant"
        records = [dict(recipe=production_recipe().to_dict(), revision=1), dict(recipe=other.to_dict(), revision=7)]
        self.window.test_page.set_profiles(records)
        self.window.test_page.variant.setCurrentIndex(1)
        self.window.operator_page.variant.setCurrentIndex(-1)
        self.window._operator_live_reason = lambda process, **kwargs: ""
        recipe, record, operator = self.window.test_controller._context(self.payload()["program"])
        self.assertEqual(recipe.name, "Independent test variant")
        self.assertEqual(record["revision"], 7)
        self.assertEqual(operator["id"], "TEST")
        self.assertIsNone(self.window.session_store.active())
        self.window.test_page.identity.records[1]["recipe"]["name"] = "Edited later"
        self.assertEqual(recipe.name, "Independent test variant")
        self.assertEqual(record["recipe"]["name"], "Independent test variant")

    def test_disabled_operator_and_manual_command_are_blocked_at_entry(self):
        self.login()
        with patch.object(self.window.accounts_page.accounts, "list_operators", return_value=[]):
            self.window.test_controller.start(self.payload())
            self.assertFalse(self.window.test_controller.busy)
            self.assertIn("disabled", self.window.test_page.status.text())
        self.window.legacy_page._live_future = Mock()
        self.window.legacy_page._live_operation = "send_parameters"
        try:
            self.window.test_controller.start(self.payload())
            self.assertFalse(self.window.test_controller.busy)
            self.assertIn("CAN command", self.window.test_page.status.text())
        finally:
            self.window.legacy_page._live_future = None
            self.window.legacy_page._live_operation = None
        self.assertFalse((self.folder / "test_runs").exists())

    def test_recipe_refresh_preserves_each_tabs_selection(self):
        self.login()
        second = production_recipe()
        second.name = "Second variant"
        records = [dict(recipe=production_recipe().to_dict(), revision=1), dict(recipe=second.to_dict(), revision=2)]
        self.window.recipes.profiles_changed.emit(records, 0)
        self.window.test_page.variant.setCurrentIndex(1)
        self.window.firmware_page.variant.setCurrentIndex(0)
        self.window.recipes.profiles_changed.emit(records, 0)
        self.assertEqual(self.window.test_page.identity.selected_record["recipe"]["name"], second.name)
        self.assertEqual(self.window.firmware_page.identity.selected_record["recipe"]["name"], production_recipe().name)
        self.assertEqual(self.window.operator_page.variant.currentIndex(), 0)


if __name__ == "__main__":
    unittest.main()
