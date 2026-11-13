"""Operator integration checks. Hardware entry points are forbidden."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from leanwatts_can.gui import CalibrationWindow
from leanwatts_can.recipe_page import atomic_json, RAMP_DEFAULTS
from leanwatts_can.core import Recipe


def profile():
    return Recipe(name="UI fixture", cc_current=15, end_voltage=58.4, end_current=1.1,
        current_tolerance=.1, voltage_tolerance=.1, end_current_tolerance=.1,
        max_voltage=65, max_current=20, max_power=1300,
        programmed_cc_current=15, programmed_end_voltage=58.4, programmed_end_current=1.5,
        cc_test_load_voltage=49, voltage_test_load_current=2,
        termination_test={"enabled": True, **RAMP_DEFAULTS},
        device_settings=dict(batteryType=0, ratedVoltage=48, startVoltage=40,
            temperatureLimit=70, maxChargeMinutes=480, derating110=50, derating220=100,
            blindCharge=0, rechargeVoltage=52, adjustCurrentMin=0, adjustCurrentMax=0,
            adjustVoltageMin=0, adjustVoltageMax=0))


class SessionUIIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        atomic_json(self.data / "profiles.json", [dict(recipe=profile().to_dict(), revision=1)])
        self.load_guard = patch("leanwatts_can.maynuo.MaynuoLoad.open", side_effect=AssertionError("Hardware forbidden"))
        self.can_guard = patch("leanwatts_can.can_monitor.MonitorClient.open", side_effect=AssertionError("Hardware forbidden"))
        self.load_guard.start(); self.can_guard.start()
        self.window = CalibrationWindow(self.data)
        self.window.legacy_page.mode_select.setCurrentIndex(2)

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.load_guard.stop(); self.can_guard.stop()
        self.tmp.cleanup()

    def test_login_required_and_no_account_fabricated(self):
        page = self.window.operator_page
        page.serial.setText("WO-S00430-1-001")
        page.start()
        self.assertFalse(page.busy)
        self.assertIsNone(self.window.session_store.active())
        self.assertFalse((self.data / "operators.json").exists())

    def test_failed_connection_recorded_once_without_running_test(self):
        page = self.window.operator_page
        page.accounts.upsert_operator("OP", "Operator", "password", role="admin")
        operator = page.accounts.authenticate("OP", "password")
        page._authenticated_operator = operator
        page._session = page.store.start(operator)
        page.serial.setText("WO-S00430-1-001")
        page.live_readiness = lambda process: ""
        calls = []
        def factory(recipe, process, cancel):
            def prepare():
                calls.append(process)
                raise RuntimeError("Load-bank response timed out")
            return prepare
        page.live_run_factory = factory
        page.start()
        deadline = time.monotonic() + 3
        while page.busy and time.monotonic() < deadline:
            self.app.processEvents()
            page.refresh()
            time.sleep(.005)
        self.assertFalse(page.busy)
        units = page.store.active()["units"]
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["outcome"], "FAIL")
        self.assertEqual(calls, ["calibration"])
        page.refresh(); page.refresh()
        self.assertEqual(len(page.store.active()["units"]), 1)

    def test_recipe_preserves_required_vs_programmed_and_updates_block_on_session(self):
        page = self.window.recipes
        page.name.setText("Second variant")
        page.save_variant()
        self.assertEqual(len(page.records), 2)
        saved = Recipe.from_dict(page.records[-1]["recipe"])
        self.assertEqual(saved.end_current, 1.1)
        self.assertEqual(saved.programmed_end_current, 1.5)
        self.assertEqual(saved.test_cv_voltage, 49)
        self.assertFalse(self.window._update_busy())
        self.window.session_store.start(dict(id="OP", name="Operator", enabled=True, role="operator"))
        self.assertTrue(self.window._update_busy())
        self.window.session_store.close()
        self.assertFalse(self.window._update_busy())


if __name__ == "__main__":
    unittest.main()
