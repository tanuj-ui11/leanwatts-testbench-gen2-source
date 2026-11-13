"""Original manual flow, without Send data or configuration-readback gates."""
import time
import unittest
from concurrent.futures import Future

from PySide6.QtWidgets import QApplication

from leanwatts_can.legacy_page import LegacyChargerPage
from leanwatts_can.protocol import command, encode_duty
from leanwatts_can.tests.test_live_control_queue import DeferredExecutor, FakeClient, FakeEvent


class ManualCalibrationFlowTests(unittest.TestCase):
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
        p.set_bench_limits(100, 30, 3000)
        p._refresh_controls()

    def tearDown(self):
        self.page.dispose()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def sends(self):
        return [call[1] for call in self.client.calls if call[0] == "send"]

    def finish_worker(self):
        p = self.page
        token = FakeEvent()
        if p._live_cancel_send.is_set():
            token.set()
        p._live_cancel_send = token
        p._live_executor.run_next()
        p._live_tick()
        p._live_next_poll = p._live_next_receive = float("inf")
        return token

    def shutdown(self):
        self.page.buttons["shutdown"].click()
        self.finish_worker()
        self.assertTrue(self.page._live_shutdown_requested)

    def startup_frames(self, voltage=80, current=80):
        return [command("calibrationStart")] + [encode_duty("voltage", voltage), encode_duty("current", current)] * 3

    def test_real_button_flow_from_all_zero_settings_and_no_readbacks(self):
        p = self.page
        self.assertTrue(all(float(edit.text()) == 0 for edit in p.settings.values()))
        self.assertEqual(p._live_observed, {})
        self.assertEqual([edit.text() for edit in p.duty.values()], ["80.0", "80.0"])
        self.assertFalse(p.buttons["calibration_start"].isEnabled())
        self.shutdown()
        self.assertTrue(p.buttons["calibration_start"].isEnabled())
        self.assertTrue(p.duty["voltage"].isEnabled())
        self.assertIn("Cal. start is ready", p.banner.text())
        self.assertIn("Send data optional", p.banner.text())
        self.assertNotIn("positive end voltage", p.banner.text())
        p.buttons["calibration_start"].click()
        self.assertIsNone(p._live_applied_duties)
        token = self.finish_worker()
        self.assertEqual(token.waits, [.1, .1, .1])
        self.assertEqual(p._live_applied_duties, {"voltage": 80.0, "current": 80.0})
        self.assertTrue(p.buttons["save_voltage"].isEnabled())
        p.buttons["voltage_plus"].click()
        self.finish_worker()
        p.buttons["save_voltage"].click()
        self.finish_worker()
        self.assertEqual(self.sends(), [command("shutdown")] + self.startup_frames()
                         + [encode_duty("voltage", 80.1), encode_duty("current", 80)] * 3
                         + [command("saveVoltage")])
        self.assertFalse(p._live_verified)
        self.assertIsNone(p._live_written_values)
        self.assertEqual(p._live_observed, {})
        self.assertTrue(all(edit.text() == "—" for edit in p.readbacks.values()))

    def test_typing_is_local_startup_applies_displayed_duties_save_is_explicit(self):
        p = self.page
        self.shutdown()
        before = list(self.sends())
        p.duty["voltage"].setText("75.5")
        p.duty["current"].setText("78.2")
        p.duty["voltage"].editingFinished.emit()
        self.assertEqual(self.sends(), before)
        p.calibration_start()
        self.finish_worker()
        self.assertEqual(self.sends()[1:], self.startup_frames(75.5, 78.2))
        self.assertEqual(p._live_applied_duties, {"voltage": 75.5, "current": 78.2})
        self.assertTrue(p.buttons["save_full_current"].isEnabled())
        self.assertFalse(any(frame == command("saveFullCurrent") for frame in self.sends()))
        p.buttons["save_full_current"].click()
        self.finish_worker()
        self.assertEqual(self.sends()[-1], command("saveFullCurrent"))

    def test_work_start_ignores_configuration_and_invalid_duty_fields(self):
        p = self.page
        self.shutdown()
        for edit in p.settings.values():
            edit.clear()
        p.duty["voltage"].setText("invalid")
        p._refresh_controls()
        self.assertFalse(p.buttons["calibration_start"].isEnabled())
        self.assertTrue(p.buttons["work_start"].isEnabled())
        p.buttons["work_start"].click()
        token = self.finish_worker()
        self.assertEqual(self.sends(), [command("shutdown"), command("workStart")])
        self.assertEqual(token.waits, [])

    def test_current_save_then_voltage_calibration_restart_without_another_shutdown(self):
        p = self.page
        self.shutdown()
        p.buttons["calibration_start"].click()
        self.finish_worker()
        p.buttons["save_full_current"].click()
        self.finish_worker()
        self.assertTrue(p.buttons["calibration_start"].isEnabled())
        self.assertFalse(p.buttons["work_start"].isEnabled())
        p.buttons["calibration_start"].click()
        self.finish_worker()
        p.buttons["save_voltage"].click()
        self.finish_worker()
        self.assertEqual(self.sends(), [command("shutdown")] + self.startup_frames()
                         + [command("saveFullCurrent")] + self.startup_frames() + [command("saveVoltage")])

    def test_pending_missing_mismatched_stale_readback_never_blocks_manual_flow(self):
        p = self.page
        self.shutdown()
        p.pending_settings = dict.fromkeys(p.settings, 100.0)
        p._live_deadline = time.monotonic() + 8
        p._live_observed = {"ccCurrent": 99}
        p._live_observed_at = {"ccCurrent": time.monotonic() - 100}
        for edit in p.settings.values():
            edit.setText("not a setting")
        p._refresh_controls()
        self.assertTrue(p.buttons["calibration_start"].isEnabled())
        p.calibration_start()
        self.finish_worker()
        p._live_deadline = time.monotonic() - 1
        p._live_tick()
        self.assertEqual(p.running_mode, "calibration_requested")
        p._live_duty("current", .1)
        self.finish_worker()
        p.save_half_current()
        self.finish_worker()
        self.assertEqual(self.sends()[-1], command("saveHalfCurrent"))

    def test_missing_limits_invalid_duties_and_active_transmission_still_block(self):
        p = self.page
        self.shutdown()
        p.bench_limits = None
        with self.assertRaises(ValueError):
            p.calibration_start()
        p.set_bench_limits(100, 30, 3000)
        for value in ("", "bad", "nan", "-1", "1001"):
            with self.subTest(value=value):
                p.duty["voltage"].setText(value)
                with self.assertRaises(ValueError):
                    p.calibration_start()
        p.duty["voltage"].setText("80")
        p._live_future = Future()
        p._live_operation = "send_settings"
        p._refresh_controls()
        self.assertFalse(p.buttons["calibration_start"].isEnabled())
        with self.assertRaises(ValueError):
            p.calibration_start()
        self.assertEqual(self.sends(), [command("shutdown")])

    def test_queue_revalidates_duties_limits_shutdown_and_recent_output(self):
        p = self.page
        self.shutdown()
        for change in ("duty", "limits", "shutdown", "output"):
            with self.subTest(change=change):
                p.duty["voltage"].setText("80")
                p.bench_limits = {"max_voltage": 100, "max_current": 30, "max_power": 3000}
                p._live_shutdown_requested = True
                p._live_telemetry.clear()
                future = p._live_future = Future()
                p._live_operation = "receive"
                p.calibration_start()
                if change == "duty":
                    p.duty["voltage"].setText("81")
                elif change == "limits":
                    p.bench_limits["max_voltage"] = 90
                elif change == "shutdown":
                    p._live_shutdown_requested = False
                else:
                    p._live_telemetry["outputCurrent"] = 31
                    p._live_telemetry_at["outputCurrent"] = time.monotonic()
                future.set_result({"frames": []})
                p._live_tick()
                self.assertIsNone(p._live_pending_operator)
                self.assertEqual(self.sends(), [command("shutdown")])
                self.assertTrue(p.device_open)

    def test_queued_start_ignores_config_edits_and_missing_telemetry(self):
        p = self.page
        self.shutdown()
        future = p._live_future = Future()
        p._live_operation = "receive"
        p.calibration_start()
        p.settings["ccCurrent"].setText("arbitrary")
        p._live_observed_at = {"ccCurrent": 0}
        future.set_result({"frames": []})
        p._live_tick()
        self.finish_worker()
        self.assertEqual(self.sends(), [command("shutdown")] + self.startup_frames())

    def test_shutdown_before_startup_worker_cancels_all_seven_frames(self):
        p = self.page
        self.shutdown()
        p.calibration_start()
        p.shutdown()
        self.finish_worker()
        self.assertIsNone(p._live_applied_duties)
        self.finish_worker()
        self.assertEqual(self.sends(), [command("shutdown"), command("shutdown")])
        self.assertEqual(p.running_mode, "stop_requested")
        self.assertIsNone(p._live_pending_start)

    def test_shutdown_inside_initial_duty_pairs_cancels_remaining_startup(self):
        p = self.page
        self.shutdown()
        original_send = self.client.send
        startup_count = 0

        def interrupting_send(frame):
            nonlocal startup_count
            result = original_send(frame)
            startup_count += 1
            if startup_count == 3:
                p.shutdown()
            return result

        self.client.send = interrupting_send
        p.calibration_start()
        self.finish_worker()
        self.assertIsNone(p._live_applied_duties)
        self.finish_worker()
        self.assertEqual(self.sends(), [command("shutdown")] + self.startup_frames()[:3] + [command("shutdown")])
        self.assertEqual(p.running_mode, "stop_requested")

    def test_partial_startup_failure_never_marks_initial_duties_applied(self):
        p = self.page
        self.shutdown()
        original_send = self.client.send
        count = 0

        def failing_send(frame):
            nonlocal count
            count += 1
            if count == 4:
                raise RuntimeError("initial duty send failed")
            return original_send(frame)

        self.client.send = failing_send
        p.calibration_start()
        self.finish_worker()
        self.assertIsNone(p._live_applied_duties)
        self.assertIsNone(p._live_pending_start)
        self.assertFalse(p._live_shutdown_requested)
        self.assertFalse(p.can_started)
        self.assertEqual(p.running_mode, "unknown")
        self.assertEqual(self.sends(), [command("shutdown")] + self.startup_frames()[:3])

    def test_close_before_startup_worker_cannot_send_or_restore_applied_duties(self):
        p = self.page
        self.shutdown()
        p.calibration_start()
        p.close_live()
        self.finish_worker()
        self.assertEqual(self.sends(), [command("shutdown")])
        self.assertIsNone(p._live_pending_start)
        self.assertIsNone(p._live_applied_duties)
        self.assertFalse(p._live_shutdown_requested)
        self.assertEqual(p.running_mode, "unknown")


if __name__ == "__main__":
    unittest.main()
