"""Read-only driver-state classification and evidence parsing, without device I/O."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from leanwatts_can import device_diagnostics as d


def node(code=0, flags=d.DN_STARTED):
    return {"instance_id": "USB\\VID_0471&PID_1200\\TEST", "name": "ZLG USBCAN",
            "problem_code": code, "status_flags": flags, "status_error": ""}


class DeviceDiagnosticsTests(unittest.TestCase):
    def test_exact_hardware_id_only(self):
        self.assertTrue(d._matches_adapter("usb\\vid_0471&pid_1200\\1"))
        for text in ["USB\\VID_0471&PID_12001\\1", "USB\\VID_0471&PID_1201\\1", None, "PCI\\VID_0471&PID_1200"]:
            self.assertFalse(d._matches_adapter(text))

    def test_absent_is_not_found(self):
        result = d._classify([], [])
        self.assertEqual(result["state"], "not_found")
        self.assertFalse(result["communication_tested"])

    def test_ready_requires_started_clean_status(self):
        self.assertEqual(d._classify([node()], [])["state"], "driver_ready")
        for flags in [0, d.DN_STARTED | d.DN_HAS_PROBLEM, d.DN_STARTED | d.DN_PRIVATE_PROBLEM]:
            self.assertEqual(d._classify([node(flags=flags)], [])["state"], "driver_blocked")

    def test_code39_explains_driver_load_failure(self):
        result = d._classify([node(39, d.DN_HAS_PROBLEM)], [])
        self.assertEqual(result["state"], "driver_blocked")
        self.assertTrue(any("Code 39" in line for line in result["details"]))

    def test_incomplete_inventory_and_status_fail_closed(self):
        self.assertEqual(d._classify([node()], ["partial enumeration"])["state"], "error")
        for field in ("problem_code", "status_flags"):
            item = node()
            item[field] = None
            self.assertEqual(d._classify([item], [])["state"], "error")

    def test_one_bad_device_blocks_multiple(self):
        self.assertEqual(d._classify([node(), node(39, d.DN_HAS_PROBLEM)], [])["state"], "driver_blocked")

    def test_event_xml_parses_without_localized_messages(self):
        xml = """<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><EventID>3111</EventID><TimeCreated SystemTime='2026-11-10T05:21:24Z'/><EventRecordID>123</EventRecordID></System><EventData><Data Name='FileNameBuffer'>\\Device\\Volume3\\drivers\\usbcan.sys</Data></EventData></Event>"""
        events = d._parse_event_xml(xml + xml)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event_id"], 3111)
        self.assertEqual(len(d._matching_recent_events(events, datetime(2026, 11, 10, 6, tzinfo=timezone.utc))), 2)

    def test_old_future_and_other_driver_events_excluded(self):
        now = datetime.now(timezone.utc)
        valid = {"event_id": 3111, "timestamp": now.isoformat(), "data_values": [r"\SystemRoot\drivers\usbcan.sys"]}
        self.assertEqual(len(d._matching_recent_events([valid], now)), 1)
        for patch_data in [{"timestamp": (now-timedelta(days=8)).isoformat()}, {"timestamp": (now+timedelta(days=1)).isoformat()}, {"data_values": [r"\drivers\not-usbcan.sys"]}, {"event_id": 3077}]:
            self.assertEqual(d._matching_recent_events([dict(valid, **patch_data)], now), [])

    def test_no_event_lookup_when_windows_ready(self):
        with patch.object(d.sys, "platform", "win32"), patch.object(d, "_enumerate_adapter_nodes", return_value=([node()], [])), patch.object(d, "_query_code_integrity_events") as query:
            result = d.check_adapter()
        query.assert_not_called()
        self.assertEqual(result["state"], "driver_ready")
        self.assertFalse(result["communication_tested"])

    def test_optional_event_failure_does_not_hide_code39(self):
        with patch.object(d.sys, "platform", "win32"), patch.object(d, "_enumerate_adapter_nodes", return_value=([node(39, d.DN_HAS_PROBLEM)], [])), patch.object(d, "_query_code_integrity_events", return_value=([], "access denied")):
            result = d.check_adapter()
        self.assertEqual(result["state"], "driver_blocked")
        self.assertIn("access denied", result["details"])

    def test_native_inventory_failure_is_reported(self):
        with patch.object(d.sys, "platform", "win32"), patch.object(d, "_enumerate_adapter_nodes", side_effect=OSError("denied")):
            self.assertEqual(d.check_adapter()["state"], "error")


if __name__ == "__main__":
    unittest.main()
