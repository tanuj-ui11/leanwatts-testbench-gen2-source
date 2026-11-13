"""Operator UI isolation, reporting and layout; no hardware transports are used."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QScrollArea

from leanwatts_can import theme
from leanwatts_can.automation import AutomationEngine
from leanwatts_can.core import Recipe
from leanwatts_can.gui import CalibrationWindow
from leanwatts_can.operator_page import OperatorPage
from leanwatts_can.recipe_page import RAMP_DEFAULTS


def demo_recipe():
    """Complete fixture variant (the old gui.demo_recipe helper no longer exists)."""
    return Recipe(name="Demo fixture", cc_current=15, end_voltage=58.4, end_current=1.1,
                  current_tolerance=.1, voltage_tolerance=.1, end_current_tolerance=.1,
                  max_voltage=65, max_current=20, max_power=1300,
                  programmed_cc_current=15, programmed_end_voltage=58.4, programmed_end_current=1.5,
                  cc_test_load_voltage=49, voltage_test_load_current=2,
                  termination_test={"enabled": True, **RAMP_DEFAULTS},
                  device_settings=dict(batteryType=0, ratedVoltage=48, startVoltage=40,
                      temperatureLimit=70, maxChargeMinutes=480, derating110=50, derating220=100,
                      blindCharge=0, rechargeVoltage=52, adjustCurrentMin=0, adjustCurrentMax=0,
                      adjustVoltageMin=0, adjustVoltageMax=0))


class FastDemoEngine(AutomationEngine):
    """Advance the real demo engine with deterministic time, without sleeps."""
    def start_calibration(self, now=None):
        return super().start_calibration(now=1000.0)

    def start_test(self, now=None):
        return super().start_test(now=1000.0)

    def tick(self, now=None):
        return super().tick(now=self._now + .25)


class DelayedDemoEngine:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.stop_calls = 0
        self.is_running = True

    def tick(self):
        self.entered.set()
        if not self.release.wait(2):
            raise RuntimeError("Test did not release the delayed tick")

    def stop(self, *args):
        self.stop_calls += 1
        self.is_running = False

    def snapshot(self):
        return {"simulation": True, "is_running": self.is_running,
                "state": "OLD DEMO RESULT", "detail": "Old mode only", "unit": "A",
                "target": 10.0, "measured": 9.7, "error": -.3,
                "reading": {"current": 9.7, "voltage": 50.0, "power": 485.0},
                "results": [{"name": "CC current", "measured": 9.7, "target": 10,
                             "tolerance": .5, "status": "PASS"}], "report_paths": {}}


class OperatorPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.load_fonts()
        cls.app.setFont(theme.ui_font(14))
        cls.app.setStyleSheet(theme.STYLESHEET)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.legacy = SimpleNamespace(can_started=False, _live_telemetry={}, _live_telemetry_at={})
        self.page = OperatorPage(Path(self.directory.name), self.legacy)
        self.page.timer.stop()
        self.recipe = demo_recipe()
        self.page.set_profiles([{"revision": 2, "recipe": self.recipe.to_dict()}])
        self.page.serial.setText("UNIT-UI-TEST")
        self.page.operator.setText("Test operator")

    def tearDown(self):
        engine = self.page.engine
        if isinstance(engine, DelayedDemoEngine):
            engine.release.set()
        if not self.page._closing:
            self.page.dispose()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def assertNoPreviousResults(self):
        # A mode change shows that mode's placeholder stages; nothing measured carries over.
        for row in range(self.page.results.rowCount()):
            self.assertEqual(self.page.results.item(row, 1).text(), "—")
            self.assertEqual(self.page.results.item(row, 3).text(), "Pending")

    def finish_run(self):
        for _ in range(600):
            future = self.page._future
            if future is not None:
                future.result(timeout=3)
            self.page.refresh()
            if not self.page.busy:
                return
        self.fail("Demo did not reach a terminal state")

    def publish_can(self):
        self.legacy.can_started = True
        self.legacy._live_telemetry = {"outputVoltage": 48.0, "outputCurrent": 15.0, "faultWord": 0x1234}
        self.legacy._live_telemetry_at = {key: time.monotonic() for key in self.legacy._live_telemetry}

    def test_live_auto_actions_are_disabled_and_direct_calls_create_no_engine(self):
        p = self.page
        with patch("leanwatts_can.operator_page.AutomationEngine") as factory:
            for mode in ("manual", "receive"):
                p.set_mode(mode)
                self.assertFalse(p.calibrate.isEnabled())
                self.assertFalse(p.test.isEnabled())
                p.start("calibration")
                self.assertIn("Maynuo", p.detail.text())  # Load setup is required first.
                p.start("test")  # Live tests are not exposed by this page.
                self.assertIn("after calibration is established", p.detail.text())
                p.refresh()
                factory.assert_not_called()
                self.assertIsNone(p._future)

    def test_live_cards_are_charger_observations_and_stale_values_disappear(self):
        p = self.page
        p.set_mode("manual")
        self.publish_can()
        p.refresh()
        self.assertEqual(p.current.value.text(), "15.000")
        self.assertEqual(p.voltage.value.text(), "48.000")
        self.assertIn("charger readings", p.source.text())
        self.assertIn("720.000 W", p.power.text())
        self.assertIn("0x1234", p.faults.text())
        self.assertIn("Maynuo", p.reference.text())
        self.assertIn("Load reference  —", p.reference.text())
        self.assertEqual(p.error_side.text(), "Target error  —")
        self.legacy._live_telemetry_at["outputCurrent"] = time.monotonic() - 3
        p.refresh()
        self.assertEqual(p.current.value.text(), "—")
        self.assertEqual(p.voltage.value.text(), "48.000")
        self.assertEqual(p.power.text(), "Power  — W")
        self.assertIn("stale", p.source.text())
        self.legacy.can_started = False
        p.refresh()
        self.assertEqual(p.voltage.value.text(), "—")
        self.assertIn("no fresh data", p.faults.text())

    def test_run_status_shows_unregulated_without_hiding_protection_or_can_faults(self):
        self.page._render_run({
            "simulation": False, "is_running": True,
            "reading": {"fault": "Over-temperature", "unregulated": True},
            "charger_observations": {"faultWord": 0x1234},
            "charger_observed_at": {"faultWord": time.monotonic()},
        })
        self.assertIn("Over-temperature", self.page.faults.text())
        self.assertIn("Load unregulated (not at setpoint)", self.page.faults.text())
        self.assertIn("0x1234", self.page.faults.text())

    def test_invalid_or_future_dated_live_values_are_not_presented_as_fresh(self):
        p = self.page
        p.set_mode("manual")
        self.publish_can()
        self.legacy._live_telemetry["outputVoltage"] = float("nan")
        self.legacy._live_telemetry_at["outputCurrent"] = time.monotonic() + 20
        self.legacy._live_telemetry["faultWord"] = float("inf")
        p.refresh()
        self.assertEqual(p.voltage.value.text(), "—")
        self.assertEqual(p.current.value.text(), "—")
        self.assertIn("stale", p.source.text())
        self.assertIn("no fresh data", p.faults.text())

    def test_leaving_live_mode_clears_all_live_measurements(self):
        p = self.page
        p.set_mode("manual")
        self.publish_can()
        p.refresh()
        p.set_mode("simulation")
        self.assertIn("SIMULATION", p.source.text())
        self.assertEqual(p.voltage.value.text(), "—")
        self.assertEqual(p.current.value.text(), "—")
        self.assertNotIn("720.000", p.power.text())
        self.assertNotIn("0x1234", p.faults.text())
        self.assertNotIn("Independent Maynuo measurements", p.reference.text())

    def test_mode_switch_stops_snapshot_only_run_and_clears_metrics(self):
        p = self.page
        engine = p.engine = DelayedDemoEngine()
        p.snapshot = engine.snapshot()
        p._render_run(p.snapshot)
        p.set_mode("manual")
        if p._future is not None:  # A fast stop may already have been collected by set_mode().
            p._future.result(timeout=3)
        p.refresh()
        self.assertEqual(engine.stop_calls, 1)
        self.assertFalse(engine.is_running)
        self.assertIsNone(p.snapshot)
        self.assertFalse(p.busy)
        self.assertNotEqual(p.stage.text(), "OLD DEMO RESULT")
        self.assertEqual(p.measured_value.text(), "Measured\n—")
        self.assertNoPreviousResults()

    def test_inflight_demo_cannot_render_into_live_mode_or_be_replaced_before_stop(self):
        p = self.page
        engine = p.engine = DelayedDemoEngine()
        p.snapshot = engine.snapshot()
        p._future = p._executor.submit(p._tick_worker)
        self.assertTrue(engine.entered.wait(1))
        p.set_mode("manual")
        p.set_mode("simulation")
        p.start("test")
        self.assertIs(p.engine, engine)
        p.set_mode("manual")
        self.publish_can()
        engine.release.set()
        p._future.result(timeout=3)
        p.refresh()
        self.assertGreaterEqual(engine.stop_calls, 1)
        self.assertFalse(engine.is_running)
        self.assertIsNone(p.snapshot)
        self.assertEqual(p.measured_value.text(), "Measured\n—")
        self.assertNoPreviousResults()
        self.assertNotIn("OLD DEMO RESULT", p.stage.text())
        self.assertIn("LIVE CAN", p.source.text())

    def test_demo_calibration_stays_separate_and_saves_simulation_report(self):
        p = self.page
        saved = []
        p.report_saved.connect(lambda: saved.append(True))
        with patch("leanwatts_can.operator_page.AutomationEngine", FastDemoEngine):
            p.start("calibration")
        self.finish_run()
        self.assertEqual(p.engine.operation, "calibration")
        self.assertTrue(p.snapshot["simulation"])
        self.assertIn("SIMULATION", p.snapshot["outcome"])
        commands = p.engine.bench.commands
        self.assertTrue(any(item[0] == "save_current" for item in commands))
        self.assertTrue(any(item[0] == "save_voltage" for item in commands))
        self.assertFalse(any(item[0] == "passive" for item in commands))
        self.assertEqual(len(saved), 1)
        self.assertTrue(Path(p._last_report).is_file())
        record = json.loads(Path(p.snapshot["report_paths"]["json"]).read_text(encoding="utf-8"))
        self.assertTrue(record["simulation"])
        self.assertEqual(record["unit_serial"], "UNIT-UI-TEST")
        self.assertFalse(self.legacy.can_started)
        self.assertEqual(self.legacy._live_telemetry, {})

    def test_demo_test_can_run_without_calibration_and_has_zero_demo_can_tx(self):
        p = self.page
        with patch("leanwatts_can.operator_page.AutomationEngine", FastDemoEngine):
            p.start("test")
        self.finish_run()
        self.assertEqual(p.engine.operation, "test")
        self.assertEqual(p.snapshot["can_tx_count"], 0)
        self.assertIn(("passive",), p.engine.bench.commands)
        self.assertFalse(any(item[0].startswith("save_") for item in p.engine.bench.commands))
        self.assertTrue(p.snapshot["simulation"])
        self.assertFalse(self.legacy.can_started)

    def test_new_unit_clears_completed_results_but_retains_identified_previous_report(self):
        p = self.page
        with patch("leanwatts_can.operator_page.AutomationEngine", FastDemoEngine):
            p.start("test")
        self.finish_run()
        report = p._last_report
        self.assertGreater(p.results.rowCount(), 0)
        p.serial.setText("NEXT-UNIT")
        self.assertIsNone(p.snapshot)
        self.assertIsNone(p.engine)
        self.assertEqual(p.results.rowCount(), 0)
        self.assertEqual(p.measured_value.text(), "Measured\n—")
        self.assertEqual(p.current.value.text(), "—")
        self.assertEqual(p._last_report, report)
        self.assertIn("UNIT-UI-TEST", p.report_status.text())

    def test_report_failure_does_not_claim_saved_or_offer_missing_html(self):
        p = self.page
        p._record_report({"simulation": True, "unit_serial": "UNIT-UI-TEST", "outcome": "SIMULATION — ERROR",
                          "report_paths": {"html": str(Path(self.directory.name) / "missing.html")},
                          "report_error": "Report storage failed"})
        self.assertFalse(p.open_report.isEnabled())
        self.assertNotIn("report saved", p.report_status.text().lower())
        self.assertTrue(any(word in p.report_status.text().lower() for word in ("failed", "could not", "not saved")))

    def test_start_guard_prevents_overlapping_simulation_lab_run(self):
        p = self.page
        p.start_guard = lambda: "Stop the Simulation lab run first."
        with patch("leanwatts_can.operator_page.AutomationEngine") as factory:
            p.start("calibration")
            factory.assert_not_called()
        self.assertIn("Simulation lab", p.detail.text())

    def test_dispose_waits_for_demo_stop_and_report_before_directory_cleanup(self):
        p = self.page
        with patch("leanwatts_can.operator_page.AutomationEngine", FastDemoEngine):
            p.start("calibration")
        p._future.result(timeout=3)
        p.refresh()
        p.dispose()
        self.assertFalse(p.engine.is_running)
        self.assertIn("ABORTED", p.engine.outcome)
        self.assertTrue(Path(p.engine.report_paths["json"]).is_file())

    def test_compact_session_page_has_no_outer_scroll(self):
        # The window shell now hosts SessionPage as its operator page; its supported
        # minimum is the shell's minimum size (1040 x 680), not 1024 x 650.
        window = CalibrationWindow(Path(self.directory.name) / "layout")
        window.operator_page.timer.stop()
        try:
            minimum = window.minimumSize()
            self.assertLessEqual((minimum.width(), minimum.height()), (1040, 680))
            window.resize(minimum)
            window.show()
            for mode in (2, 0):
                window.legacy_page.mode_select.setCurrentIndex(mode)
                self.app.processEvents()
                p = window.operator_page
                self.assertIs(window.tabs.currentWidget(), p)
                self.assertNotIsInstance(p, QScrollArea)
                self.assertEqual(p.findChildren(QScrollArea), [])
                self.assertLessEqual(window.width(), minimum.width())
                self.assertLessEqual(window.height(), minimum.height())
                for control in (p.calibrate, p.test_only, p.stop_button, p.open_report, p.setup_button):
                    self.assertTrue(control.isVisible())
                    self.assertTrue(window.rect().contains(control.mapTo(window, control.rect().bottomRight())), control.text())
                self.assertIsNone(window.legacy_page._live_client)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

if __name__ == "__main__":
    unittest.main()
