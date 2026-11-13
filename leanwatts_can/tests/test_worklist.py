"""Work-order serial list: parsing, scan normalisation, classification and page wiring. No devices or network."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from leanwatts_can import theme
from leanwatts_can import worklist as wl
from leanwatts_can.gui import CalibrationWindow
from leanwatts_can.worklist import Worklist, classify, normalise_scan, split_serial


def entry(serial, seq, line_token, line_seq, **fields):
    base = dict(serial=serial, seq=seq, line_token=line_token, line_seq=line_seq, product=None,
                is_buffer_stock=False, rejected=False, reworked=False, replaced_by_serial=None, report=None)
    base.update(fields)
    return base


CONTRACT = {
    "wo_number": "WO-S00220", "work_order_id": "5c1f0a2e-0000-4000-8000-000000000220", "order": "S00220",
    "quantity": 6, "as_of": "2026-11-13T09:00:00+05:30",
    "serials": [
        entry("WO-S00220-1-001", 1, "1", 1),
        entry("WO-S00220-1-002", 2, "1", 2, rejected=True),
        entry("WO-S00220-1-003", 3, "1", 3, replaced_by_serial="WO-S00220-1-099"),
        entry("WO-S00220-BS1-004", 4, "BS1", 4, is_buffer_stock=True,
              report={"id": "r-4", "result": "pass", "tested_at": "2026-11-12T10:00:00+05:30",
                      "generated_at": "2026-11-12T10:05:00+05:30"}),
        entry("WO-S00220-1-036", 36, "1", 36),
        entry("WO-S00220-1-011", 11, "1", 11),
        entry("WO-S00220-BS1-011", 12, "BS1", 11),
    ],
}


def fixture():
    return Worklist.from_contract(CONTRACT)


class SplitAndNormaliseTests(unittest.TestCase):
    def test_standard_buffer_and_legacy_parse(self):
        self.assertEqual(split_serial("WO-S00429-1-045"), ("WO-S00429", "1", 45))
        self.assertEqual(split_serial("WO-S00318-BS1-001"), ("WO-S00318", "BS1", 1))
        self.assertEqual(split_serial("WO-S00125-001"), ("WO-S00125", None, 1))

    def test_unparseable_text_is_not_guessed(self):
        for text in ("", "UNIT-1", "WO-S00429-1-45", "wo-s00429-1-045", "WO-S0042-1-045", None):
            self.assertIsNone(split_serial(text), text)

    def test_normalise_strips_cr_lf_and_whitespace(self):
        self.assertEqual(normalise_scan("  WO-S00429-1-045\r\n"), "WO-S00429-1-045")
        self.assertEqual(normalise_scan("WO-S00429-1-045\n\r"), "WO-S00429-1-045")

    def test_normalise_removes_aim_prefix(self):
        self.assertEqual(normalise_scan("]C1WO-S00429-1-045"), "WO-S00429-1-045")
        self.assertEqual(normalise_scan("]d2WO-S00318-BS1-001\r\n"), "WO-S00318-BS1-001")

    def test_normalise_uppers_only_lower_case_standard_serials(self):
        self.assertEqual(normalise_scan("wo-s00429-1-045"), "WO-S00429-1-045")
        self.assertEqual(normalise_scan("wo-s00220-036"), "WO-S00220-036")
        self.assertEqual(normalise_scan("Wo-S00429-1-045"), "Wo-S00429-1-045")
        self.assertEqual(normalise_scan("unit-7"), "unit-7")
        self.assertEqual(normalise_scan(None), "")


class LookupTests(unittest.TestCase):
    def test_exact_match_after_normalisation(self):
        wl_ = fixture()
        self.assertEqual(wl_.lookup("WO-S00220-1-001").serial, "WO-S00220-1-001")
        self.assertEqual(wl_.lookup("]C1wo-s00220-1-001\r\n").serial, "WO-S00220-1-001")
        self.assertIsNone(wl_.lookup("WO-S00220-1-777"))

    def test_short_form_resolves_when_exactly_one_serial_matches(self):
        self.assertEqual(fixture().lookup("WO-S00220-036").serial, "WO-S00220-1-036")

    def test_short_form_ambiguous_is_not_resolved(self):
        wl_ = fixture()
        self.assertIsNone(wl_.lookup("WO-S00220-011"))
        self.assertTrue(wl_.short_form_ambiguous("WO-S00220-011"))
        self.assertFalse(wl_.short_form_ambiguous("WO-S00220-036"))

    def test_short_form_for_another_work_order_is_never_guessed(self):
        self.assertIsNone(fixture().lookup("WO-S00318-036"))


class ContractAndRoundTripTests(unittest.TestCase):
    def test_contract_fields_are_read(self):
        wl_ = fixture()
        self.assertEqual(wl_.wo_number, "WO-S00220")
        self.assertEqual(wl_.work_order_id, "5c1f0a2e-0000-4000-8000-000000000220")
        self.assertEqual(wl_.as_of, "2026-11-13T09:00:00+05:30")
        self.assertEqual(len(wl_.serials), 7)
        self.assertEqual(wl_.passed_remote(), {"WO-S00220-BS1-004"})

    def test_round_trip_is_lossless_through_json(self):
        original = fixture()
        restored = Worklist.from_dict(json.loads(json.dumps(original.to_dict())))
        self.assertEqual(restored.to_dict(), original.to_dict())
        self.assertEqual(restored.lookup("WO-S00220-036").serial, "WO-S00220-1-036")
        self.assertEqual(classify("WO-S00220-1-002", restored).code, wl.REJECTED)

    def test_bad_contract_is_refused(self):
        for payload in (None, {"wo_number": "WO-S00220"}, {"wo_number": "220", "serials": []},
                        {"wo_number": "WO-S00220", "serials": [{"seq": 1}]},
                        {"wo_number": "WO-S00220", "serials": [entry("WO-S00220-1-001", 1, "1", 1)] * 2}):
            with self.assertRaises(ValueError, msg=repr(payload)):
                Worklist.from_contract(payload)


class ClassifyTests(unittest.TestCase):
    def test_every_code_is_produced_and_none_blocks(self):
        wl_ = fixture()
        cases = {
            wl.OK: classify("WO-S00220-1-001", wl_),
            wl.NOT_ON_LIST: classify("WO-S00220-1-777", wl_),
            wl.OTHER_WORK_ORDER: classify("WO-S00318-1-001", wl_),
            wl.REJECTED: classify("WO-S00220-1-002", wl_),
            wl.REPLACED: classify("WO-S00220-1-003", wl_),
            wl.ALREADY_PASSED_REMOTE: classify("WO-S00220-BS1-004", wl_,
                                               already_passed_remote=wl_.passed_remote()),
            wl.ALREADY_DONE_LOCAL: classify("WO-S00220-1-001", wl_, already_done_local={"WO-S00220-1-001"}),
        }
        self.assertEqual(set(cases), set(wl.CODES))
        for code, result in cases.items():
            self.assertEqual(result.code, code)
            self.assertIs(result.blocking, False, code)
            self.assertTrue(result.message and not result.message.endswith(" "), code)
        self.assertEqual(cases[wl.OK].item.serial, "WO-S00220-1-001")
        self.assertIsNone(cases[wl.OTHER_WORK_ORDER].item)

    def test_passed_remote_only_when_supplied(self):
        self.assertEqual(classify("WO-S00220-BS1-004", fixture()).code, wl.OK)

    def test_short_form_scan_is_classified_against_the_list(self):
        self.assertEqual(classify("WO-S00220-036", fixture()).item.serial, "WO-S00220-1-036")
        result = classify("WO-S00220-011", fixture())
        self.assertEqual(result.code, wl.NOT_ON_LIST)
        self.assertIn("more than one", result.message)

    def test_empty_scan_is_not_on_list(self):
        self.assertEqual(classify("   ", fixture()).code, wl.NOT_ON_LIST)

    def test_blocking_true_is_refused(self):
        with self.assertRaises(ValueError):
            wl.Classification(code=wl.OK, message="x", blocking=True)


class WorkOrderPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.load_fonts()
        cls.app.setFont(theme.ui_font(14))
        cls.app.setStyleSheet(theme.STYLESHEET)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.guards = [patch("leanwatts_can.maynuo.MaynuoLoad.open", side_effect=AssertionError("Hardware forbidden")),
                       patch("leanwatts_can.can_monitor.MonitorClient.open", side_effect=AssertionError("Hardware forbidden"))]
        for guard in self.guards:
            guard.start()
        self.window = CalibrationWindow(Path(self.temp.name))
        self.page = self.window.operator_page
        self.page.timer.stop()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        for guard in self.guards:
            guard.stop()
        self.temp.cleanup()

    def settle(self, timeout=5.0):
        """Wait for the background download to deliver its result on the GUI thread."""
        end = time.monotonic() + timeout
        while self.page._downloading and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertFalse(self.page._downloading, "download did not finish")

    def test_download_button_uses_injected_fetch_and_keeps_list_in_memory(self):
        fake = Mock(return_value=CONTRACT)
        self.page.fetch_worklist = fake
        self.page.work_order.setText(" WO-S00220 ")
        self.page.download_serials.click()
        self.settle()
        fake.assert_called_once_with("WO-S00220")
        self.assertEqual(self.page._worklist.wo_number, "WO-S00220")
        self.assertIn("7 serials", self.page.worklist_status.text())
        self.assertIn("Downloaded 7 serials", self.page.detail.text())
        written = [p for p in Path(self.temp.name).rglob("*") if p.is_file()]
        for path in written:
            self.assertNotIn("WO-S00220-1-001", path.read_text(errors="ignore"), path)

    def slow_fetch(self, payload=CONTRACT, error=None):
        gate, started = threading.Event(), threading.Event()
        calls = []

        def fetch(reference):
            calls.append(reference)
            started.set()
            gate.wait(5)
            if error is not None:
                raise error
            return payload

        return fetch, gate, started, calls

    def test_download_does_not_block_and_applies_the_list_when_done(self):
        fetch, gate, started, calls = self.slow_fetch()
        self.page.fetch_worklist = fetch
        self.page.work_order.setText("WO-S00220")
        began = time.monotonic()
        self.page.download_serials.click()
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertTrue(started.wait(5))
        self.assertFalse(self.page.download_serials.isEnabled())
        self.assertEqual(self.page.download_serials.text(), "Downloading\u2026")
        self.assertIn("Downloading", self.page.detail.text())
        self.assertIsNone(self.page._worklist)
        gate.set()
        self.settle()
        self.assertEqual(self.page._worklist.wo_number, "WO-S00220")
        self.assertEqual(self.page.download_serials.text(), "Download serials")
        self.assertTrue(self.page.download_serials.isEnabled())
        self.assertIn("Downloaded 7 serials", self.page.detail.text())

    def test_download_error_is_shown_and_the_button_recovers(self):
        fetch, gate, started, calls = self.slow_fetch(error=ValueError("Dashboard key rejected"))
        self.page.fetch_worklist = fetch
        self.page.work_order.setText("WO-S00220")
        self.page.download_serials.click()
        gate.set()
        self.settle()
        self.assertEqual(self.page.detail.text(), "Serials could not be downloaded: Dashboard key rejected")
        self.assertIsNone(self.page._worklist)
        self.assertTrue(self.page.download_serials.isEnabled())
        self.assertEqual(self.page.download_serials.text(), "Download serials")

    def test_double_press_is_ignored_while_downloading(self):
        fetch, gate, started, calls = self.slow_fetch()
        self.page.fetch_worklist = fetch
        self.page.work_order.setText("WO-S00220")
        self.page.download_serials.click()
        self.assertTrue(started.wait(5))
        self.page.download_serials.click()
        self.page._download_worklist()
        gate.set()
        self.settle()
        self.assertEqual(calls, ["WO-S00220"])

    def test_result_is_ignored_when_the_work_order_changed_meanwhile(self):
        fetch, gate, started, calls = self.slow_fetch()
        self.page.fetch_worklist = fetch
        self.page.work_order.setText("WO-S00220")
        self.page.download_serials.click()
        self.assertTrue(started.wait(5))
        self.page.work_order.setText("WO-S00999")
        gate.set()
        self.settle()
        self.assertIsNone(self.page._worklist)
        self.assertTrue(self.page.download_serials.isEnabled())
        self.assertEqual(self.page.download_serials.text(), "Download serials")
        self.assertNotIn("Downloaded", self.page.detail.text())

    def test_result_is_ignored_after_the_page_is_closing(self):
        fetch, gate, started, calls = self.slow_fetch()
        self.page.fetch_worklist = fetch
        self.page.work_order.setText("WO-S00220")
        self.page.download_serials.click()
        self.assertTrue(started.wait(5))
        self.page._closing = True
        gate.set()
        self.settle()
        self.page._closing = False
        self.assertIsNone(self.page._worklist)

    def test_download_without_dashboard_says_so(self):
        self.page.fetch_worklist = None
        self.page.work_order.setText("WO-S00220")
        self.page.download_serials.click()
        self.assertEqual(self.page.detail.text(), "Dashboard not configured")
        self.assertIsNone(self.page._worklist)

    def test_download_failure_keeps_no_list(self):
        self.page.fetch_worklist = Mock(side_effect=OSError("network unreachable"))
        self.page.work_order.setText("WO-S00220")
        self.page.download_serials.click()
        self.settle()
        self.assertIn("could not be downloaded", self.page.detail.text())
        self.assertIsNone(self.page._worklist)

    def test_empty_work_order_is_asked_for(self):
        fake = Mock(return_value=CONTRACT)
        self.page.fetch_worklist = fake
        self.page.download_serials.click()
        fake.assert_not_called()
        self.assertIn("Enter the work-order number", self.page.detail.text())

    def test_warning_needs_second_start_press_for_the_same_serial(self):
        self.page._worklist = fixture()
        self.assertFalse(self.page._worklist_gate("WO-S00220-1-002"))
        self.assertEqual(self.page._pending_warning_serial, "WO-S00220-1-002")
        self.assertIn("rejected", self.page.detail.text())
        self.assertIn("Press Start again", self.page.detail.text())
        self.assertTrue(self.page._worklist_gate("WO-S00220-1-002"))
        self.assertIsNone(self.page._pending_warning_serial)

    def test_editing_the_serial_withdraws_the_confirmation(self):
        self.page._worklist = fixture()
        self.assertFalse(self.page._worklist_gate("WO-S00220-1-002"))
        self.page.serial.setText("WO-S00220-1-003")
        self.assertIsNone(self.page._pending_warning_serial)
        self.assertFalse(self.page._worklist_gate("WO-S00220-1-002"))

    def test_ok_serial_passes_and_other_number_skips_the_check(self):
        self.page._worklist = fixture()
        self.assertTrue(self.page._worklist_gate("WO-S00220-1-001"))
        self.page.other_number.setChecked(True)
        self.assertTrue(self.page._worklist_gate("WO-S00220-1-777"))
        self.page.other_number.setChecked(False)

    def test_both_required_pending_unit_skips_the_check(self):
        self.page._worklist = fixture()
        with patch.object(self.page.store, "pending_unit", return_value={"serial": "WO-S00220-1-002"}):
            self.assertTrue(self.page._worklist_gate("WO-S00220-1-002"))

    def test_scan_shows_the_classification_then_starts(self):
        self.page._worklist = fixture()
        self.page.serial.setText("wo-s00220-036")
        with patch.object(self.page, "start") as start:
            self.page._scan_entered()
        start.assert_called_once_with("calibration")
        self.assertEqual(self.page.serial.text(), "WO-S00220-036")
        self.assertIn("WO-S00220-1-036", self.page.detail.text())

    def test_scan_without_list_behaves_as_before(self):
        self.page.serial.setText("  UNIT-1  ")
        with patch.object(self.page, "start") as start:
            self.page._scan_entered()
        start.assert_called_once_with("calibration")
        self.assertEqual(self.page.serial.text(), "  UNIT-1  ")


if __name__ == "__main__":
    unittest.main()
