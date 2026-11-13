"""Test-only handoff and setup must not require or invoke calibration controls."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from leanwatts_can.gui import CalibrationWindow
from leanwatts_can.tests.test_session_ui_v8 import profile


class TestOnlyConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_test_only_ignores_blank_trim_fields_and_closes_manual_can_before_open(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch("leanwatts_can.maynuo.MaynuoLoad.open", side_effect=AssertionError("No load I/O")), \
             patch("leanwatts_can.can_monitor.MonitorClient.open", side_effect=AssertionError("No CAN I/O")):
            window = CalibrationWindow(Path(folder))
            try:
                window.legacy_page.mode_select.setCurrentIndex(2)
                window.load_setup.port.setCurrentText("COM11")
                for field in window.load_setup.duty_fields.values():
                    field.clear()
                r = profile()
                r.device_settings = {}  # No programming fields needed for test-only.
                window.operator_page.set_profiles([dict(recipe=r.to_dict(), revision=1)])
                self.assertEqual(window._operator_live_reason("test_only"), "")
                self.assertTrue(window._operator_live_reason("calibration"))
                handoff = Future()
                handoff.set_result(dict(state="closed"))
                close = Mock(return_value=handoff)
                identity = window.load_setup.PROFILE_IDENTITY
                bench = Mock()
                bench.probe.return_value = dict(identity=dict(model_code=identity.model_code,
                    firmware_code=identity.firmware_code))
                with patch.object(window.legacy_page, "close_for_automatic_run", close), \
                     patch("leanwatts_can.gui.BenchSession", return_value=bench) as make:
                    prepare = window._operator_live_factory(r, "test_only", threading.Event())
                    close.assert_called_once_with()
                    make.assert_not_called()
                    result, settings = prepare()
                    self.assertIs(result, bench)
                    self.assertIsNone(make.call_args.kwargs["charger_parameters"])
                    # Tests use external_restart: no CAN start, ACK-only passive connection.
                    self.assertFalse(make.call_args.kwargs["post_test_can_start"])
                    self.assertEqual(make.call_args.kwargs["test_start_method"], "external_restart")
                    self.assertTrue(make.call_args.kwargs["standalone_ready"])
                    self.assertFalse(settings.commissioned)
                    self.assertEqual(settings.validate(calibration=False), "")
            finally:
                window.close()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
