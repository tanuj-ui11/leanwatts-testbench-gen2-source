"""Deterministic GUI scheduling tests; never load the CAN SDK or use hardware."""
import time
import unittest
from unittest.mock import patch
from concurrent.futures import Future

from PySide6.QtWidgets import QApplication

from leanwatts_can.legacy_page import LegacyChargerPage
from leanwatts_can.protocol import CONFIG, command, encode_duty, poll


class DeferredExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, callback):
        future = Future()
        self.jobs.append((callback, future))
        return future

    def run_next(self):
        callback, future = self.jobs.pop(0)
        try:
            future.set_result(callback())
        except Exception as exc:
            future.set_exception(exc)

    def shutdown(self, **kwargs):
        while self.jobs:
            self.run_next()


class FakeClient:
    def __init__(self):
        self.calls = []

    def send(self, frame):
        self.calls.append(("send", frame))
        return {"accepted": 1}

    def receive(self):
        self.calls.append(("receive",))
        return {"frames": []}

    def reset(self):
        self.calls.append(("reset",))
        return {"state": "open"}

    def close(self):
        self.calls.append(("close",))
        return {"state": "closed"}


class FakeEvent:
    def __init__(self):
        self.waits = []
        self.cancelled = False

    def is_set(self):
        return self.cancelled

    def set(self):
        self.cancelled = True

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.cancelled


class LiveControlQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.client = FakeClient()
        self.executor = DeferredExecutor()
        self.page = LegacyChargerPage(monitor_factory=lambda: self.client)
        p = self.page
        p.mode_select.setCurrentIndex(2)
        p._live_timer.stop()
        p._live_client = self.client
        p._live_executor = self.executor
        p.device_open = p.can_started = True
        p._live_next_receive = p._live_next_poll = float("inf")
        p.set_bench_limits(100, 30, 3000)
        p.settings["endVoltage"].setText("58.4")
        p.settings["ccCurrent"].setText("15")
        p.settings["endCurrent"].setText("0.5")
        p._live_verified_values = {key: float(edit.text()) for key, edit in p.settings.items()}
        p._live_observed = dict(p._live_verified_values)
        p._live_observed_at = {key: time.monotonic() for key in CONFIG}
        p._live_verified = p._live_shutdown_requested = True
        p.running_mode = "calibration_requested"
        p.duty["voltage"].setText("80.0")
        p.duty["current"].setText("80.0")
        p._live_applied_duties = {"voltage": 80.0, "current": 80.0}
        p._refresh_controls()

    def tearDown(self):
        self.page.dispose()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def background(self, operation="receive"):
        future = Future()
        self.page._live_future = future
        self.page._live_operation = operation
        self.page._refresh_controls()
        return future

    def finish_background(self, future, result=None):
        future.set_result({"frames": []} if result is None else result)
        self.page._live_tick()

    def sent(self):
        return [call[1] for call in self.client.calls if call[0] == "send"]

    def test_click_during_receive_queues_once_before_due_poll(self):
        p = self.page
        future = self.background()
        p._live_next_poll = 0
        self.assertTrue(p.buttons["calibration_start"].isEnabled())
        p.buttons["calibration_start"].click()
        p.buttons["calibration_start"].click()
        self.assertEqual(p._live_pending_operator[0], "calibrationStart")
        self.assertEqual(self.executor.jobs, [])
        self.assertEqual(self.client.calls, [])
        self.assertTrue(p._live_controls_busy())
        self.assertFalse(p.settings["endVoltage"].isEnabled())
        self.finish_background(future)
        self.assertEqual(p._live_operation, "calibrationStart")
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.run_next()
        self.assertEqual(self.sent(), [command("calibrationStart")]
                         + [encode_duty("voltage", 80), encode_duty("current", 80)] * 3)

    def test_receive_and_poll_keep_edit_focus_and_controls_enabled(self):
        p = self.page
        p.show()
        p.activateWindow()
        p.settings["endVoltage"].setFocus()
        self.app.processEvents()
        for operation in ("receive", "readback_poll") * 5:
            self.background(operation)
            self.assertFalse(p._live_controls_busy())
            self.assertTrue(p.settings["endVoltage"].isEnabled())
            self.assertTrue(p.settings["endVoltage"].hasFocus())
            self.assertTrue(p.duty["voltage"].isEnabled())
            self.assertTrue(p.buttons["calibration_start"].isEnabled())
            self.assertTrue(p.buttons["reset_can"].isEnabled())
        p.duty["voltage"].setFocus()
        p._refresh_controls()
        self.assertTrue(p.duty["voltage"].hasFocus())

    def test_dispatch_rechecks_displayed_duties_and_keeps_healthy_adapter(self):
        p = self.page
        future = self.background()
        p.calibration_start()
        p.duty["voltage"].setText("81")
        self.finish_background(future)
        self.assertIsNone(p._live_pending_operator)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.executor.jobs, [])
        self.assertTrue(p.device_open)
        self.assertIn("not sent", p.trace.toPlainText())

    def test_settings_snapshot_cannot_change_while_queued(self):
        p = self.page
        future = self.background()
        p.send_settings()
        self.assertIsNone(p.pending_settings)
        p.settings["ccCurrent"].setText("14")  # Programmatic changes also revalidate.
        self.finish_background(future)
        self.assertEqual(self.sent(), [])
        self.assertIsNone(p.pending_settings)
        self.assertIn("settings changed", p.trace.toPlainText())
        self.assertTrue(p.device_open)

    def test_settings_dispatch_clears_old_deadline_and_waits_for_readback(self):
        p = self.page
        future = self.background("readback_poll")
        p._live_deadline = time.monotonic() - 100
        p.send_settings()
        self.finish_background(future, {"accepted_frames": []})
        self.assertEqual(p._live_operation, "send_settings")
        self.assertEqual(p._live_deadline, 0)
        self.assertIsNotNone(p.pending_settings)
        p._live_cancel_send = FakeEvent()
        self.executor.run_next()
        p._live_tick()
        self.assertIsNotNone(p.pending_settings)
        self.assertGreater(p._live_deadline, time.monotonic())
        self.assertTrue(p.buttons["calibration_start"].isEnabled())

    def test_bench_limits_allowed_during_background_only(self):
        p = self.page
        self.background("readback_poll")
        p.set_bench_limits(90, 25, 2000)
        self.assertEqual(p.bench_limits["max_voltage"], 90)
        p._live_verified = True
        p._live_verified_values = p._live_values()
        p.calibration_start()
        with self.assertRaises(ValueError):
            p.set_bench_limits(100, 30, 3000)
        p._cancel_live_operator("test")
        p.pending_settings = p._live_values()
        with self.assertRaises(ValueError):
            p.set_bench_limits(100, 30, 3000)

    def test_background_failure_discards_action_without_replay(self):
        p = self.page
        future = self.background()
        p.calibration_start()
        future.set_exception(RuntimeError("receive failed"))
        p._live_tick()
        self.assertIsNone(p._live_pending_operator)
        self.assertFalse(p.can_started)
        for _ in range(3):
            p._live_tick()
        self.assertEqual(self.sent(), [])
        self.assertEqual(len(self.executor.jobs), 1)  # Cleanup only.

    def test_shutdown_supersedes_queued_start(self):
        p = self.page
        future = self.background()
        p.calibration_start()
        p.shutdown()
        p.shutdown()
        self.assertIsNone(p._live_pending_operator)
        self.finish_background(future)
        self.assertEqual(p._live_operation, "shutdown")
        self.executor.run_next()
        self.assertEqual(self.sent(), [command("shutdown")])
        self.assertIn("calibrationStart cancelled", p.trace.toPlainText())

    def test_reset_supersedes_queued_start_and_precedes_poll(self):
        p = self.page
        future = self.background()
        p._live_next_poll = 0
        p.calibration_start()
        self.assertTrue(p.buttons["reset_can"].isEnabled())
        p.buttons["reset_can"].click()
        p._live_reset()
        self.assertEqual(p._live_pending_operator[0], "reset")
        self.finish_background(future)
        self.assertEqual(p._live_operation, "reset")
        self.executor.run_next()
        p._live_tick()
        self.assertEqual(self.client.calls, [("reset",)])
        self.assertFalse(p.can_started)

    def test_close_cancels_queued_operator_before_sdk_call(self):
        p = self.page
        future = self.background()
        p.calibration_start()
        p.close_live()
        self.finish_background(future)
        self.executor.run_next()
        p._live_tick()
        self.assertEqual(self.client.calls, [("close",)])
        self.assertIsNone(p._live_pending_operator)

    def test_dispose_cancels_queued_operator_without_sending(self):
        p = self.page
        future = self.background()
        p.calibration_start()
        p.dispose()
        self.finish_background(future)
        self.assertEqual(self.client.calls, [("close",)])
        self.assertIsNone(p._live_pending_operator)

    def test_six_readback_requests_have_five_cancellable_100ms_gaps(self):
        token = FakeEvent()
        self.page._live_cancel_send = token
        frames = [poll(group) for group in range(1, 7)]
        result = self.page._send_batch(frames)
        self.assertEqual(result["accepted_frames"], frames)
        self.assertEqual(token.waits, [.1] * 5)

    def test_prerequisites_and_missing_adjustable_readback_are_visible(self):
        p = self.page
        p.bench_limits = None
        p._refresh_controls()
        self.assertIn("Bench limits in the top bar", p.banner.text())
        self.assertIn("· Manual calibration |", p.banner.text())
        self.assertFalse(p.buttons["calibration_start"].isEnabled())
        self.assertTrue(p.buttons["shutdown"].isEnabled())
        p.bench_limits = {"max_voltage": 100, "max_current": 30, "max_power": 3000}
        p.pending_settings = dict(p._live_verified_values)
        p._live_observed = {key: value for key, value in p.pending_settings.items()
                            if CONFIG[key]["response"] != 0x18FF56E5}
        p._refresh_controls()
        self.assertIn("18FF56E5", p.banner.text())
        p._live_deadline = time.monotonic() - 1
        p._live_tick()
        self.assertIn("adjustVoltageMax", p.trace.toPlainText())
        self.assertIn("18FF56E5", p.trace.toPlainText())
        self.assertIn("Last readback failed: 4 missing", p.banner.text())
        p._refresh_controls()
        self.assertIn("18FF56E5", p.banner.text())
        self.assertTrue(p.buttons["calibration_start"].isEnabled())

    def test_failed_send_logs_attempted_frame_without_claiming_acceptance(self):
        frame = command("shutdown")
        with patch.object(self.client, "send", side_effect=RuntimeError("SDK rejected frame")):
            result = self.page._send_batch([frame])
        self.assertEqual(result["accepted_frames"], [])
        self.assertEqual(result["unconfirmed_frame"], frame)
        with self.assertRaisesRegex(RuntimeError, "SDK rejected frame"):
            self.page._complete_live("shutdown", result)
        self.assertEqual(self.page.frame_log[-1]["status"], "SEND UNCONFIRMED")
        self.assertEqual(self.page.frame_log[-1]["data"], [0, 20, 0, 128])
        self.assertNotIn("API ACCEPTED", self.page.trace.toPlainText())

    def test_drain_failure_does_not_claim_a_frame_was_attempted(self):
        with patch.object(self.client, "receive", side_effect=RuntimeError("receive failed")):
            result = self.page._send_batch([command("shutdown")], drain=True)
        self.assertEqual(result["accepted_frames"], [])
        self.assertIsNone(result["unconfirmed_frame"])
        self.assertEqual(self.sent(), [])


if __name__ == "__main__":
    unittest.main()
