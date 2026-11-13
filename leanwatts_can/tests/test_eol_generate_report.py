"""Generate report button in History & Reports: real handler, temp data dir, fake client. No network, no hardware."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from leanwatts_can import theme
from leanwatts_can.outbox import Outbox, report_client_id
from leanwatts_can.outbox_service import OutboxService
from leanwatts_can.session_history import SessionHistoryPage
from leanwatts_can.session_store import SessionStore

from test_eol_report import SERIAL, pass_attempt, session

SESSION_ID = "20261113-153700-abcd1234"
STATION = "LW-EOL-01"
CONTRACT_KEYS = {"serial", "client_report_id", "result", "tested_at", "generated_at", "station", "operator",
                 "tool_version", "firmware_version", "summary", "report", "report_html"}


class FakeClient:
    def __init__(self):
        self.calls = []

    def post_report(self, payload, client_report_id):
        self.calls.append((dict(payload), client_report_id))
        return {"id": "srv-1", "serial": payload["serial"], "duplicate": False, "replaced": False}


class GenerateReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.load_fonts()
        cls.app.setFont(theme.ui_font(14))
        cls.app.setStyleSheet(theme.STYLESHEET)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.outbox = Outbox(self.data / "outbox")
        self.station = STATION
        self.page = None

    def tearDown(self):
        if self.page is not None:
            self.page.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def load(self, units):
        record = session(units, session_id=SESSION_ID, status="CLOSED")
        folder = self.data / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (SESSION_ID + ".json")).write_text(json.dumps(record), encoding="utf-8")
        store = SessionStore(self.data)
        self.page = SessionHistoryPage(self.data, store, outbox=self.outbox, station_name=lambda: self.station)
        self.assertEqual(self.page.table.rowCount(), 1)
        self.page.table.selectRow(0)
        self.app.processEvents()
        return self.page

    def press(self, serial):
        rows = {self.page.units.item(row, 1).text(): row for row in range(self.page.units.rowCount())}
        self.assertIn(serial, rows)
        self.page.units.selectRow(rows[serial])
        self.app.processEvents()
        self.assertTrue(self.page.generate_button.isEnabled())
        self.page.generate_button.click()
        return self.page.status.text()

    def pending_files(self):
        folder = self.data / "outbox" / "pending"
        return sorted(path.name for path in folder.glob("*.json") if not path.name.endswith(".state.json"))

    def test_history_shows_the_serial_and_generate_queues_one_pending_item(self):
        self.load([pass_attempt()])
        self.assertEqual(self.page.units.item(0, 1).text(), SERIAL)
        self.assertEqual(self.page.units.item(0, 7).text(), "Not generated")
        message = self.press(SERIAL)
        self.assertIn("Report queued", message)
        files = self.pending_files()
        self.assertEqual(len(files), 1)
        payload = json.loads((self.data / "outbox" / "pending" / files[0]).read_text(encoding="utf-8"))
        self.assertEqual(payload["body"]["serial"], SERIAL)
        self.assertEqual(payload["body"]["result"], "pass")
        self.assertTrue(payload["html"].startswith("<!doctype html>"))
        self.assertIn(SERIAL, payload["html"])
        self.assertEqual(self.page.units.item(0, 7).text(), "Pending")
        self.assertEqual(len(list((self.data / "outbox" / "sent").glob("*.json"))), 0)

    def test_client_report_id_is_stable_across_two_presses(self):
        self.load([pass_attempt()])
        self.press(SERIAL)
        first = self.pending_files()
        self.press(SERIAL)
        self.assertEqual(self.pending_files(), first)
        self.assertEqual(first, [report_client_id(STATION, SESSION_ID, SERIAL, "a1") + ".json"])

    def test_nothing_is_sent_by_pressing_and_the_wire_body_matches_the_contract(self):
        self.load([pass_attempt()])
        client = FakeClient()
        service = OutboxService(self.outbox, lambda: client)
        self.press(SERIAL)
        self.assertEqual(client.calls, [])
        self.assertEqual(service.tick(self.outbox._clock() + 1)["sent"], 1)  # the queue sends only when its worker runs
        (payload, client_id), = client.calls
        wire = dict(payload, client_report_id=client_id)  # what DashboardClient.post_report puts on the wire
        self.assertEqual(set(wire), CONTRACT_KEYS)
        self.assertEqual(wire["serial"], SERIAL)
        self.assertEqual(wire["result"], "pass")
        self.assertEqual(wire["station"], STATION)
        self.assertEqual(wire["operator"], "R. Kumar (OP07)")
        self.assertEqual(wire["tool_version"], "1.1.13")
        self.assertIsNone(wire["firmware_version"])
        self.assertEqual(wire["summary"], {"stages": 8, "failed": 0})
        self.assertEqual(wire["report"]["schema"], "leanwatts-eol-report/1")
        self.assertIsInstance(wire["report_html"], str)
        datetime.fromisoformat(wire["tested_at"])
        self.assertIsNotNone(datetime.fromisoformat(wire["generated_at"]).tzinfo)
        self.assertLessEqual(len(client_id), 80)

    def test_simulation_and_other_number_units_are_refused(self):
        simulated = pass_attempt("s1", 2, simulation=True, serial="WO-S00430-1-013")
        other = pass_attempt("o1", 3, serial_override=True, serial="BENCH-UNIT-7")
        self.load([pass_attempt(), simulated, other])
        for serial in ("WO-S00430-1-013", "BENCH-UNIT-7"):
            message = self.press(serial)
            self.assertEqual(message, "Report not generated: Simulation runs and Other-number serials cannot be sent to the dashboard.")
        self.assertEqual(self.pending_files(), [])

    def test_running_unit_and_missing_station_are_refused(self):
        running = dict(pass_attempt("r1", 2, serial="WO-S00430-1-014"), outcome="RUNNING", finished_at=None)
        self.load([pass_attempt(), running])
        self.assertIn("Wait for this charger to finish", self.press("WO-S00430-1-014"))
        self.station = "  "
        self.assertIn("Enter the station name in Settings > Dashboard", self.press(SERIAL))
        self.assertEqual(self.pending_files(), [])

    def test_button_is_disabled_without_an_outbox(self):
        self.load([pass_attempt()])
        self.page.outbox = None
        self.page._sync_actions()
        self.assertFalse(self.page.generate_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
