"""Offscreen application integration checks; no hardware drivers are loaded."""
import json
import os
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QScrollArea
from leanwatts_can import theme
from leanwatts_can.automation import AutomationEngine
from leanwatts_can.core import Recipe
from leanwatts_can.gui import CalibrationWindow
from leanwatts_can.recipe_page import RAMP_DEFAULTS


def fixture_recipe(name="Variant A"):
    return Recipe(name=name, cc_current=15, end_voltage=58.4, end_current=1.1,
                  max_voltage=65, max_current=20, max_power=1300,
                  programmed_cc_current=15, programmed_end_voltage=58.4, programmed_end_current=1.5,
                  termination_test={"enabled": True, **RAMP_DEFAULTS},
                  device_settings=dict(batteryType=2, ratedVoltage=48, startVoltage=40,
                      temperatureLimit=70, maxChargeMinutes=480, derating110=50, derating220=100,
                      blindCharge=0, rechargeVoltage=52, adjustCurrentMin=0, adjustCurrentMax=0,
                      adjustVoltageMin=0, adjustVoltageMax=0))


class FastDemoEngine(AutomationEngine):
    """The real simulated engine with deterministic time, so a demo run takes no wall time."""
    def start_calibration(self, now=None):
        return super().start_calibration(now=1000.0)

    def start_test(self, now=None):
        return super().start_test(now=1000.0)

    def tick(self, now=None):
        return super().tick(now=self._now + .25)


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.load_fonts()
        cls.app.setFont(theme.ui_font(14))
        cls.app.setStyleSheet(theme.STYLESHEET)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.guards = [patch("leanwatts_can.maynuo.MaynuoLoad.open", side_effect=AssertionError("Hardware forbidden")),
                       patch("leanwatts_can.can_monitor.MonitorClient.open", side_effect=AssertionError("Hardware forbidden"))]
        for guard in self.guards:
            guard.start()
        self.window = CalibrationWindow(Path(self.temp.name))
        self.window.operator_page.timer.stop()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        for guard in self.guards:
            guard.stop()
        self.temp.cleanup()

    def login(self, page):
        page.accounts.upsert_operator("OP", "Operator", "password", role="admin")
        operator = page.accounts.authenticate("OP", "password")
        page._session = page.store.start(operator)
        page._authenticated_operator = operator

    def run_simulated_unit(self, serial="WO-S00430-1-001"):
        w = self.window
        page = w.operator_page
        page.serial.setText(serial)
        with patch("leanwatts_can.production_flow.AutomationEngine", FastDemoEngine), \
             patch("leanwatts_can.gui.BenchSession", side_effect=AssertionError("Simulation must not build a bench")):
            page.start("calibration")
            self.assertTrue(page.busy, page.detail.text())
            for _ in range(2000):
                if page._future is not None:
                    page._future.result(timeout=5)
                page.refresh()
                if not page.busy:
                    break
        self.assertFalse(page.busy)
        return page.snapshot

    def save_variant(self):
        recipes = self.window.recipes
        recipes._show_recipe(fixture_recipe())
        recipes.save_variant()
        self.assertIn("saved", recipes.status.text())
        return recipes

    def test_auto_profile_history_and_reload(self):
        w = self.window
        recipes = self.save_variant()
        self.assertEqual(len(recipes.records), 1)
        self.assertEqual(w.operator_page.variant.count(), 1)
        w.legacy_page.mode_select.setCurrentIndex(0)
        w.operator_page.set_mode("simulation")
        self.login(w.operator_page)
        snapshot = self.run_simulated_unit()
        self.assertEqual(snapshot["unit_result"]["outcome"], "SIMULATION")
        self.assertTrue(snapshot["unit_result"]["simulation"])
        self.assertEqual(w.legacy_page.frame_log, [])
        unit = w.session_store.active()["units"][-1]
        self.assertEqual(unit["serial"], "WO-S00430-1-001")
        self.assertEqual(unit["recipe"]["device_settings"]["batteryType"], 2)
        self.assertTrue(unit["simulation"])
        w.history_page.refresh()
        self.assertEqual(w.history_page.table.rowCount(), 1)
        second = CalibrationWindow(Path(self.temp.name))
        try:
            second.operator_page.timer.stop()
            self.assertEqual(second.operator_page.variant.count(), 1)
            self.assertEqual(second.recipes.records[0]["recipe"]["device_settings"]["batteryType"], 2)
            self.assertEqual(second.history_page.table.rowCount(), 1)
        finally:
            second.close()
            second.deleteLater()

    def test_identity_change_prevents_cross_unit_results(self):
        w = self.window
        self.save_variant()
        w.legacy_page.mode_select.setCurrentIndex(0)
        w.operator_page.set_mode("simulation")
        self.login(w.operator_page)
        self.run_simulated_unit()
        page = w.operator_page
        self.assertIsNotNone(page.snapshot)
        page.serial.setText("WO-S00430-1-002")
        self.assertIsNone(page.snapshot)
        self.assertIsNone(page.flow)
        self.assertEqual(page.stage.text(), "Ready for the next charger")
        self.assertEqual(page.current.value.text(), "—")
        self.assertEqual(page.voltage.value.text(), "—")
        for row in range(page.results.rowCount()):
            self.assertIn(page.results.item(row, 4).text(), ("Pending", "NOT RUN"))

    def test_no_outer_scroll_and_laptop_layout(self):
        w = self.window
        minimum = w.minimumSize()
        # The shell's supported minimum (formerly 1024 x 650) must not grow.
        self.assertLessEqual((minimum.width(), minimum.height()), (1040, 680))
        w.resize(minimum)
        w.show()
        self.app.processEvents()
        for tab in range(w.tabs.count()):
            w.tabs.setCurrentIndex(tab)
            self.app.processEvents()
            self.assertNotIsInstance(w.tabs.widget(tab), QScrollArea)
            self.assertLessEqual(w.height(), minimum.height(), f"tab {tab} grew beyond the supported height")
            self.assertLessEqual(w.width(), minimum.width(), f"tab {tab} grew beyond the supported width")
        w.tabs.setCurrentWidget(w.operator_page)
        self.app.processEvents()
        p = w.operator_page
        for control in (p.calibrate, p.test_only, p.stop_button, p.variant, p.serial, p.results, p.open_report):
            point = control.mapTo(w, control.rect().bottomRight())
            self.assertLess(point.y(), w.height(), control.objectName())
            self.assertLess(point.x(), w.width(), control.objectName())

    def test_unstarted_export_and_malformed_history(self):
        w = self.window
        w.operator_page._open_report()
        self.assertIn("not available yet", w.operator_page.report_status.text())
        folder = Path(self.temp.name) / "reports"
        folder.mkdir()
        (folder / "stray.json").write_text("[]", encoding="utf-8")
        (folder / "null.json").write_text('{"recipe": null}', encoding="utf-8")
        (Path(self.temp.name) / "sessions" / "broken.json").write_text("{not json", encoding="utf-8")
        w.history_page.refresh()
        self.assertEqual(w.history_page.table.rowCount(), 0)
        w.history_page.kind.setCurrentIndex(w.history_page.kind.findData("legacy"))
        self.assertEqual(w.history_page.table.rowCount(), 0)
        self.assertEqual(w.history_page.status.text(), "")
        w.history_page._export()
        self.assertIn("Select a report first", w.history_page.status.text())

    def test_no_original_app_or_hardware_transports_imported(self):
        for prefix in ("lw_eol", "serial", "can", "pyvisa"):
            self.assertFalse(any(name == prefix or name.startswith(prefix + ".") for name in sys.modules), prefix)
        self.assertIsNone(self.window.legacy_page._live_client)
        self.assertEqual(self.window.legacy_page.frame_log, [])
        self.assertFalse(self.window.load_setup.busy)


if __name__ == "__main__":
    unittest.main()
