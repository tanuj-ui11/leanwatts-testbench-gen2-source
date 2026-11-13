"""Readable report accuracy and selected-workflow result guards, no hardware."""
import csv
import io
import tempfile
import unittest
from pathlib import Path

from leanwatts_can.session_history import export_html_reports
from leanwatts_can.session_store import SessionStore
from leanwatts_can.tests.test_production_sessions import production_recipe


def completed_result():
    stages = [dict(name="Current calibration", before=23, after=15.02, unit="A", duration_seconds=21, status="PASS"),
              dict(name="Voltage calibration", before=55.2, after=58.39, unit="V", duration_seconds=9, status="PASS"),
              dict(name="CC current", after=15.01, unit="A", duration_seconds=4, status="PASS"),
              dict(name="Normal output / end voltage", after=58.4, unit="V", duration_seconds=5, status="PASS"),
              dict(name="End current / observed shutdown", after=1.1, unit="A", duration_seconds=18, status="PASS")]
    return dict(outcome="PASS", calibration_result="PASS", test_result="PASS", simulation=False,
        run_calibration=True, run_tests=True, workflow_mode="parameters_calibration_test",
        baseline_result=dict(status="PASS",
            current=dict(status="PASS", measured=16.2, load_setpoint=49, duration_seconds=2),
            open_voltage=dict(status="PASS", measured=59.1, duration_seconds=2)),
        preflight_result=dict(status="PASS", before=dict(ccCurrent=14, endVoltage=58.4),
            after=dict(ccCurrent=15, endVoltage=58.4), desired=dict(ccCurrent=15, endVoltage=58.4),
            updated_fields=["ccCurrent"]),
        starting_point=dict(method="session_reference", source_serial="FIRST", current_initial=71.5,
            voltage_initial=83.1, clamped=[]),
        stage_details=stages, calibration=dict(results=[dict(name="Current calibration", measured=15.02,
            unit="A", status="SAVE REQUEST ACCEPTED", duration_seconds=21),
            dict(name="Voltage calibration", measured=58.39, unit="V", status="SAVE REQUEST ACCEPTED", duration_seconds=9)]),
        test=dict(results=[dict(name=x["name"], measured=x["after"], unit=x["unit"],
            duration_seconds=x["duration_seconds"], status=x["status"]) for x in stages[2:]]))


class SimpleSessionReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SessionStore(self.tmp.name)
        self.session = self.store.start(dict(id="OP", name="Operator", role="operator", enabled=True))
        self.recipe = production_recipe()

    def content(self, key="html"):
        return Path(self.store.report_paths(self.session["session_id"])[key]).read_text(encoding="utf-8-sig")

    def record(self, result=None, **options):
        attempt = self.store.begin_unit("WO-S24250-001-001", self.recipe, 1, **options)
        self.store.finish_unit(attempt, completed_result() if result is None else result)
        return attempt

    def test_one_summary_row_has_real_baselines_changes_and_each_test(self):
        self.record()
        page = self.content()
        self.assertEqual(page.count("<table>"), 1)
        self.assertEqual(page.count("<tr>"), 2)
        self.assertIn("CC current (A): 14 → 15", page)
        self.assertNotIn("End voltage (V):", page)
        self.assertIn("16.2 A → 15.02 A", page)
        self.assertIn("59.1 V → 58.39 V", page)
        self.assertIn("CV 49 V → CV 48 V", page)
        self.assertIn("load OFF → CC 1 A", page)
        self.assertIn("PASS · 4 s", page)
        self.assertIn("PASS · 5 s", page)
        self.assertIn("PASS · 18 s", page)
        self.assertIn("Session reference FIRST +2", page)
        self.assertIn("Open detailed report", page)
        self.assertIn("<h3>Charger parameter readback</h3>", self.content("detail_html"))
        rows = list(csv.reader(io.StringIO(self.content("csv"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]), len(rows[1]))
        self.assertIn("Starting trims", rows[0])
        self.assertNotIn("Stage details", rows[0])

    def test_unconfirmed_parameter_write_is_not_rendered_as_a_verified_change(self):
        result = completed_result()
        result["outcome"] = "FAIL"
        result["preflight_result"] = dict(status="FAIL", before={"ccCurrent": 14},
            desired={"ccCurrent": 15}, after={}, updated_fields=["ccCurrent"])
        self.record(result)
        self.assertIn("14 → Unconfirmed (requested 15)", self.content())
        self.assertNotIn("14 → 15", self.content())

    def test_legacy_trimmed_before_value_is_explicit_not_claimed_as_baseline(self):
        result = completed_result()
        result.pop("baseline_result")
        self.record(result)
        self.assertIn("23 A → 15.02 A", self.content())
        self.assertIn("first trimmed reading", self.content())

    def test_failed_baseline_is_not_zero_or_legacy_fallback(self):
        result = completed_result()
        result["outcome"] = "FAIL"
        result["baseline_result"] = {"status": "FAIL", "current": {"status": "FAIL", "measured": 0}}
        self.record(result)
        self.assertIn("Unavailable → 15.02 A", self.content())
        self.assertNotIn("0 A → 15.02 A", self.content())

    def test_test_only_pass_is_allowed_without_calibration_claim(self):
        result = dict(outcome="PASS", calibration_result="NOT RUN", test_result="PASS",
                      simulation=False, run_calibration=False, run_tests=True,
                      workflow_mode="test_only", calibration=None)
        self.record(result, run_calibration=False)
        self.assertIn("NOT RUN", self.content())
        self.assertEqual(self.store.active()["units"][0]["workflow_mode"], "test_only")

    def test_full_pass_still_requires_calibration_and_tests_and_simulation_is_not_pass(self):
        for options, result in [({}, dict(outcome="PASS", calibration_result="NOT RUN", test_result="PASS")),
            ({"run_calibration": False}, dict(outcome="PASS", calibration_result="PASS", test_result="PASS")),
            ({"run_tests": False}, dict(outcome="PASS", calibration_result="PASS", test_result="NOT RUN")),
            ({"run_calibration": False}, dict(outcome="PASS", calibration_result="NOT RUN", test_result="PASS", simulation=True))]:
            with self.subTest(options=options, result=result):
                attempt = self.store.begin_unit("WO-S24250-001-002", self.recipe, 1, **options)
                with self.assertRaises(ValueError):
                    self.store.finish_unit(attempt, result)
                self.store.finish_unit(attempt, dict(outcome="FAIL", calibration_result="NOT RUN", test_result="NOT RUN"))

    def test_workflow_choice_cannot_change_and_empty_choice_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.begin_unit("WO-S24250-001-003", self.recipe, 1, run_tests=False, run_calibration=False)
        attempt = self.store.begin_unit("WO-S24250-001-003", self.recipe, 1, run_calibration=False)
        with self.assertRaises(ValueError):
            self.store.finish_unit(attempt, dict(outcome="FAIL", run_calibration=True))

    def test_calibration_only_remains_calibrated_not_full_pass(self):
        self.record(dict(outcome="CALIBRATED", calibration_result="PASS", test_result="NOT RUN",
                         simulation=False, run_calibration=True, run_tests=False, test=None), run_tests=False)
        self.assertIn("CALIBRATED", self.content())
        self.assertIn("NOT RUN", self.content())

    def test_html_and_csv_escape_operator_entered_text(self):
        attempt = self.store.begin_unit('=<script>alert(1)</script>', self.recipe, 1, serial_override=True)
        self.store.finish_unit(attempt, completed_result())
        self.assertNotIn("<script>", self.content())
        self.assertIn("&lt;script&gt;", self.content())
        self.assertIn("CUSTOM NUMBER", self.content())
        self.assertIn("custom number", self.content("detail_html"))
        csv_rows = list(csv.reader(io.StringIO(self.content("csv"))))
        self.assertTrue(csv_rows[1][5].startswith("'="))

    def test_export_preserves_relative_detail_and_summary_links_after_rename(self):
        self.record()
        paths = self.store.report_paths(self.session["session_id"])
        output = Path(self.tmp.name) / "renamed & shared.html"
        export_html_reports(paths, output)
        detail = output.with_name(output.stem + ".detail.html")
        self.assertTrue(detail.is_file())
        self.assertIn('href="renamed%20%26%20shared.detail.html"', output.read_text(encoding="utf-8"))
        self.assertIn('href="renamed%20%26%20shared.html"', detail.read_text(encoding="utf-8"))
        self.assertIn(Path(paths["detail_html"]).name, self.content())

    def test_summary_and_csv_explain_ocv_failure_with_required_and_observed_values(self):
        item = dict(name="Open-circuit voltage", target=58.4, tolerance=.1, measured=57.8,
                    unit="V", status="FAIL", duration_seconds=4,
                    detail="Settled open voltage is below the required range.")
        self.record(dict(outcome="FAIL", calibration_result="NOT RUN", test_result="FAIL", simulation=False,
            calibration=None, detail="Some required checks failed.", test=dict(outcome="FAIL", results=[item]),
            stage_details=[dict(item, after=57.8)]), run_calibration=False)
        headers, row = list(csv.reader(io.StringIO(self.content("csv"))))
        reason = row[headers.index("Failure reason")]
        self.assertIn("Open-circuit voltage test", reason)
        self.assertIn("Required 58.4 ±0.1 V", reason)
        self.assertIn("Observed 57.8 V", reason)
        self.assertIn("below the required range", row[headers.index("Open-circuit voltage test")])
        self.assertIn("Failure reason:", self.content("detail_html"))
        self.assertIn("below the required range", self.store.active()["units"][0]["failure_reason"])

    def test_timeout_before_result_uses_failed_stage_and_engine_reason(self):
        stage = dict(name="Full current", status="ERROR", after=None, unit="A", duration_seconds=60)
        self.record(dict(outcome="FAIL", calibration_result="NOT RUN", test_result="FAIL", simulation=False,
            calibration=None, detail="Process could not finish.",
            test=dict(outcome="ERROR", results=[], stage_details=[stage],
                      detail="Current did not reach a stable plateau within 60 seconds."),
            stage_details=[stage]), run_calibration=False)
        reason = self.store.active()["unit_summaries"][0]["failure_reason"]
        self.assertIn("Full-current test", reason)
        self.assertIn("within 60 seconds", reason)
        self.assertIn("Required 15 ±0.1 A", reason)
        self.assertNotIn("Observed 0", reason)
        headers, row = list(csv.reader(io.StringIO(self.content("csv"))))
        self.assertIn("within 60 seconds", row[headers.index("Full-current test")])

    def test_cutoff_restart_and_interval_are_in_per_test_and_overall_reason(self):
        item = dict(name="End current / observed shutdown", target=1.1, tolerance=.1, measured=1.35,
            observed_interval_a=[1.3, 1.4], unit="A", status="FAIL", duration_seconds=35,
            note="Output restarted after the first drop. Relay chatter is not a verified cutoff.")
        self.record(dict(outcome="FAIL", calibration_result="NOT RUN", test_result="FAIL", simulation=False,
            calibration=None, test=dict(outcome="FAIL", results=[item]), stage_details=[]), run_calibration=False)
        headers, row = list(csv.reader(io.StringIO(self.content("csv"))))
        for column in ("Failure reason", "End-current cutoff test"):
            self.assertIn("Relay chatter", row[headers.index(column)])
            self.assertIn("Observed cutoff interval 1.3–1.4 A", row[headers.index(column)])
            self.assertIn("Required 1.1 ±0.1 A", row[headers.index(column)])

    def test_pass_has_no_stale_failure_reason(self):
        result = completed_result()
        result["failure_reason"] = "Old failed attempt"
        self.record(result)
        headers, row = list(csv.reader(io.StringIO(self.content("csv"))))
        self.assertEqual(row[headers.index("Failure reason")], "—")
        self.assertEqual(self.store.active()["units"][0]["failure_reason"], "")
        self.assertNotIn("Old failed attempt", self.content())

    def test_older_failure_without_details_remains_explicit_and_nonblank(self):
        self.record(dict(outcome="FAIL", calibration_result="FAIL", test_result="NOT RUN"))
        self.assertIn("Failure reason was not recorded in this older result", self.content())
        self.assertTrue(self.store.active()["unit_summaries"][0]["failure_reason"])

    def test_failure_text_is_escaped_in_html_and_spreadsheet_export(self):
        self.record(dict(outcome="FAIL", calibration_result="FAIL", test_result="NOT RUN",
                         detail='=<script>alert(1)</script>'))
        headers, row = list(csv.reader(io.StringIO(self.content("csv"))))
        self.assertTrue(row[headers.index("Failure reason")].startswith("'="))
        self.assertNotIn("<script>", self.content())
        self.assertIn("&lt;script&gt;", self.content())


if __name__ == "__main__":
    unittest.main()
