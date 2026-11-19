"""Work-order dropdown, list refresh and sign-off on the session page. Offscreen Qt, fakes only."""
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QDialog

from leanwatts_can import theme
from leanwatts_can import work_order_list as wol
from leanwatts_can.gui import CalibrationWindow
from leanwatts_can.session_page import SessionPage, SignOffDialog, WorkOrderCombo
from leanwatts_can.tests.test_worklist import CONTRACT

NOW = datetime.now(timezone.utc)


def wo(number, state="open", untested=50, total=50, customer="RTCXFC INDUSTRIES PRIVATE LIMITED.", signoff=None):
    return {"wo_number": number, "order": number[3:], "customer": customer, "total": total, "tested": total - untested,
            "passed": total - untested, "failed": 0, "untested": untested, "last_report_at": None, "state": state,
            "signoff": signoff}


def signed(days_ago, operator="Asha (1042)"):
    return {"signed_off_at": (NOW - timedelta(days=days_ago)).isoformat(), "operator": operator, "note": None,
            "untested_count": 3, "total_units": 50, "station": None}


LIST = {"generated_at": NOW.isoformat(), "work_orders": [
    wo("WO-S00410"), wo("WO-S00411", untested=0),
    wo("WO-S00400", "signed_off", signoff=signed(2)), wo("WO-S00300", "past", signoff=signed(30))]}


class FakeLogin:
    """Stands in for LoginDialog: accepted with an operator, or cancelled."""
    result = QDialog.DialogCode.Accepted
    operator = {"id": "1042", "name": "Asha", "role": "operator", "enabled": True}
    created = []

    def __init__(self, accounts, parent=None, *, title="", admin_only=False):
        self.admin_only, self.title = admin_only, title
        self.operator = type(self).operator
        type(self).created.append(self)

    def exec(self):
        return type(self).result


class Base(unittest.TestCase):
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
        self.page.accounts.upsert_operator("1042", "Asha", "password", role="admin")
        FakeLogin.result = QDialog.DialogCode.Accepted
        FakeLogin.created = []

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        for guard in self.guards:
            guard.stop()
        self.temp.cleanup()

    def wait_for(self, condition, message, timeout=5.0):
        end = time.monotonic() + timeout
        while not condition() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(condition(), message)

    def load_list(self, payload=LIST):
        self.page.fetch_work_orders = Mock(return_value=payload)
        self.page.refresh_work_orders()
        self.wait_for(lambda: not self.page._wo_refreshing, "list did not load")

    def items(self):
        combo = self.page.work_order
        return [combo.itemText(i) for i in range(combo.count())]


class WiringTests(Base):
    def test_real_window_wires_the_dashboard_callables(self):
        self.assertEqual(self.page.fetch_worklist, self.window.fetch_worklist)
        self.assertEqual(self.page.fetch_work_orders, self.window.fetch_work_orders)
        self.assertEqual(self.page.sign_off_work_order, self.window.sign_off_work_order)

    def test_unconfigured_download_reports_the_real_reason_not_a_missing_wire(self):
        self.page.work_order.setText("WO-S00410")
        self.page.download_serials.click()
        self.wait_for(lambda: not self.page._downloading, "download did not finish")
        self.assertNotEqual(self.page.detail.text(), "Dashboard not configured")
        self.assertIn("Set the dashboard address", self.page.detail.text())


class ComboTests(Base):
    def test_text_api_matches_a_line_edit(self):
        combo = self.page.work_order
        self.assertIsInstance(combo, WorkOrderCombo)
        combo.setText("WO-S00429")
        self.assertEqual(combo.text(), "WO-S00429")
        self.assertEqual(combo.lineEdit().placeholderText(), "WO-S00429")

    def test_entries_groups_and_past_heading(self):
        self.load_list()
        items = self.items()
        self.assertEqual(items[0], "WO-S00410 · RTCXFC INDUSTRIES… · 50 untested")
        self.assertIn("all tested", items[1])
        self.assertIn("days left", items[2])
        self.assertEqual(items[3], wol.PAST_HEADING)
        self.assertEqual(len(items), 5)
        heading = self.page.work_order.model().item(3)
        self.assertFalse(heading.isEnabled())

    def test_no_heading_without_past_rows(self):
        self.load_list({"generated_at": NOW.isoformat(), "work_orders": [wo("WO-S00410")]})
        self.assertEqual(self.items(), ["WO-S00410 · RTCXFC INDUSTRIES… · 50 untested"])

    def test_typed_number_survives_a_refresh_and_picking_sets_the_bare_number(self):
        self.page.work_order.setText("WO-S99999")
        self.load_list()
        self.assertEqual(self.page.work_order.text(), "WO-S99999")
        self.page.work_order.activated.emit(1)
        self.assertEqual(self.page.work_order.text(), "WO-S00411")

    def test_pick_then_download_uses_the_bare_number(self):
        self.load_list()
        fetch = Mock(return_value=CONTRACT)
        self.page.fetch_worklist = fetch
        self.page.work_order.setCurrentIndex(0)
        self.page.work_order.activated.emit(0)
        self.assertEqual(self.page.work_order.text(), "WO-S00410")
        self.page.download_serials.click()
        self.wait_for(lambda: not self.page._downloading, "download did not finish")
        fetch.assert_called_once_with("WO-S00410")

    def test_typing_a_number_not_in_the_list_still_downloads(self):
        self.load_list()
        fetch = Mock(return_value=CONTRACT)
        self.page.fetch_worklist = fetch
        self.page.work_order.setText("WO-S00220")
        self.page.download_serials.click()
        self.wait_for(lambda: not self.page._downloading, "download did not finish")
        fetch.assert_called_once_with("WO-S00220")


class RefreshTests(Base):
    def test_cache_is_written_without_serials_and_shown_offline(self):
        self.load_list()
        cache = Path(self.temp.name) / "work_orders.json"
        self.assertTrue(cache.exists())
        self.assertNotIn("-1-0", cache.read_text(encoding="utf-8"))
        self.assertIn("4 work orders", self.page.work_order_status.text())
        self.page.fetch_work_orders = Mock(side_effect=ValueError("Could not reach the dashboard"))
        self.page.work_order.clear()
        self.page.refresh_work_orders()
        self.wait_for(lambda: not self.page._wo_refreshing, "refresh did not finish")
        self.assertEqual(len(self.items()), 5)
        self.assertIn("old work-order list", self.page.work_order_status.text())
        self.assertNotIn("Traceback", self.page.detail.text())

    def test_not_configured_shows_cache_marked_old_and_no_dialog(self):
        wol.WorkOrderCache(self.temp.name).save(wol.parse_work_orders(LIST)[0])
        self.page.fetch_work_orders = None
        with patch("PySide6.QtWidgets.QMessageBox.exec", side_effect=AssertionError("no dialog")), \
             patch("PySide6.QtWidgets.QMessageBox.warning", side_effect=AssertionError("no dialog")):
            self.page.refresh_work_orders()
        self.assertEqual(len(self.items()), 5)
        self.assertIn("Dashboard not configured", self.page.work_order_status.text())
        self.assertIn("old work-order list", self.page.work_order_status.text())

    def test_malformed_payload_falls_back_to_cache(self):
        wol.WorkOrderCache(self.temp.name).save(wol.parse_work_orders(LIST)[0])
        self.page.fetch_work_orders = Mock(return_value={"nope": 1})
        self.page.refresh_work_orders()
        self.wait_for(lambda: not self.page._wo_refreshing, "refresh did not finish")
        self.assertIn("old work-order list", self.page.work_order_status.text())

    def test_refresh_button_pressing_download_and_first_show_all_refresh(self):
        self.page.fetch_work_orders = Mock(return_value=LIST)
        self.page.refresh_work_orders_button.click()
        self.wait_for(lambda: not self.page._wo_refreshing, "refresh did not finish")
        self.assertEqual(self.page.fetch_work_orders.call_count, 1)
        self.page.fetch_worklist = Mock(return_value=CONTRACT)
        self.page.work_order.setText("WO-S00410")
        self.page.download_serials.click()
        self.wait_for(lambda: not self.page._downloading and not self.page._wo_refreshing, "download did not finish")
        self.assertEqual(self.page.fetch_work_orders.call_count, 2)
        self.page._wo_shown = False
        self.window.show()
        self.wait_for(lambda: not self.page._wo_refreshing, "first show refresh did not finish")
        self.assertEqual(self.page.fetch_work_orders.call_count, 3)
        self.window.hide()
        self.window.show()
        self.app.processEvents()
        self.assertEqual(self.page.fetch_work_orders.call_count, 3)

    def test_list_refresh_uses_its_own_generation_and_signal(self):
        gate = threading.Event()

        def slow():
            gate.wait(5)
            return LIST

        self.page.fetch_work_orders = slow
        before = self.page._download_generation
        self.page.refresh_work_orders()
        self.assertEqual(self.page._download_generation, before)
        self.assertEqual(self.page._wo_generation, 1)
        self.assertFalse(self.page._downloading)
        gate.set()
        self.wait_for(lambda: not self.page._wo_refreshing, "refresh did not finish")

    def test_stale_serial_download_guard_is_unchanged(self):
        gate = threading.Event()

        def slow(reference):
            gate.wait(5)
            return CONTRACT

        self.page.fetch_worklist = slow
        self.page.work_order.setText("WO-S00220")
        self.page.download_serials.click()
        self.page.work_order.setText("WO-S00999")
        gate.set()
        self.wait_for(lambda: not self.page._downloading, "download did not finish")
        self.assertIsNone(self.page._worklist)


class SignOffTests(Base):
    def setUp(self):
        super().setUp()
        self.patches = [patch("leanwatts_can.session_page.LoginDialog", FakeLogin)]
        for item in self.patches:
            item.start()
        self.confirm = patch.object(SignOffDialog, "exec", return_value=QDialog.DialogCode.Accepted)
        self.confirm.start()
        self.answer = {"wo_number": "WO-S00410", "already_signed_off": False,
                       "signoff": {"signed_off_at": NOW.isoformat(), "operator": "Asha (1042)", "note": "ok",
                                   "untested_count": 50, "total_units": 50, "station": "Bench 1"}}
        self.post = Mock(return_value=self.answer)
        self.page.sign_off_work_order = self.post
        self.page.fetch_work_orders = Mock(return_value=LIST)
        self.page.station_name = lambda: "Bench 1"

    def tearDown(self):
        self.confirm.stop()
        for item in self.patches:
            item.stop()
        super().tearDown()

    def finish(self):
        self.wait_for(lambda: not self.page._signing_off and not self.page._wo_refreshing, "sign-off did not finish")

    def test_button_state_follows_text_and_activity(self):
        self.page.work_order.setText("")
        self.assertFalse(self.page.sign_off_button.isEnabled())
        self.page.work_order.setText("not a number!")
        self.assertFalse(self.page.sign_off_button.isEnabled())
        self.page.work_order.setText("WO-S00410")
        self.assertTrue(self.page.sign_off_button.isEnabled())
        self.page._downloading = True
        self.page._lock_controls()
        self.assertFalse(self.page.sign_off_button.isEnabled())
        self.page._downloading = False
        with patch.object(SessionPage, "busy", new=True):
            self.page._lock_controls()
            self.assertFalse(self.page.sign_off_button.isEnabled())
        self.page._lock_controls()
        self.assertTrue(self.page.sign_off_button.isEnabled())

    def test_success_posts_identity_note_station_and_updates_list(self):
        self.load_list()
        with patch.object(SignOffDialog, "note", new="two units returned"):
            self.page.work_order.setText("WO-S00410")
            self.page.sign_off_button.click()
            self.finish()
        self.post.assert_called_once_with("WO-S00410", "Asha (1042)", "two units returned", "Bench 1")
        self.assertFalse(FakeLogin.created[0].admin_only)
        self.assertIn("Signed off WO-S00410", self.page.detail.text())
        self.assertIn("by Asha (1042)", self.page.detail.text())
        self.assertIn("50 untested units were recorded", self.page.detail.text())
        self.assertEqual(self.page.work_order.text(), "WO-S00410")

    def test_local_list_marks_the_order_signed_off(self):
        self.load_list()
        self.page.fetch_work_orders = Mock(side_effect=ValueError("offline"))
        self.page.work_order.setText("WO-S00410")
        self.page.sign_off_button.click()
        self.finish()
        row = self.page._signoff_row("WO-S00410")
        self.assertEqual(row.state, "signed_off")
        self.assertIn("signed off", self.items()[0] + self.items()[1])

    def test_confirm_shows_the_untested_count_from_the_list(self):
        self.load_list()
        seen = []
        original = SignOffDialog.__init__

        def spy(dialog, reference, untested, total, parent=None):
            seen.append((reference, untested, total))
            original(dialog, reference, untested, total, parent)

        with patch.object(SignOffDialog, "__init__", spy):
            self.page.work_order.setText("wo-s00410")
            self.page.sign_off_button.click()
            self.finish()
            self.page.work_order.setText("WO-UNKNOWN")
            self.page.sign_off_button.click()
            self.finish()
        self.assertEqual(seen, [("wo-s00410", 50, 50), ("WO-UNKNOWN", None, None)])

    def test_login_cancelled_posts_nothing(self):
        FakeLogin.result = QDialog.DialogCode.Rejected
        self.page.work_order.setText("WO-S00410")
        self.page.sign_off_button.click()
        self.app.processEvents()
        self.post.assert_not_called()
        self.assertIn("Nothing was recorded", self.page.detail.text())

    def test_confirm_cancelled_posts_nothing(self):
        self.confirm.stop()
        with patch.object(SignOffDialog, "exec", return_value=QDialog.DialogCode.Rejected):
            self.page.work_order.setText("WO-S00410")
            self.page.sign_off_button.click()
        self.confirm.start()
        self.post.assert_not_called()
        self.assertIn("cancelled", self.page.detail.text())

    def test_no_operator_accounts_posts_nothing(self):
        with patch.object(self.page.accounts, "list_operators", return_value=[]):
            self.page.work_order.setText("WO-S00410")
            self.page.sign_off_button.click()
        self.post.assert_not_called()
        self.assertEqual(FakeLogin.created, [])

    def test_server_error_shows_plain_message_and_records_nothing(self):
        self.post.side_effect = ValueError("Could not reach the dashboard over HTTPS.")
        self.page.work_order.setText("WO-S00410")
        self.page.sign_off_button.click()
        self.finish()
        self.assertIn("Could not reach the dashboard", self.page.detail.text())
        self.assertIn("Nothing was recorded", self.page.detail.text())
        self.assertEqual(self.post.call_count, 1)
        self.assertIsNone(self.page._signoff_row("WO-S00410"))
        self.assertTrue(self.page.sign_off_button.isEnabled())

    def test_malformed_answer_is_an_error(self):
        self.post.return_value = {"nonsense": True}
        self.page.work_order.setText("WO-S00410")
        self.page.sign_off_button.click()
        self.finish()
        self.assertIn("Sign-off failed", self.page.detail.text())

    def test_already_signed_off_message(self):
        self.answer["already_signed_off"] = True
        self.answer["signoff"]["signed_off_at"] = "2026-11-10T09:30:00+00:00"
        self.page.work_order.setText("WO-S00410")
        self.page.sign_off_button.click()
        self.finish()
        text = self.page.detail.text()
        self.assertTrue(text.startswith("Already signed off on "), text)
        self.assertIn("by Asha (1042)", text)
        self.assertIn("2026", text)

    def test_nothing_is_queued_offline(self):
        self.post.side_effect = ValueError("offline")
        before = {p for p in Path(self.temp.name).rglob("*") if p.is_file()}
        self.page.work_order.setText("WO-S00410")
        self.page.sign_off_button.click()
        self.finish()
        after = {p for p in Path(self.temp.name).rglob("*") if p.is_file()}
        self.assertEqual(after - before, set())

    def test_unconfigured_dashboard_message(self):
        self.page.sign_off_work_order = None
        self.page.work_order.setText("WO-S00410")
        self.page.sign_off_button.click()
        self.assertEqual(self.page.detail.text(), "Dashboard not configured")
        self.assertEqual(FakeLogin.created, [])

    def test_real_window_sign_off_error_without_dashboard(self):
        self.page.sign_off_work_order = self.window.sign_off_work_order
        self.page.work_order.setText("WO-S00410")
        self.page.sign_off_button.click()
        self.finish()
        self.assertIn("Set the dashboard address", self.page.detail.text())


if __name__ == "__main__":
    unittest.main()
