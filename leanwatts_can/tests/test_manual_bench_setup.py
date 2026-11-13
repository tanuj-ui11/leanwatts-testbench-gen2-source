"""Direct bench setup from the original-layout page; fake transport only."""
import unittest
from concurrent.futures import Future

from PySide6.QtWidgets import QApplication

from leanwatts_can.legacy_page import LegacyChargerPage
from leanwatts_can.protocol import CONFIG
from leanwatts_can.tests.test_live_control_queue import DeferredExecutor, FakeClient, FakeEvent


class ManualBenchSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.client = FakeClient()
        self.page = LegacyChargerPage(monitor_factory=lambda: self.client)
        p = self.page
        p.mode_select.setCurrentIndex(2)
        p._live_timer.stop()
        p._live_client = self.client
        p._live_executor = DeferredExecutor()
        p.device_open = p.can_started = True
        p._live_next_poll = p._live_next_receive = float("inf")
        p.settings["endVoltage"].setText("58.4")
        p.settings["ccCurrent"].setText("15")
        p.settings["endCurrent"].setText("0.5")
        p.settings["blindCharge"].setText("1")
        self.original_settings = {key: edit.text() for key, edit in p.settings.items()}
        p._refresh_controls()

    def tearDown(self):
        dialog = self.page._bench_limits_dialog
        if dialog is not None:
            dialog.reject()
        self.page.dispose()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def fill(self, dialog, values=(100, 30, 3000)):
        for key, value in zip(("max_voltage", "max_current", "max_power"), values):
            dialog.limit_fields[key].setText(str(value))

    def open_dialog(self):
        self.page.bench_limits_button.click()
        dialog = self.page._bench_limits_dialog
        self.assertIsNotNone(dialog)
        return dialog

    def test_manual_dialog_apply_preserves_all_values_and_sends_nothing(self):
        p = self.page
        dialog = self.open_dialog()
        self.assertTrue(all(edit.text() == "" for edit in dialog.limit_fields.values()))
        self.fill(dialog)
        dialog.apply_button.click()
        self.assertIsNone(p._bench_limits_dialog)
        self.assertEqual(p.bench_limits, {"max_voltage": 100.0, "max_current": 30.0, "max_power": 3000.0})
        self.assertEqual({key: edit.text() for key, edit in p.settings.items()}, self.original_settings)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(p._live_executor.jobs, [])
        self.assertIsNone(p.pending_settings)
        self.assertFalse(p._live_verified)
        self.assertIn("Shutdown", p.banner.text())
        again = self.open_dialog()
        self.assertEqual([edit.text() for edit in again.limit_fields.values()], ["100", "30", "3000"])

    def test_blank_invalid_zero_negative_and_nonfinite_limits_do_not_apply(self):
        p = self.page
        dialog = self.open_dialog()
        dialog.apply_button.click()
        self.assertIn("all three", dialog.validation_message.text())
        for invalid in ("wrong", "0", "-1", "nan", "inf", "-inf"):
            with self.subTest(invalid=invalid):
                self.fill(dialog)
                dialog.limit_fields["max_power"].setText(invalid)
                dialog.apply_button.click()
                self.assertIsNone(p.bench_limits)
                self.assertIs(p._bench_limits_dialog, dialog)
                self.assertEqual(self.client.calls, [])
                self.assertEqual({key: edit.text() for key, edit in p.settings.items()}, self.original_settings)
                self.assertTrue(dialog.validation_message.text())

    def test_background_receive_and_poll_allow_apply_without_transport_work(self):
        p = self.page
        for operation in ("receive", "readback_poll"):
            with self.subTest(operation=operation):
                future = Future()
                p._live_future = future
                p._live_operation = operation
                p._refresh_controls()
                self.assertTrue(p.bench_limits_button.isEnabled())
                dialog = self.open_dialog()
                self.fill(dialog, (90, 25, 2000))
                dialog.apply_button.click()
                self.assertEqual(p.bench_limits["max_voltage"], 90)
                self.assertIs(p._live_future, future)
                self.assertEqual(self.client.calls, [])
                self.assertEqual(p._live_executor.jobs, [])

    def test_open_dialog_revalidates_busy_readback_close_and_dispose(self):
        p = self.page
        dialog = self.open_dialog()
        self.fill(dialog)
        p._live_future = Future()
        p._live_operation = "workStart"
        p._refresh_controls()
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.apply_button.clicked.emit()  # Even stale/queued signals revalidate.
        self.assertIsNone(p.bench_limits)
        p._live_future = None
        p.pending_settings = {key: float(edit.text()) for key, edit in p.settings.items()}
        p._refresh_controls()
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.apply_button.clicked.emit()
        self.assertIsNone(p.bench_limits)
        self.assertIn("readback", dialog.validation_message.text())
        p.pending_settings = None
        p._live_future = Future()
        p._live_operation = "receive"
        p.close_live()
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.apply_button.clicked.emit()
        self.assertIsNone(p.bench_limits)
        p.dispose()
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.apply_button.clicked.emit()
        self.assertIsNone(p.bench_limits)
        self.assertFalse(any(call[0] == "send" for call in self.client.calls))

    def test_button_disabled_outside_manual_mode(self):
        p = self.page
        p.device_open = p.can_started = False
        p._live_client = None
        for mode in (0, 1):
            p.mode_select.setCurrentIndex(mode)
            p._live_timer.stop()
            self.assertFalse(p.bench_limits_button.isEnabled())
            with self.assertRaises(ValueError):
                p._show_bench_limits_dialog()
        self.assertEqual(self.client.calls, [])

    def test_limits_and_shutdown_enable_manual_calibration_independent_of_parameter_readback(self):
        p = self.page
        p.shutdown()
        p._live_executor.run_next()
        p._live_tick()
        self.assertTrue(p._live_shutdown_requested)
        self.assertFalse(p.buttons["send_settings"].isEnabled())
        self.assertIn("Bench limits in the top bar", p.banner.text())
        previous_calls = list(self.client.calls)
        dialog = self.open_dialog()
        self.fill(dialog)
        dialog.apply_button.click()
        self.assertEqual(self.client.calls, previous_calls)
        self.assertEqual({key: edit.text() for key, edit in p.settings.items()}, self.original_settings)
        self.assertTrue(p.buttons["send_settings"].isEnabled())
        self.assertTrue(p.buttons["calibration_start"].isEnabled())
        p.send_settings()
        p._live_cancel_send = FakeEvent()
        p._live_executor.run_next()
        p._live_tick()
        self.assertTrue(p.buttons["calibration_start"].isEnabled())
        requested = dict(p.pending_settings)
        response_data = {}
        for key, spec in CONFIG.items():
            data = response_data.setdefault(spec["response"], [0] * 8)
            raw = round(requested[key] * spec["scale"])
            if spec.get("width") == 1:
                data[spec["offset"]] = raw
            else:
                data[spec["offset"]:spec["offset"] + 2] = [raw >> 8, raw & 255]
        frames = [{"id": identifier, "dlc": 8, "data": data, "extended": True, "remote": False}
                  for identifier, data in response_data.items()]
        p._receive_live({"frames": [frame for frame in frames if frame["id"] != 0x18FF56E5]})
        p._refresh_controls()
        self.assertTrue(p.buttons["calibration_start"].isEnabled())
        self.assertIn("18FF56E5", p.banner.text())
        p._receive_live({"frames": [frame for frame in frames if frame["id"] == 0x18FF56E5]})
        p._refresh_controls()
        self.assertTrue(p.buttons["calibration_start"].isEnabled())
        self.assertTrue(p._live_verified)

    def test_disabled_send_reason_shows_numeric_and_limit_errors(self):
        p = self.page
        dialog = self.open_dialog()
        self.fill(dialog, (100, 30, 500))
        dialog.apply_button.click()
        p._live_shutdown_requested = True
        p._refresh_controls()
        self.assertFalse(p.buttons["send_settings"].isEnabled())
        self.assertIn("targets exceed", p.buttons["send_settings"].toolTip())
        self.assertIn("Cal. start is ready", p.banner.text())
        self.assertNotIn("recipe", p.banner.text().lower())
        p.settings["ccCurrent"].setText("bad")
        p._refresh_controls()
        self.assertIn("all 16 variant settings as numbers", p.buttons["send_settings"].toolTip())
        self.assertTrue(p.buttons["calibration_start"].isEnabled())
        self.assertFalse(p.buttons["send_settings"].isEnabled())

    def test_compact_dialog_fits_1024_by_650(self):
        p = self.page
        p.resize(1024, 650)
        p.show()
        dialog = self.open_dialog()
        self.app.processEvents()
        self.assertLessEqual(dialog.width(), 460)
        self.assertLess(dialog.height(), 500)
        self.assertIn("· Manual calibration |", p.banner.text())
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
