"""Idle screens do no repeated work: readiness is cached per input set, History reuses one outbox scan."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

from leanwatts_can.outbox import Outbox, report_client_id
from leanwatts_can.session_history import SessionHistoryPage
from leanwatts_can.session_page import SessionPage
from leanwatts_can.session_store import SessionStore
from leanwatts_can.tests.test_session_ui_v8 import profile

from test_session_evidence_equivalence import OPERATOR, cal_result, deterministic, run, test_only_result


class IdleRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.page = SessionPage(self.data, None, SessionStore(self.data))
        self.page.timer.stop()
        self.page.accounts.upsert_operator("OP", "Operator", "password", role="admin")
        operator = self.page.accounts.authenticate("OP", "password")
        self.page._session = self.page.store.start(operator, mode="both_required")
        self.page._authenticated_operator = operator
        self.page.live_run_factory = Mock(return_value=Mock())
        self.page.live_readiness = Mock(return_value="")
        self.page.set_profiles([dict(recipe=profile().to_dict(), revision=1)])
        self.page.serial.setText("WO-S00430-1-001")

    def tearDown(self):
        self.page.dispose()
        self.page.close()
        self.tmp.cleanup()

    def test_idle_refresh_asks_the_store_nothing_after_the_first_pass(self):
        self.page.refresh()
        with patch.object(self.page.store, "readiness", side_effect=AssertionError("recomputed")), \
                patch.object(self.page.store, "process_readiness", side_effect=AssertionError("recomputed")):
            for _ in range(5):
                self.page.refresh()

    def test_inputs_that_change_the_answer_invalidate_the_cache(self):
        self.assertEqual(self.page._readiness("calibration"), "")
        self.assertIn("complete Parameters + Calibration", self.page._readiness("test_only"))
        self.page.serial.setText("bad")
        self.assertNotEqual(self.page._readiness("calibration"), "")
        self.page.serial.setText("WO-S00430-1-001")
        self.assertEqual(self.page._readiness("calibration"), "")
        # A store change (the unit begins) is seen immediately, even for another serial.
        self.page.store.begin_unit("WO-S00430-1-001", profile().to_dict(), 1, run_tests=False, run_calibration=True)
        self.page.serial.setText("WO-S00430-1-002")
        # The unfinished charger is pinned into the serial field, exactly as before.
        self.assertEqual(self.page.serial.text(), "WO-S00430-1-001")
        self.assertEqual(self.page._readiness("calibration"), "")
        # Operator and live-bench checks are never cached.
        self.page.store.finish_unit(self.page.store.active()["units"][0]["attempt_id"],
                                    {"outcome": "FAIL", "calibration_result": "FAIL", "test_result": "NOT RUN",
                                     "detail": "x", "stage_details": []})
        self.page.serial.setText("WO-S00430-1-003")
        self.page.set_mode("simulation")
        self.assertEqual(self.page._readiness("calibration"), "")
        self.page.live_readiness.return_value = "Bench not ready"
        self.page._mode = "live"
        self.assertEqual(self.page._readiness("calibration"), "Bench not ready")
        self.page.live_readiness.return_value = ""
        self.assertEqual(self.page._readiness("calibration"), "")
        self.page._authenticated_operator = None
        self.assertEqual(self.page._readiness("calibration"), "Start or resume an operator session.")


class HistoryStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dashboard_column_uses_one_outbox_scan_and_updates_only_changed_cells(self):
        with tempfile.TemporaryDirectory() as folder, deterministic():
            store = SessionStore(folder)
            store.start(OPERATOR, mode="both_required")
            for number in range(1, 6):
                serial = f"WO-S00430-1-{number:03d}"
                run(store, serial, "cal", cal_result("c%d" % number))
                run(store, serial, "test", test_only_result("t%d" % number))
            session_id = store.active()["session_id"]
            store.close()
            outbox = Outbox(Path(folder) / "outbox")
            items = {}
            for number in (1, 2):
                serial = f"WO-S00430-1-{number:03d}"
                items[serial] = outbox.write_pending({"serial": serial}, "<html/>", report_client_id("ST", session_id, serial, "a"),
                                                     session_id=session_id)
            page = SessionHistoryPage(folder, store, outbox=outbox)
            page.table.selectRow(0)
            self.app.processEvents()
            texts = [page.units.item(row, 7).text() for row in range(page.units.rowCount())]
            self.assertEqual(texts, ["Pending", "Pending", "Not generated", "Not generated", "Not generated"])
            with patch.object(Outbox, "status_for", side_effect=AssertionError("per-row scan")):
                page.table.selectRow(0)
                outbox.mark_sent(items["WO-S00430-1-001"], {"id": "r"})
                changed = []
                original = type(page.units.item(0, 7)).setText
                with patch.object(type(page.units.item(0, 7)), "setText", lambda cell, text: (changed.append(text), original(cell, text))[1]):
                    page.refresh_status()
            self.assertEqual(changed, ["Sent"])
            self.assertEqual(page.units.item(0, 7).text(), "Sent")
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
