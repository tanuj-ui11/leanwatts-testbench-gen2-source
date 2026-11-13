"""Main-window integration of real-mode ownership, setup and recipe semantics.

The fake session never constructs an instrument and the fake engine never emits
a charger command. Transport and controller behavior are tested separately.
"""
import json
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QScrollArea

from leanwatts_can import theme
from leanwatts_can.core import Recipe
from leanwatts_can.gui import CalibrationWindow
from leanwatts_can.recipe_page import RAMP_DEFAULTS, atomic_json


def fixture_recipe():
    return Recipe(name="Live fixture", cc_current=15, end_voltage=58.4, end_current=1.1,
                  current_tolerance=.1, voltage_tolerance=.1, end_current_tolerance=.1,
                  max_voltage=65, max_current=20, max_power=1300,
                  programmed_cc_current=15, programmed_end_voltage=58.4, programmed_end_current=1.5,
                  cc_test_load_voltage=49, voltage_test_load_current=2,
                  termination_test={"enabled": True, **RAMP_DEFAULTS},
                  device_settings=dict(batteryType=0, ratedVoltage=48, startVoltage=40,
                      temperatureLimit=70, maxChargeMinutes=480, derating110=50, derating220=100,
                      blindCharge=0, rechargeVoltage=52, adjustCurrentMin=0, adjustCurrentMax=0,
                      adjustVoltageMin=0, adjustVoltageMax=0))


class PreparedSession:
    instances = []
    probe_entered = None
    probe_release = None
    probe_identity = None

    def __init__(self, load_factory, **kwargs):
        self.load_factory = load_factory
        self.kwargs = kwargs
        self.probed = False
        self.capabilities = SimpleNamespace(simulation=False)
        self.__class__.instances.append(self)

    def probe(self):
        self.probed = True
        if self.__class__.probe_entered is not None:
            self.__class__.probe_entered.set()
        if self.__class__.probe_release is not None:
            if not self.__class__.probe_release.wait(3):
                raise RuntimeError("Test did not release probe")
        return {"identity": dict(self.__class__.probe_identity)}


class PreparedEngine:
    instances = []

    def __init__(self, recipe, unit, operator, *, recipe_revision, bench, settings, **kwargs):
        self.recipe = recipe
        self.bench = bench
        self.settings = settings
        self.samples = []
        self.is_running = False
        self.operation = ""
        self.start_calls = self.stop_calls = 0
        self.__class__.instances.append(self)

    def _event(self, text):
        pass

    def start_calibration(self):
        self.start_calls += 1
        self.operation, self.is_running = "calibration", True

    def start_test(self):
        self.start_calls += 1
        self.operation, self.is_running = "test", True

    def tick(self):
        self.is_running = False

    def stop(self, *args):
        self.stop_calls += 1
        self.is_running = False

    def snapshot(self):
        return {"simulation": False, "is_running": self.is_running, "operation": self.operation,
                "state": "Fake prepared hardware run", "detail": "Test transport only",
                "reading": {}, "results": [], "report_paths": {}, "events": []}


class LiveAutomationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.load_fonts()
        cls.app.setFont(theme.ui_font(14))
        cls.app.setStyleSheet(theme.STYLESHEET)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        atomic_json(self.data / "profiles.json", [dict(recipe=fixture_recipe().to_dict(), revision=1)])
        self.guards = [patch("leanwatts_can.maynuo.MaynuoLoad.open", side_effect=AssertionError("Hardware forbidden")),
                       patch("leanwatts_can.can_monitor.MonitorClient.open", side_effect=AssertionError("Hardware forbidden"))]
        for guard in self.guards:
            guard.start()
        self.window = CalibrationWindow(self.data)
        self.page = self.window.operator_page
        self.page.timer.stop()
        self.window.legacy_page.mode_select.setCurrentIndex(2)
        self.window.load_setup.port.setCurrentText("COM11")
        self.page.accounts.upsert_operator("OP", "Operator", "password", role="admin")
        operator = self.page.accounts.authenticate("OP", "password")
        self.page._session = self.page.store.start(operator)
        self.page._authenticated_operator = operator
        self.page.serial.setText("WO-S00430-1-001")
        self.page.refresh()
        identity = self.window.load_setup.PROFILE_IDENTITY
        PreparedSession.instances = []
        PreparedSession.probe_entered = PreparedSession.probe_release = None
        PreparedSession.probe_identity = {"model_code": identity.model_code, "firmware_code": identity.firmware_code}
        PreparedEngine.instances = []

    def tearDown(self):
        if PreparedSession.probe_release is not None:
            PreparedSession.probe_release.set()
        if self.page._future is not None and not isinstance(self.page._executor, Mock):
            self.page._future.result(timeout=3)
            self.page.refresh()
        self.page._future = None
        self.page.snapshot = None
        self.window.load_setup._future = None
        self.window.legacy_page._live_future = None
        self.window.legacy_page._live_client = None
        self.window.legacy_page._live_pending_operator = None
        self.window.legacy_page.device_open = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        for guard in self.guards:
            guard.stop()
        self.temp.cleanup()

    def finish(self):
        for _ in range(15):
            if self.page._future is not None:
                self.page._future.result(timeout=3)
            self.page.refresh()
            if not self.page.busy:
                return
        self.fail("Fake prepared run remained busy")

    def test_required_and_programmed_values_roundtrip_and_manual_uses_programmed(self):
        w = self.window
        recipes = w.recipes
        recipes.new_variant()
        recipes.name.setText("Required versus programmed test")
        recipes.fields["cc_current"].setText("14")
        recipes.fields["end_voltage"].setText("58.2")
        recipes.fields["end_current"].setText("1.1")
        recipes.fields["max_voltage"].setText("65")
        recipes.fields["max_current"].setText("20")
        recipes.parameters["ccCurrent"].setText("15")
        recipes.parameters["endVoltage"].setText("58.4")
        recipes.parameters["endCurrent"].setText("1.5")
        recipes.save_variant()
        self.assertIn("saved", recipes.status.text())
        stored = json.loads((self.data / "profiles.json").read_text())[-1]["recipe"]
        self.assertEqual((stored["cc_current"], stored["end_voltage"], stored["end_current"]), (14, 58.2, 1.1))
        self.assertEqual((stored["programmed_cc_current"], stored["programmed_end_voltage"], stored["programmed_end_current"]), (15, 58.4, 1.5))
        self.assertEqual(recipes.fields["end_current"].text(), "1.1")
        self.assertEqual(recipes.parameters["endCurrent"].text(), "1.5")
        with patch.object(w.legacy_page, "_monitor_factory") as transport:
            recipes.send_parameters()
            transport.assert_not_called()
        self.assertIsNone(w.legacy_page._live_client)
        self.assertEqual(w.legacy_page.settings["ccCurrent"].text(), "15")
        self.assertEqual(w.legacy_page.settings["endVoltage"].text(), "58.4")
        self.assertEqual(w.legacy_page.settings["endCurrent"].text(), "1.5")

    def test_unsaved_example_cannot_start_real_instruments(self):
        record = dict(recipe=fixture_recipe().to_dict(), revision=1, simulation_only=True)
        self.page.set_profiles([record])
        self.page.refresh()
        self.assertFalse(self.page.calibrate.isEnabled())
        self.assertFalse(self.page.test_only.isEnabled())
        self.assertIn("actual production variant", self.page.calibrate.toolTip())
        self.assertIn("actual variant", self.window._operator_live_reason("test_only", record=record))

    def test_manual_commands_and_load_probe_each_block_exclusive_run(self):
        w = self.window
        legacy = w.legacy_page
        self.assertEqual(w._operator_live_reason("calibration"), "")
        with patch("leanwatts_can.gui.BenchSession") as session:
            legacy._live_operation = "send_settings"
            legacy._live_future = Future()
            self.page.start("calibration")
            self.assertIn("current CAN command", self.page.detail.text())
            legacy._live_future = None
            legacy._live_pending_operator = ("shutdown", lambda: None)
            self.assertIn("queued CAN command", w._operator_live_reason("test_only"))
            legacy._live_pending_operator = None
            w.load_setup._future = Future()
            self.page.start("test_only")
            self.assertIn("connection check", self.page.detail.text())
            session.assert_not_called()
        w.load_setup._future = None
        self.assertFalse(self.page.busy)

    def test_live_test_uses_external_restart_and_opens_nothing_on_the_gui_thread(self):
        w = self.window
        self.page._executor.shutdown(wait=True)
        self.page._executor = Mock()
        self.page._executor.submit.side_effect = lambda *args, **kwargs: Future()
        factory = Mock(wraps=w._operator_live_factory)
        self.page.live_run_factory = factory
        with patch("leanwatts_can.gui.BenchSession") as session, \
             patch("leanwatts_can.gui.MaynuoLoad", side_effect=AssertionError("No hardware in this test")):
            self.page.start("test_only")
            session.assert_not_called()
        self.assertEqual([call.args[1] for call in factory.call_args_list], ["test_only"])
        self.assertEqual(factory.call_args.kwargs["test_start_method"], "external_restart")
        self.assertIn("AC restart", self.page.detail.text())

    def test_prepared_real_factory_runs_without_demo_and_locks_tabs_until_finished(self):
        w = self.window
        owners = (w.recipes, w.load_setup, w.legacy_page, w.settings_page, w.test_page, w.firmware_page)
        with patch("leanwatts_can.gui.BenchSession", PreparedSession), \
             patch("leanwatts_can.production_flow.AutomationEngine", PreparedEngine), \
             patch("leanwatts_can.session_page.DemoBench", side_effect=AssertionError("No demo fallback")), \
             patch("leanwatts_can.gui.MaynuoLoad", side_effect=AssertionError("No hardware in this test")):
            self.page.start("calibration")
            self.assertTrue(self.page.busy, self.page.detail.text())
            for page in owners:
                self.assertFalse(w.tabs.isTabEnabled(w.tabs.indexOf(page)), w.tabs.tabText(w.tabs.indexOf(page)))
            self.finish()
        self.assertEqual(len(PreparedSession.instances), 1)
        self.assertTrue(PreparedSession.instances[0].probed)
        self.assertTrue(PreparedSession.instances[0].kwargs["exclusive_confirmed"])
        self.assertEqual(PreparedEngine.instances[0].operation, "calibration")
        self.assertFalse(self.page.snapshot["simulation"])
        self.assertFalse(self.page.snapshot["unit_result"]["simulation"])
        self.assertTrue(all(w.tabs.isTabEnabled(i) for i in range(w.tabs.count())))

    def test_factory_freezes_configuration_and_checks_identity(self):
        w = self.window
        recipe = fixture_recipe()
        cancel = threading.Event()
        with patch("leanwatts_can.gui.BenchSession", PreparedSession), \
             patch("leanwatts_can.gui.MaynuoLoad") as load:
            prepare = w._operator_live_factory(recipe, "test", cancel)
            w.load_setup.port.setCurrentText("COM12")
            w.load_setup.PROFILE_MODES = {"CC": 123, "CV": 456}
            session, settings = prepare()
            self.assertEqual(session.kwargs["cc_mode_code"], 0x3B01)
            self.assertTrue(session.kwargs["standalone_ready"])
            session.load_factory()  # Mocked: records the frozen serial settings only.
            self.assertEqual(load.call_args.args[0].port, "COM11")
            PreparedSession.probe_identity = {"model_code": 1000, "firmware_code": 42}
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                prepare()

    def test_mode_change_during_probe_cancels_before_engine_start(self):
        w = self.window
        PreparedSession.probe_entered = threading.Event()
        PreparedSession.probe_release = threading.Event()
        with patch("leanwatts_can.gui.BenchSession", PreparedSession), \
             patch("leanwatts_can.production_flow.AutomationEngine", PreparedEngine), \
             patch("leanwatts_can.session_page.DemoBench", side_effect=AssertionError("No demo fallback")):
            self.page.start("calibration")
            self.assertTrue(PreparedSession.probe_entered.wait(1))
            w.legacy_page.mode_select.setCurrentIndex(0)
            PreparedSession.probe_release.set()
            self.finish()
        self.assertEqual(sum(engine.start_calls for engine in PreparedEngine.instances), 0)
        self.assertEqual(self.page.snapshot["unit_result"]["outcome"], "STOPPED")
        self.assertEqual(w.session_store.active()["units"][-1]["outcome"], "STOPPED")
        self.assertTrue(all(w.tabs.isTabEnabled(i) for i in range(w.tabs.count())))

    # Known bug: SessionPage.set_mode() stops a busy run and returns before storing the new
    # mode (session_page.py set_mode), so the page stays in the old mode while the header and
    # Manual Calibration switch. Fails safe (live start is refused). Remove this decorator
    # with the fix.
    @unittest.expectedFailure
    def test_mode_change_during_run_leaves_session_page_in_the_new_mode(self):
        # Suspected app bug: SessionPage.set_mode() only stops a busy run and returns
        # without recording the new mode, so after the run the session page stays
        # live while Manual Calibration and the header badge show SIMULATION.
        w = self.window
        PreparedSession.probe_entered = threading.Event()
        PreparedSession.probe_release = threading.Event()
        with patch("leanwatts_can.gui.BenchSession", PreparedSession), \
             patch("leanwatts_can.production_flow.AutomationEngine", PreparedEngine):
            self.page.start("calibration")
            self.assertTrue(PreparedSession.probe_entered.wait(1))
            w.legacy_page.mode_select.setCurrentIndex(0)
            PreparedSession.probe_release.set()
            self.finish()
        self.assertEqual(w.mode_badge.text(), "SIMULATION")
        self.assertEqual(self.page._mode, "simulation")

    def test_operator_and_setup_fit_supported_window_without_outer_scroll(self):
        w = self.window
        minimum = w.minimumSize()
        self.assertLessEqual((minimum.width(), minimum.height()), (1040, 680))
        w.resize(minimum)
        w.show()
        setup = w.load_setup
        checks = ((self.page, None, (self.page.calibrate, self.page.test_only, self.page.stop_button,
                                     self.page.open_report, self.page.setup_button,
                                     self.page.current.value, self.page.voltage.value)),
                  (setup, 0, (setup.port, setup.refresh_button, setup.profile_button)),
                  (setup, 1, (setup.probe_button, setup.cc_button, setup.cv_button,
                              setup.readings_match, setup.termination_ramp_approved, setup.manual_setup_button)))
        for page, inner_tab, controls in checks:
            w.tabs.setCurrentWidget(page)
            if inner_tab is not None:
                setup.tabs.setCurrentIndex(inner_tab)
            self.app.processEvents()
            self.assertEqual(w.size(), minimum)
            self.assertEqual(page.findChildren(QScrollArea), [])
            for control in controls:
                self.assertTrue(control.isVisible(), control.objectName() or str(control))
                self.assertTrue(w.rect().contains(control.mapTo(w, control.rect().bottomRight())), control.objectName() or str(control))
        self.assertEqual(w.tabs.count(), 8)


if __name__ == "__main__":
    unittest.main()
