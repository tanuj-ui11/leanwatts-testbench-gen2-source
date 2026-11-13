"""Persistent two-button session rules and summary evidence; no instrument access."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

from leanwatts_can.session_store import SessionStore


OPERATOR = dict(id="OP", name="Operator", role="operator", enabled=True)
RECIPE = dict(name="57.3 V / 20 A", cc_current=20, end_voltage=57.3)
SERIAL = "WO-S00430-1-001"


def calibrated(**changes):
    result = dict(outcome="CALIBRATED", calibration_result="PASS", test_result="NOT RUN",
                  simulation=False, test=None, stage_details=[
                      dict(name="Current calibration", before=23.0, after=20.01, unit="A", status="PASS"),
                      dict(name="Voltage calibration", before=56, after=57.3, unit="V", status="PASS")])
    result.update(changes)
    return result


def tested(**changes):
    result = dict(outcome="PASS", calibration_result="NOT RUN", test_result="PASS",
                  simulation=False, calibration=None, stage_details=[
                      dict(name="Full-current test", after=20.0, unit="A", status="PASS", duration_seconds=20)],
                  test=dict(results=[dict(name="Full-current test", measured=20.0, unit="A", status="PASS", duration_seconds=20)]))
    result.update(changes)
    return result


class SessionPairingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(self.temp.name)
        self.store.start(OPERATOR, mode="both_required")

    def cal(self, result=None, serial=SERIAL, recipe=None, revision=1):
        attempt = self.store.begin_unit(serial, recipe or RECIPE, revision, run_tests=False)
        self.store.finish_unit(attempt, calibrated() if result is None else result)
        return attempt

    def record_test(self, result=None, serial=SERIAL):
        attempt = self.store.begin_unit(serial, RECIPE, 1, run_calibration=False)
        self.store.finish_unit(attempt, tested() if result is None else result)
        return attempt

    def test_both_required_blocks_test_first_without_creating_attempt(self):
        with self.assertRaisesRegex(ValueError, "before Test"):
            self.store.begin_unit(SERIAL, RECIPE, 1, run_calibration=False)
        self.assertEqual(self.store.active()["units"], [])

    def test_pair_freezes_serial_recipe_and_revision_and_releases_after_success(self):
        self.cal()
        for serial, recipe, revision in [("WO-S00430-1-002", RECIPE, 1),
            (SERIAL, dict(RECIPE, cc_current=21), 1), (SERIAL, RECIPE, 2)]:
            with self.subTest(serial=serial, recipe=recipe, revision=revision):
                with self.assertRaisesRegex(ValueError, "frozen variant"):
                    self.store.begin_unit(serial, recipe, revision, run_tests=False)
        self.assertEqual(self.store.pending_unit()["next_step"], "Test")
        self.record_test()
        self.assertIsNone(self.store.pending_unit())
        self.cal(serial="WO-S00430-1-002")
        self.assertEqual(self.store.pending_unit()["serial"], "WO-S00430-1-002")

    def test_pair_has_one_summary_row_and_two_audited_attempts(self):
        cal_id, test_id = self.cal(), self.record_test()
        session = self.store.active()
        self.assertEqual(len(session["units"]), 2)
        row, = session["unit_summaries"]
        self.assertEqual(row["attempt_ids"], [cal_id, test_id])
        self.assertEqual((row["calibration_result"], row["test_result"], row["outcome"]), ("PASS", "PASS", "PASS"))
        with Path(session["report_paths"]["csv"]).open(encoding="utf-8-sig", newline="") as handle:
            csv_rows = list(csv.reader(handle))
        self.assertEqual(len(csv_rows), 2)
        html = Path(session["report_paths"]["html"]).read_text(encoding="utf-8")
        self.assertIn("23 A → 20.01 A", html)
        self.assertIn("PASS · 20 s", html)
        self.assertIn("2 attempts", html)
        detail = Path(session["report_paths"]["detail_html"]).read_text(encoding="utf-8")
        self.assertEqual(detail.count("<section>"), 2)

    def test_failed_calibration_or_stop_holds_unit_and_allows_retry(self):
        for outcome in ("FAIL", "STOPPED", "INTERRUPTED"):
            with self.subTest(outcome=outcome):
                self.cal(calibrated(outcome=outcome, calibration_result="FAIL"))
                self.assertEqual(self.store.pending_unit()["next_step"], "Parameters + Calibration")
                with self.assertRaisesRegex(ValueError, "before Test"):
                    self.record_test()
        self.cal()
        self.record_test()
        self.assertIsNone(self.store.pending_unit())

    def test_native_stage_names_merge_once_with_correct_test_time(self):
        self.cal()
        actual = [dict(name="Full current", measured=20, after=20, status="PASS", duration_seconds=21, unit="A"),
                  dict(name="End voltage at CC load", measured=57.3, after=57.3, status="PASS", duration_seconds=5, unit="V")]
        self.record_test(tested(stage_details=actual, test=dict(results=actual)))
        session = self.store.active()
        merged, = self.store.summarize(session, evidence=True)
        self.assertEqual([item["name"] for item in merged["stage_details"]],
                         ["Current calibration", "Voltage calibration", "Full current", "End voltage at CC load"])
        html = Path(session["report_paths"]["html"]).read_text(encoding="utf-8")
        self.assertEqual(html.count("<th>Full-current test</th>"), 1)
        self.assertEqual(html.count("<th>End-voltage test</th>"), 1)
        self.assertNotIn("<th>End voltage at CC load</th>", html)
        self.assertIn("PASS · 21 s", html)
        self.assertIn("PASS · 5 s", html)

    def test_ocv_is_first_test_and_does_not_inherit_legacy_loaded_voltage_pass(self):
        self.cal()
        stages = [dict(name="Open-circuit voltage", measured=57.3, after=57.3, status="PASS", duration_seconds=5, unit="V"),
                  dict(name="Full current", measured=20, after=20, status="PASS", duration_seconds=21, unit="A"),
                  dict(name="End current / observed shutdown", measured=1.1, after=1.1, status="PASS", duration_seconds=8, unit="A")]
        self.record_test(tested(stage_details=stages, test=dict(results=stages)))
        session = self.store.active()
        with Path(session["report_paths"]["csv"]).open(encoding="utf-8-sig", newline="") as handle:
            headers, row = list(csv.reader(handle))
        start = headers.index("Open-circuit voltage test")
        self.assertEqual(headers[start:start + 3], ["Open-circuit voltage test", "Full-current test", "End-current cutoff test"])
        self.assertNotIn("End-voltage test", headers)
        self.assertIn("PASS · 5 s", row[start])
        self.record_test(tested(stage_details=[dict(name="End voltage at CC load", after=57.3, status="PASS")],
                               test=dict(results=[dict(name="End voltage at CC load", measured=57.3, status="PASS")])))
        with Path(session["report_paths"]["csv"]).open(encoding="utf-8-sig", newline="") as handle:
            headers, row = list(csv.reader(handle))
        self.assertIn("NOT RUN", row[headers.index("Open-circuit voltage test")])
        self.assertIn("PASS", row[headers.index("End-voltage test")])

    def test_single_process_modes_are_enforced_by_store_and_survive_reload(self):
        for mode, forbidden_cal, forbidden_test in (("calibration_only", False, True), ("test_only", True, False)):
            with self.subTest(mode=mode):
                self.store.close()
                self.store.start(OPERATOR, mode=mode)
                self.store = SessionStore(self.temp.name)
                self.assertEqual(self.store.active()["session_mode"], mode)
                with self.assertRaisesRegex(ValueError, "This session is"):
                    self.store.begin_unit(SERIAL, RECIPE, 1, run_calibration=forbidden_cal, run_tests=forbidden_test)
                with self.assertRaisesRegex(ValueError, "This session is"):
                    self.store.begin_unit(SERIAL, RECIPE, 1, run_calibration=True, run_tests=True)
                self.assertEqual(self.store.active()["units"], [])
                if mode == "calibration_only":
                    self.cal()
                    self.assertEqual(self.store.active()["unit_summaries"][0]["test_result"], "NOT RUN")
                else:
                    self.record_test()
                    self.assertEqual(self.store.active()["unit_summaries"][0]["calibration_result"], "NOT RUN")

    def test_failed_test_and_stop_do_not_release_lock(self):
        self.cal()
        for outcome in ("FAIL", "STOPPED"):
            self.record_test(tested(outcome=outcome, test_result="FAIL"))
            self.assertEqual(self.store.pending_unit()["next_step"], "Test")
            with self.assertRaises(ValueError):
                self.cal(serial="WO-S00430-1-002")
        self.record_test()
        self.assertIsNone(self.store.pending_unit())

    def test_recalibration_invalidates_old_test_even_when_new_calibration_fails(self):
        self.cal()
        self.record_test()
        attempt = self.store.begin_unit(SERIAL, RECIPE, 1, run_tests=False)
        self.assertEqual(self.store.pending_unit()["test_result"], "INVALIDATED")
        self.store.finish_unit(attempt, calibrated(outcome="FAIL", calibration_result="FAIL"))
        self.assertEqual(self.store.pending_unit()["next_step"], "Parameters + Calibration")
        self.cal()
        self.assertEqual(self.store.pending_unit()["next_step"], "Test")
        self.assertEqual(self.store.pending_unit()["test_result"], "INVALIDATED")
        self.record_test()
        self.assertIsNone(self.store.pending_unit())

    def test_restart_preserves_mode_lock_and_frozen_snapshot(self):
        self.cal()
        self.store = SessionStore(self.temp.name)
        self.assertEqual(self.store.active()["session_mode"], "both_required")
        self.assertEqual(self.store.pending_unit()["recipe"], RECIPE)
        with self.assertRaises(ValueError):
            self.cal(serial="WO-S00430-1-002")
        self.record_test()
        self.assertIsNone(self.store.pending_unit())

    def test_interrupted_calibration_is_not_an_unlock_or_test_qualification(self):
        self.store.begin_unit(SERIAL, RECIPE, 1, run_tests=False)
        self.store = SessionStore(self.temp.name)
        self.assertEqual(self.store.active()["units"][0]["outcome"], "INTERRUPTED")
        self.assertEqual(self.store.pending_unit()["next_step"], "Parameters + Calibration")
        self.cal()

    def test_close_incomplete_is_allowed_and_report_never_passes_skipped_tests(self):
        self.cal()
        closed = self.store.close()
        self.assertEqual(closed["completion_state"], "INCOMPLETE")
        self.assertEqual(closed["unit_summaries"][0]["outcome"], "INCOMPLETE")
        self.assertEqual(closed["unit_summaries"][0]["test_result"], "NOT RUN")
        self.assertIsNone(self.store.active())
        self.assertIn("INCOMPLETE", Path(closed["report_paths"]["html"]).read_text())

    def test_simulation_cannot_satisfy_both_required(self):
        self.cal(calibrated(outcome="SIMULATION", simulation=True))
        with self.assertRaises(ValueError):
            self.record_test()
        self.assertIsNotNone(self.store.pending_unit())

    def test_independent_allows_test_before_calibration_and_next_serial(self):
        self.store.close()
        self.store.start(OPERATOR)
        self.record_test()
        row = self.store.active()["unit_summaries"][0]
        self.assertEqual(row["calibration_result"], "NOT RUN")
        self.assertEqual(row["outcome"], "PASS")
        self.cal()
        row = self.store.active()["unit_summaries"][0]
        self.assertEqual(row["test_result"], "INVALIDATED")
        self.assertEqual(row["outcome"], "CALIBRATED")
        self.cal(serial="WO-S00430-1-002")
        self.assertIsNone(self.store.pending_unit())

    def test_legacy_session_without_mode_defaults_independent(self):
        self.store.close()
        self.store.start(OPERATOR)
        session = self.store.active()
        path = Path(session["report_paths"]["json"])
        session.pop("session_mode")
        path.write_text(json.dumps(session), encoding="utf-8")
        self.store = SessionStore(self.temp.name)
        self.assertEqual(self.store.active()["session_mode"], "independent")
        self.record_test()

    def test_mode_is_validated_and_returned_session_copy_cannot_change_it(self):
        self.store.active()["session_mode"] = "independent"
        self.assertEqual(self.store.active()["session_mode"], "both_required")
        self.store.close()
        with self.assertRaises(ValueError):
            self.store.start(OPERATOR, mode="something else")


if __name__ == "__main__":
    unittest.main()
