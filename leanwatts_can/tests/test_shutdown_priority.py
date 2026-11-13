"""Operator stop scheduling with controlled fake calls; never access hardware."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from concurrent.futures import Future
import threading
import time
import unittest

from PySide6.QtWidgets import QApplication
from leanwatts_can.legacy_page import LegacyChargerPage
from leanwatts_can.protocol import CONFIG, command, encode_all_config, encode_duty, poll


class ControlledClient:
    def __init__(self):
        self.calls = []
        self.block_receive = False
        self.block_data = None
        self.fail_blocked = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def _block(self):
        self.entered.set()
        if not self.release.wait(2):
            raise TimeoutError("Test did not release its controlled call")
        if self.fail_blocked:
            raise OSError("Controlled SDK failure; outcome unknown")

    def receive(self):
        self.calls.append(("receive",))
        if self.block_receive:
            self._block()
        return {"frames": []}

    def send(self, frame):
        data = list(frame["data"])
        self.calls.append(("send", data))
        if data == self.block_data:
            self._block()
        return {"accepted": 1, "acknowledged": False}

    def close(self):
        self.calls.append(("close",))
        return {"state": "closed"}


class DeferredExecutor:
    """Hold callbacks before execution to make the pre-dispatch race exact."""
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
        except Exception as error:
            future.set_exception(error)

    def shutdown(self, **kwargs):
        while self.jobs:
            self.run_next()


class ShutdownPriorityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.client = ControlledClient()
        self.page = LegacyChargerPage(monitor_factory=lambda: self.client)
        self.page.mode_select.setCurrentIndex(2)
        self.page._live_timer.stop()
        self.page._live_client = self.client
        self.page.device_open = self.page.can_started = True
        self.page._live_next_poll = self.page._live_next_receive = float("inf")

    def tearDown(self):
        self.client.release.set()
        self.page.dispose()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def until(self, predicate, timeout=2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            self.page._live_tick()
            if predicate():
                return
            time.sleep(.002)
        self.fail("Controlled UI operation did not complete")

    def sends(self):
        return [item[1] for item in self.client.calls if item[0] == "send"]

    def test_shutdown_enabled_during_receive_and_precedes_due_poll(self):
        p = self.page
        self.client.block_receive = True
        p._live_submit("receive", self.client.receive)
        self.assertTrue(self.client.entered.wait(1))
        self.assertTrue(p.buttons["shutdown"].isEnabled())
        p._live_next_poll = 0
        p.buttons["shutdown"].click()
        p.shutdown()  # A second operator request while queued must not duplicate it.
        self.assertTrue(p._live_shutdown_pending)
        self.assertIn("Shutdown queued", p.banner.text())
        self.client.release.set()
        self.until(lambda: p._live_shutdown_requested and p._live_future is None)
        self.assertEqual(self.sends(), [[0, 20, 0, 128]])
        self.assertFalse(p._live_shutdown_pending)
        self.assertIn("physical output is unconfirmed", p.trace.toPlainText())
        self.assertEqual(p.running_mode, "stop_requested")

    def test_shutdown_interrupts_unsent_settings_and_keeps_adapter(self):
        p = self.page
        values = dict.fromkeys(CONFIG, 0)
        values.update(endVoltage=58.4, ccCurrent=15)
        frames = encode_all_config(values) + [poll(1)]
        self.client.block_data = list(frames[0]["data"])
        p.pending_settings = values
        p._live_verified = True
        p._live_verified_values = values
        p._live_submit("send_settings", lambda: p._send_batch(frames, drain=True))
        self.assertTrue(self.client.entered.wait(1))
        p.shutdown()
        self.assertIsNone(p.pending_settings)
        self.assertFalse(p._live_verified)
        self.client.release.set()
        self.until(lambda: p._live_shutdown_requested and p._live_future is None)
        self.assertEqual(self.sends(), [frames[0]["data"], [0, 20, 0, 128]])
        self.assertNotIn(("close",), self.client.calls)
        self.assertTrue(p.device_open)
        self.assertIn("Cancelled 16 unsent frame(s)", p.trace.toPlainText())
        self.assertIsNone(p.pending_settings)

    def test_shutdown_cancels_remaining_duty_pairs_without_reapplying_state(self):
        p = self.page
        frames = [encode_duty("voltage", 80), encode_duty("current", 80)] * 3
        self.client.block_data = frames[0]["data"]
        p._live_applied_duties = {"voltage": 79, "current": 79}
        p._live_pending_duties = {"voltage": 80, "current": 80}
        p._live_submit("duty_edit", lambda: p._send_batch(frames))
        self.assertTrue(self.client.entered.wait(1))
        p.shutdown()
        self.client.release.set()
        self.until(lambda: p._live_shutdown_requested and p._live_future is None)
        self.assertEqual(self.sends(), [frames[0]["data"], [0, 20, 0, 128]])
        self.assertIsNone(p._live_applied_duties)
        self.assertIsNone(p._live_pending_duties)

    def test_shutdown_cancels_start_before_worker_dispatch(self):
        p = self.page
        executor = p._live_executor = DeferredExecutor()
        p._live_submit("workStart", lambda: p._send_batch([command("workStart")]))
        p.shutdown()
        executor.run_next()
        p._live_tick()
        self.assertEqual(self.sends(), [])
        self.assertEqual(p._live_operation, "shutdown")
        executor.run_next()
        p._live_tick()
        self.assertEqual(self.sends(), [[0, 20, 0, 128]])
        self.assertEqual(p.running_mode, "stop_requested")

    def test_shutdown_cancels_save_before_worker_dispatch(self):
        p = self.page
        executor = p._live_executor = DeferredExecutor()
        p._live_submit("saveFullCurrent", lambda: p._send_batch([command("saveFullCurrent")]))
        p.shutdown()
        executor.run_next()
        p._live_tick()
        executor.run_next()
        p._live_tick()
        self.assertEqual(self.sends(), [[0, 20, 0, 128]])
        self.assertNotIn("Save frame accepted", p.trace.toPlainText())

    def test_old_completed_start_cannot_overwrite_shutdown_queued_state(self):
        p = self.page
        p._live_future = Future()
        p._live_operation = "calibrationStart"
        p._live_future.set_result({"accepted_frames": [command("calibrationStart")]})
        p.shutdown()
        self.until(lambda: p._live_shutdown_requested and p._live_future is None)
        self.assertEqual(p.running_mode, "stop_requested")
        self.assertIsNone(p._live_applied_duties)
        self.assertIn("Previous calibrationStart completed", p.trace.toPlainText())

    def test_pending_sdk_failure_discards_shutdown_without_retry(self):
        p = self.page
        self.client.block_receive = self.client.fail_blocked = True
        p._live_submit("receive", self.client.receive)
        self.assertTrue(self.client.entered.wait(1))
        p.shutdown()
        self.client.release.set()
        self.until(lambda: bool(p.last_error))
        self.assertFalse(p._live_shutdown_pending)
        self.assertFalse(p.can_started)
        self.assertEqual(self.sends(), [])
        p._live_tick()
        self.assertEqual(self.sends(), [])

    def test_close_cancels_queued_shutdown_and_mode_switch_sends_nothing(self):
        p = self.page
        self.client.block_receive = True
        p._live_submit("receive", self.client.receive)
        self.assertTrue(self.client.entered.wait(1))
        p.shutdown()
        p.close_live()
        self.client.release.set()
        self.until(lambda: not p.device_open and p._live_future is None)
        p.mode_select.setCurrentIndex(0)
        self.assertEqual(p.live_mode, "simulation")
        self.assertEqual(self.sends(), [])
        self.assertFalse(p._live_shutdown_pending)

    def test_dispose_cancels_shutdown_queued_behind_receive(self):
        p = self.page
        self.client.block_receive = True
        p._live_submit("receive", self.client.receive)
        self.assertTrue(self.client.entered.wait(1))
        p.shutdown()
        p.dispose()
        self.client.release.set()
        self.until(lambda: ("close",) in self.client.calls)
        self.assertEqual(self.sends(), [])
        self.assertFalse(p._live_shutdown_pending)

    def test_close_cancels_shutdown_worker_before_it_reaches_sdk(self):
        p = self.page
        executor = p._live_executor = DeferredExecutor()
        p.shutdown()
        self.assertEqual(p._live_operation, "shutdown")
        p.close_live()
        executor.run_next()
        p._live_tick()
        self.assertEqual(self.sends(), [])
        self.assertFalse(p._live_shutdown_pending)
        self.assertFalse(p.can_started)

    def test_dispose_cancels_shutdown_worker_before_it_reaches_sdk(self):
        p = self.page
        executor = p._live_executor = DeferredExecutor()
        p.shutdown()
        self.assertEqual(p._live_operation, "shutdown")
        p.dispose()  # DeferredExecutor drains callbacks after disposal is marked.
        self.assertEqual(self.sends(), [])
        self.assertFalse(p._live_shutdown_pending)

    def test_receive_only_rejects_shutdown_without_transmission(self):
        p = self.page
        p._live_client = None
        p.device_open = p.can_started = False
        p.mode_select.setCurrentIndex(1)
        p.device_open = p.can_started = True
        with self.assertRaises(ValueError):
            p.shutdown()
        self.assertEqual(self.sends(), [])


if __name__ == "__main__":
    unittest.main()
