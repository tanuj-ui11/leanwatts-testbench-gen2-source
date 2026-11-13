"""Live-mode main-window boundaries; no hardware or DLL is opened."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from leanwatts_can.core import DEVICE_SETTING_SCALES, Recipe
from leanwatts_can.gui import CalibrationWindow


def fixture_recipe():
    """Required 15 A / 58.4 V / 1.1 A; programmed 15 A / 58.4 V / 1.5 A; 65 V bench."""
    return Recipe(name="Live fixture", cc_current=15, end_voltage=58.4, end_current=1.1,
                  max_voltage=65, max_current=20, max_power=1300,
                  programmed_cc_current=15, programmed_end_voltage=58.4, programmed_end_current=1.5,
                  device_settings={key: 0 for key in DEVICE_SETTING_SCALES})


class LiveIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.load_guard = patch("leanwatts_can.maynuo.MaynuoLoad.open", side_effect=AssertionError("Hardware forbidden"))
        self.can_guard = patch("leanwatts_can.can_monitor.MonitorClient.open", side_effect=AssertionError("Hardware forbidden"))
        self.load_guard.start()
        self.can_guard.start()
        self.window = CalibrationWindow(Path(self.directory.name))
        self.window.operator_page.timer.stop()

    def tearDown(self):
        self.window.legacy_page._live_future = None
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.load_guard.stop()
        self.can_guard.stop()
        self.directory.cleanup()

    def test_live_mode_blocks_simulated_work_even_via_direct_methods(self):
        page = self.window.legacy_page
        session = self.window.operator_page
        page.mode_select.setCurrentIndex(2)
        self.assertEqual(session._mode, "manual")
        # Simulated (offline) frame generation is refused outright in live mode.
        with self.assertRaisesRegex(ValueError, "unavailable in live mode"):
            page._append_frames("calibrationStart", [{"id": 0, "dlc": 0, "data": [], "extended": True}])
        # The same entry points route to the live path, which refuses without an open live CAN connection.
        for action in (page.calibration_start, page.save_full_current, lambda: page.adjust_duty("current", .1)):
            self.assertFalse(page._guarded(action))
            self.assertTrue(page.last_error)
        page.complete_readback()  # Simulated readback is a no-op in live mode.
        self.assertEqual(page.frame_log, [])
        self.assertIsNone(page._live_client)
        self.assertIsNone(page._live_future)
        for name in ("calibration_start", "save_full_current", "current_plus"):
            self.assertFalse(page.buttons[name].isEnabled(), name)
        # The session page never builds a simulated bench in live mode.
        with patch("leanwatts_can.session_page.DemoBench", side_effect=AssertionError("No simulated fallback")):
            session.start("calibration")
        self.assertFalse(session.busy)
        page.mode_select.setCurrentIndex(0)
        self.assertEqual(session._mode, "simulation")
        page.open_device()
        page.start_can()
        self.assertTrue(page.buttons["calibration_start"].isEnabled())
        self.assertIsNone(page._live_client)

    def test_recipe_can_load_during_background_reads_without_transmission(self):
        recipes = self.window.recipes
        recipes._show_recipe(fixture_recipe())
        page = self.window.legacy_page
        page.mode_select.setCurrentIndex(2)
        page._live_timer.stop()
        page.device_open = page.can_started = True
        for operation in ("receive", "readback_poll"):
            with self.subTest(operation=operation):
                page.bench_limits = None
                page._live_operation = operation
                pending = Future()
                page._live_future = pending
                recipes.send_parameters()
                self.assertEqual(page.settings["endVoltage"].text(), "58.4")  # Programmed value.
                self.assertEqual(page.settings["endCurrent"].text(), "1.5")
                self.assertEqual(page.bench_limits["max_voltage"], 65)
                self.assertIs(page._live_future, pending)
                # No Shutdown was requested, so the explicit send is refused; nothing is queued or sent.
                self.assertIn("Shutdown", page.last_error)
                self.assertIsNone(page._live_pending_operator)
                self.assertIsNone(page.pending_settings)
                self.assertIsNone(page._live_client)
                self.assertFalse(page.frame_log)
        page._live_future = None

    def test_recipe_cannot_replace_an_active_operator_command(self):
        recipes = self.window.recipes
        recipes._show_recipe(fixture_recipe())
        page = self.window.legacy_page
        page.mode_select.setCurrentIndex(2)
        page._live_timer.stop()
        page._live_operation = "send_settings"
        page._live_future = Future()
        before = {key: edit.text() for key, edit in page.settings.items()}
        recipes.send_parameters()
        self.assertIn("Wait for the active CAN action", recipes.status.text())
        self.assertEqual({key: edit.text() for key, edit in page.settings.items()}, before)
        self.assertIsNone(page.bench_limits)
        self.assertIsNone(page._live_client)
        page._live_future = None


if __name__ == "__main__":
    unittest.main()
