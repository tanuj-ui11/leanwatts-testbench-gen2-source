"""Real-mode routing and failure handling with a fake transport, never hardware."""
import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from leanwatts_can.legacy_page import LegacyChargerPage
from leanwatts_can.protocol import CONFIG


class FakeClient:
    def __init__(self):
        self.calls = []
        self.frames = []
        self.values = None
        self.fail_open = False
        self.fail_send = False

    def open(self, index=0, channel=0):
        self.calls.append(("open", index, channel))
        if self.fail_open:
            raise OSError("Driver unavailable")
        return {"state": "open", "listen_only": True}

    def start(self, bitrate=250000, listen_only=True):
        self.calls.append(("start", bitrate, listen_only))
        return {"state": "listening" if listen_only else "active", "listen_only": listen_only}

    def receive(self):
        frames, self.frames = self.frames, []
        return {"frames": frames}

    def reset(self):
        self.calls.append(("reset",))
        return {"state": "open"}

    def close(self):
        self.calls.append(("close",))
        return {"state": "closed"}

    def send(self, frame):
        self.calls.append(("send", frame))
        if self.fail_send:
            raise OSError("Send timeout: outcome unknown")
        if frame["data"] == [0, 11, 0, 6] and self.values:
            records = {}
            for key, spec in CONFIG.items():
                data = records.setdefault(spec["response"], [0] * 8)
                raw = round(self.values[key] * spec["scale"])
                offset = spec["offset"]
                if spec.get("width") == 1:
                    data[offset] = raw
                else:
                    data[offset:offset+2] = [raw >> 8, raw & 255]
            self.frames += [{"id": identity, "extended": True, "remote": False, "error": False,
                             "dlc": 8, "data": data} for identity, data in records.items()]
        return {"accepted": 1, "acknowledged": False}


class LiveModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.client = FakeClient()
        self.page = LegacyChargerPage(monitor_factory=lambda: self.client)

    def tearDown(self):
        self.page.dispose()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def until(self, predicate, timeout=3):
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            self.app.processEvents()
            self.page._live_tick()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Timed out waiting for test UI state")

    def ready(self, mode=2):
        p = self.page
        p.mode_select.setCurrentIndex(mode)
        p.open_device()
        self.until(lambda: p.device_open and p._live_future is None)
        p.start_can()
        self.until(lambda: p.can_started and p._live_future is None)

    def prepare_variant(self):
        p = self.page
        p.set_bench_limits(100, 30, 3000)
        p.settings["endVoltage"].setText("58.4")
        p.settings["ccCurrent"].setText("15")
        p.settings["endCurrent"].setText("0.5")
        self.client.values = {key: float(edit.text()) for key, edit in p.settings.items()}
        p.shutdown()
        self.until(lambda: p._live_shutdown_requested and p._live_future is None)
        p.send_settings()
        self.until(lambda: p._live_verified and p._live_future is None)

    def test_mode_selection_has_no_hardware_side_effect(self):
        p = self.page
        p.mode_select.setCurrentIndex(2)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(p.readbacks["endVoltage"].text(), "—")
        self.assertEqual(p.telemetry["outputVoltage"].text(), "—")
        self.assertEqual(p.duty["voltage"].text(), "80.0")
        self.assertIn("not a measurement", p.duty["voltage"].toolTip())
        self.assertFalse(p.buttons["shutdown"].isEnabled())

    def test_receive_only_opens_real_client_and_blocks_all_commands(self):
        p = self.page
        self.ready(1)
        self.assertIn(("start", 250000, True), self.client.calls)
        for action in (p.shutdown, p.send_settings, p.calibration_start, p.save_voltage):
            with self.assertRaises(ValueError):
                action()
        self.assertFalse(any(call[0] == "send" for call in self.client.calls))
        self.assertIn("REAL device opened", p.trace.toPlainText())
        self.assertNotIn("opened in simulation", p.trace.toPlainText())

    def test_open_failure_never_falls_back_to_simulation(self):
        self.client.fail_open = True
        p = self.page
        p.mode_select.setCurrentIndex(2)
        p.open_device()
        self.until(lambda: bool(p.last_error))
        self.assertFalse(p.device_open)
        self.assertFalse(p.can_started)
        self.assertIn("Driver unavailable", p.last_error)
        self.assertNotIn("opened in simulation", p.trace.toPlainText())

    def test_settings_require_shutdown_and_actual_matching_readback(self):
        self.ready()
        p = self.page
        with self.assertRaises(ValueError):
            p.send_settings()
        with self.assertRaises(ValueError):
            p.work_start()
        self.prepare_variant()
        self.assertEqual(float(p.readbacks["endVoltage"].text()), 58.4)
        self.assertTrue(p._live_verified)
        self.assertIn("ACTUAL CAN readbacks match", p.trace.toPlainText())
        self.assertIn("API ACCEPTED", p.trace.toPlainText())
        self.assertNotIn("SIMULATED readback", p.trace.toPlainText())

    def test_changed_variant_inputs_do_not_block_manual_start(self):
        self.ready()
        self.prepare_variant()
        self.page.settings["ccCurrent"].setText("14")
        self.page.calibration_start()
        self.until(lambda: self.page.running_mode == "calibration_requested" and self.page._live_future is None)

    def test_stale_configuration_readback_does_not_block_work_start(self):
        self.ready()
        self.prepare_variant()
        self.page._live_observed_at = {key: time.monotonic() - 9 for key in CONFIG}
        self.page.work_start()
        self.until(lambda: self.page.running_mode == "work_requested" and self.page._live_future is None)

    def test_new_mismatching_actual_readback_revokes_verification(self):
        self.ready()
        self.prepare_variant()
        p = self.page
        spec = CONFIG["endVoltage"]
        data = [0] * 8
        raw = round(57 * spec["scale"])
        data[spec["offset"]:spec["offset"]+2] = [raw >> 8, raw & 255]
        p._receive_live({"frames": [{"id": spec["response"], "extended": True, "remote": False,
                                     "error": False, "dlc": 8, "data": data}]})
        self.assertFalse(p._live_verified)
        p.calibration_start()
        self.until(lambda: p.running_mode == "calibration_requested" and p._live_future is None)
        self.assertFalse(p._live_verified)

    def test_explicit_bench_limits_are_required_and_enforced(self):
        self.ready()
        p = self.page
        p.settings["endVoltage"].setText("58.4")
        p.settings["ccCurrent"].setText("15")
        p.shutdown()
        self.until(lambda: p._live_shutdown_requested and p._live_future is None)
        with self.assertRaises(ValueError):
            p.send_settings()
        p.set_bench_limits(100, 30, 700)
        with self.assertRaises(ValueError):
            p.send_settings()
        self.assertEqual(len([call for call in self.client.calls if call[0] == "send"]), 1)

    def test_duty_and_save_are_explicit_and_do_not_claim_ack(self):
        self.ready()
        self.prepare_variant()
        p = self.page
        p.calibration_start()
        self.until(lambda: p.running_mode == "calibration_requested" and p._live_future is None)
        self.assertEqual(p._live_applied_duties, {"voltage": 80.0, "current": 80.0})
        self.assertTrue(p.buttons["save_voltage"].isEnabled())
        p.duty["current"].setText("81")
        p.duty["voltage"].setText("80")
        count = len(self.client.calls)
        p.duty["current"].editingFinished.emit()
        self.assertEqual(len(self.client.calls), count)
        with self.assertRaises(ValueError):
            p.save_voltage()
        p.adjust_duty("current", .1)
        self.until(lambda: p._live_applied_duties is not None and p._live_future is None)
        p.save_full_current()
        self.until(lambda: p._live_future is None)
        self.assertEqual(self.client.calls[-1][1]["data"], [0, 24])
        self.assertIn("persistence are NOT confirmed", p.trace.toPlainText())

    def test_failure_stops_connection_without_retry_or_fake_shutdown(self):
        self.ready()
        self.client.fail_send = True
        self.page.shutdown()
        self.until(lambda: bool(self.page.last_error))
        sends = [call for call in self.client.calls if call[0] == "send"]
        self.assertEqual(len(sends), 1)
        self.assertFalse(self.page.can_started)
        self.assertFalse(self.page._live_shutdown_requested)
        self.assertIn("output state is unknown", self.page.trace.toPlainText())

    def test_close_and_mode_switch_never_transmit(self):
        self.ready(1)
        p = self.page
        p.mode_select.setCurrentIndex(0)
        self.assertEqual(p.live_mode, "receive")
        p.close_live()
        self.until(lambda: not p.device_open and p._live_future is None)
        p.mode_select.setCurrentIndex(0)
        self.assertEqual(p.live_mode, "simulation")
        self.assertFalse(any(call[0] == "send" for call in self.client.calls))

    def test_missing_parameter_readback_remains_unverified_without_blocking_manual_start(self):
        self.ready()
        p = self.page
        p.set_bench_limits(100, 30, 3000)
        p.settings["endVoltage"].setText("58.4")
        p.settings["ccCurrent"].setText("15")
        p.shutdown()
        self.until(lambda: p._live_shutdown_requested and p._live_future is None)
        p.send_settings()
        self.until(lambda: p._live_future is None)
        self.assertIsNotNone(p.pending_settings)
        p._live_deadline = time.monotonic() - 1
        p._live_tick()
        self.assertFalse(p._live_verified)
        self.assertTrue(p.buttons["calibration_start"].isEnabled())


if __name__ == "__main__":
    unittest.main()
