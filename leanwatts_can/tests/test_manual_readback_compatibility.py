"""Parameter readback is diagnostic and independent of manual calibration."""
import time
import unittest

from PySide6.QtWidgets import QApplication

from leanwatts_can.legacy_page import LegacyChargerPage
from leanwatts_can.protocol import CONFIG, encode_all_config, poll
from leanwatts_can.tests.test_live_control_queue import DeferredExecutor, FakeClient, FakeEvent


def readback_frames(values, adjustable=False):
    groups = {}
    for key, spec in CONFIG.items():
        if spec["response"] == 0x18FF56E5 and not adjustable:
            continue
        data = groups.setdefault(spec["response"], [0] * 8)
        raw, offset = round(values[key] * spec["scale"]), spec["offset"]
        if spec.get("width") == 1:
            data[offset] = raw
        else:
            data[offset:offset + 2] = [raw >> 8, raw & 255]
    return [{"id": identity, "extended": True, "remote": False, "dlc": 8, "data": data}
            for identity, data in groups.items()]


class ManualReadbackIndependenceTests(unittest.TestCase):
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
        self.values = dict.fromkeys(CONFIG, 0.0)
        self.values.update(endVoltage=58.4, ccCurrent=15, endCurrent=.5)
        for key, value in self.values.items():
            p.settings[key].setText(str(value))
        p._live_shutdown_requested = True

    def tearDown(self):
        self.page.dispose()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def send_parameters(self):
        p = self.page
        p.send_settings()
        p._live_cancel_send = FakeEvent()
        p._live_executor.run_next()
        p._live_tick()
        p._live_next_poll = p._live_next_receive = float("inf")

    def test_twelve_readbacks_remain_unverified_without_blocking_manual_start(self):
        p = self.page
        self.send_parameters()
        p._receive_live({"frames": readback_frames(self.values)})
        p._refresh_controls()
        self.assertTrue(p.buttons["calibration_start"].isEnabled())
        self.assertTrue(p.buttons["work_start"].isEnabled())
        self.assertFalse(p._live_verified)
        p._live_deadline = time.monotonic() - 1
        p._live_tick()
        self.assertFalse(p._live_verified)
        self.assertTrue(p.buttons["calibration_start"].isEnabled())
        self.assertIn("18FF56E5", p.banner.text())
        self.assertTrue(all(p.readbacks[key].text() == "—" for key, spec in CONFIG.items() if spec["response"] == 0x18FF56E5))

    def test_late_complete_readback_promotes_only_parameter_report_without_tx(self):
        p = self.page
        self.send_parameters()
        before = list(self.client.calls)
        p._receive_live({"frames": readback_frames(self.values, adjustable=True)})
        p._refresh_controls()
        self.assertTrue(p._live_verified)
        self.assertEqual(self.client.calls, before)
        self.assertEqual(p.running_mode, "stopped")

    def test_mismatch_revokes_parameter_verification_but_not_manual_controls(self):
        p = self.page
        self.send_parameters()
        p._receive_live({"frames": readback_frames(self.values, adjustable=True)})
        changed = dict(self.values, ccCurrent=14)
        p._receive_live({"frames": readback_frames(changed, adjustable=True)})
        p._refresh_controls()
        self.assertFalse(p._live_verified)
        self.assertTrue(p.buttons["calibration_start"].isEnabled())
        self.assertNotIn("start, adjustment and save are blocked", p.trace.toPlainText())

    def test_partial_or_failed_write_never_reports_verified_parameters(self):
        p = self.page
        frames = encode_all_config(self.values) + [poll(group) for group in range(1, 7)]
        for result in ({"accepted_frames": frames[:-1]}, {"accepted_frames": frames, "error": "failed"}):
            p.pending_settings = dict(self.values)
            with self.assertRaises(RuntimeError):
                p._complete_live("send_settings", result)
            p._receive_live({"frames": readback_frames(self.values, adjustable=True)})
            self.assertFalse(p._live_verified)

    def test_stale_or_old_write_does_not_synthesize_parameter_confirmation(self):
        p = self.page
        self.send_parameters()
        p._receive_live({"frames": readback_frames(self.values)})
        p._live_observed_at = {key: time.monotonic() - 20 for key in p._live_observed_at}
        p._receive_live({"frames": [frame for frame in readback_frames(self.values, adjustable=True) if frame["id"] == 0x18FF56E5]})
        self.assertFalse(p._live_verified)
        p._live_written_values = None
        p._receive_live({"frames": readback_frames(self.values, adjustable=True)})
        self.assertFalse(p._live_verified)


if __name__ == "__main__":
    unittest.main()
