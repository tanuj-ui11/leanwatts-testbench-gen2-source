"""EoL report builder: verdict, failure, rounding, time zones, size cap, HTML escaping. No hardware."""
import copy
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

from leanwatts_can import eol_report
from leanwatts_can.eol_report import build_report, ensure_serial_ok, render_html
from leanwatts_can.production_flow import ProductionFlow


SERIAL = "WO-S00430-1-012"
NOW = datetime(2026, 11, 13, 10, 20, tzinfo=timezone.utc)
OPERATOR = {"id": "OP07", "name": "R. Kumar", "role": "operator", "enabled": True}
RECIPE = {"name": "Gen2 Ruchira V1", "cc_current": 20.0, "current_tolerance": 0.1,
          "end_voltage": 56.0, "voltage_tolerance": 0.1, "end_current": 1.5, "end_current_tolerance": 0.1,
          "current_cal_load_voltage": 48.0, "voltage_cal_load_current": 1.0}
LOAD_IDENTITY = {"model_code": 0x0042, "firmware_code": 0x002A, "port": "COM11"}
CAN_ADAPTER = {"index": 0, "bitrate": 250000}


def stage(name, status, before=None, after=None, duration=None, detail=""):
    return {"name": name, "status": status, "before": before, "after": after,
            "unit": "", "duration_seconds": duration, "detail": detail}


def attempt(attempt_id, sequence, *, outcome, calibration_result, test_result, stages,
            cal=None, test=None, run_calibration=True, run_tests=True, revision=3,
            started="2026-11-13T15:37:00+05:30", finished="2026-11-13T15:41:00+05:30", **extra):
    unit = dict(attempt_id=attempt_id, sequence=sequence, serial=SERIAL, serial_override=False,
                recipe=copy.deepcopy(RECIPE), recipe_name="Gen2 Ruchira V1", recipe_revision=revision,
                started_at=started, finished_at=finished, elapsed_seconds=232.0,
                outcome=outcome, calibration_result=calibration_result, test_result=test_result,
                run_calibration=run_calibration, run_tests=run_tests, simulation=False,
                diagnostic_only=False, production_ready=True, operator=dict(OPERATOR),
                station="LW-EOL-01", application_version="1.1.13", detail="",
                load_identity=dict(LOAD_IDENTITY), can_adapter=dict(CAN_ADAPTER),
                stage_details=stages, calibration=cal, test=test, preflight_result=None)
    unit.update(extra)
    return unit


def session(units, **top):
    data = dict(schema="leanwatts-session/1", session_id="20261113-153700-abcd1234",
                application_version="1.1.13", operator=dict(OPERATOR), session_mode="independent",
                status="OPEN", units=units)
    data.update(top)
    return data


def cal_record(current=None, voltage=None):
    results = [{"name": "Current calibration", "status": "SAVE REQUEST ACCEPTED", "measured": current, "duration_seconds": 31},
               {"name": "Voltage calibration", "status": "SAVE REQUEST ACCEPTED", "measured": voltage, "duration_seconds": 24}]
    return {"results": results, "current_duty": 74.5, "voltage_duty": 82.1,
            "load_probe": {"model_code": 0x0042, "firmware_code": 0x002A, "port": "COM11"}}


PASS_STAGES = [
    stage("Baseline open voltage", "DONE", after=56.235, duration=2.5),
    stage("Baseline current", "DONE", after=18.51, duration=11.9),
    stage("Charger parameters", "PASS", before="Readback", after="Matched", duration=0.6),
    stage("Current calibration", "PASS", before=18.51449, after=20.03049, duration=31),
    stage("Voltage calibration", "PASS", before=55.2, after=56.03, duration=24),
    stage("Open-circuit voltage test", "PASS", after=56.04, duration=9),
    stage("Full-current test", "PASS", after=20.03, duration=21),
    stage("End-current cutoff test", "PASS", after=1.45, duration=46),
]
TEST_PASS_RECORD = {"results": [
    {"name": "Open-circuit voltage test", "status": "PASS", "measured": 56.04},
    {"name": "Full-current test", "status": "PASS", "measured": 20.03},
    {"name": "End-current cutoff test", "status": "PASS", "measured": 1.45}],
    "total_can_tx_count": 0}


def pass_attempt(attempt_id="a1", sequence=1, **extra):
    extra.setdefault("preflight_result", {"status": "PASS", "updated_fields": []})
    return attempt(attempt_id, sequence, outcome="PASS", calibration_result="PASS", test_result="PASS",
                   stages=copy.deepcopy(PASS_STAGES), cal=cal_record(20.03049, 56.03), test=copy.deepcopy(TEST_PASS_RECORD),
                   **extra)


def by_name(report):
    return {row["name"]: row for row in report["stages"]}


class VerdictTests(unittest.TestCase):
    def test_pass_has_no_failure_and_persistence_flag(self):
        report = build_report(session([pass_attempt()]), SERIAL, station="LW-EOL-01", now_utc=NOW)
        self.assertEqual(report["schema"], "leanwatts-eol-report/1")
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual((report["calibration_verdict"], report["test_verdict"]), ("PASS", "PASS"))
        self.assertIsNone(report["failure"])
        self.assertIn("PERSISTENCE_UNVERIFIED", report["flags"])
        self.assertEqual([row["name"] for row in report["stages"]], [
            "Baseline open voltage", "Baseline current", "Charger parameters", "Current calibration",
            "Voltage calibration", "Open-circuit voltage", "Full current", "End-current cutoff"])

    def test_cutoff_fail_names_stage_with_bracket(self):
        cutoff = [stage("End-current cutoff test", "FAIL", after=1.25, duration=46, detail="Charger cut off too late.")]
        test = {"results": [{"name": "End-current cutoff test", "status": "FAIL", "measured": 1.25,
                             "observed_interval_a": [1.2, 1.3]}], "total_can_tx_count": 0}
        failing = attempt("a1", 1, outcome="FAIL", calibration_result="PASS", test_result="FAIL",
                          stages=copy.deepcopy(PASS_STAGES[:7]) + cutoff, cal=cal_record(20.03049, 56.03),
                          test=test, preflight_result={"status": "PASS", "updated_fields": []})
        report = build_report(session([failing]), SERIAL, station="LW-EOL-01", now_utc=NOW)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertEqual(report["failure"], {"stage": "End-current cutoff", "observed": "1.200–1.300 A",
                                             "required": "1.400–1.600 A"})
        row = by_name(report)["End-current cutoff"]
        self.assertEqual(row["window"], [1.4, 1.6])
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("Charger cut off too late.", row["note"])

    def test_calibration_only_is_incomplete_with_test_not_run(self):
        stages = [s for s in PASS_STAGES[:5]] + [stage("Open-circuit voltage", "NOT RUN")]
        cal_only = attempt("a1", 1, outcome="CALIBRATED", calibration_result="PASS", test_result="NOT RUN",
                           stages=stages, cal=cal_record(20.03049, 56.03), run_tests=False, test=None,
                           preflight_result={"status": "PASS", "updated_fields": []})
        report = build_report(session([cal_only]), SERIAL, station="LW-EOL-01", now_utc=NOW)
        self.assertEqual(report["verdict"], "INCOMPLETE")
        self.assertEqual((report["calibration_verdict"], report["test_verdict"]), ("PASS", "NOT RUN"))
        self.assertIsNone(report["failure"])
        self.assertEqual(by_name(report)["Full current"]["status"], "NOT RUN")

    def test_test_only_is_incomplete_without_calibration(self):
        test_only = attempt("a1", 1, outcome="PASS", calibration_result="NOT RUN", test_result="PASS",
                            stages=[s for s in PASS_STAGES if "test" in s["name"].lower() or "cutoff" in s["name"].lower()],
                            run_calibration=False, cal=None, test=copy.deepcopy(TEST_PASS_RECORD))
        report = build_report(session([test_only]), SERIAL, station="LW-EOL-01", now_utc=NOW)
        self.assertEqual(report["verdict"], "INCOMPLETE")
        self.assertEqual((report["calibration_verdict"], report["test_verdict"]), ("NOT RUN", "PASS"))
        self.assertEqual(by_name(report)["Current calibration"]["status"], "NOT RUN")
        self.assertEqual(by_name(report)["Open-circuit voltage"]["status"], "PASS")

    def test_stopped_stage_is_a_fail(self):
        stopped = attempt("a1", 1, outcome="STOPPED", calibration_result="FAIL", test_result="NOT RUN",
                          stages=[stage("Current calibration", "STOPPED", before=18.5, after=19.2, duration=12,
                                        detail="Stopped by operator. Unfinished stages are not a PASS.")],
                          cal=cal_record(), test=None, run_tests=True)
        report = build_report(session([stopped]), SERIAL, station="LW-EOL-01", now_utc=NOW)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertEqual(report["failure"]["stage"], "Current calibration")
        self.assertEqual(report["failure"]["observed"], "19.200 A")
        self.assertEqual(report["failure"]["required"], "19.900–20.100 A")

    def test_simulation_keeps_real_fail_visible(self):
        simulated = attempt("a1", 1, outcome="SIMULATION", calibration_result="SIMULATION", test_result="SIMULATION",
                            stages=[s if s["name"] != "Full-current test" else
                                    stage("Full-current test", "FAIL", after=19.1, duration=21, detail="Plateau below target.")
                                    for s in copy.deepcopy(PASS_STAGES)],
                            cal=cal_record(20.03, 56.03), test=copy.deepcopy(TEST_PASS_RECORD), simulation=True)
        report = build_report(session([simulated]), SERIAL, station="LW-EOL-01", now_utc=NOW)
        self.assertEqual(report["verdict"], "SIMULATION")
        self.assertIn("SIMULATION", report["flags"])
        self.assertEqual(by_name(report)["Full current"]["status"], "FAIL")
        self.assertEqual(report["failure"]["stage"], "Full current")

    def test_other_number_unit_is_flagged(self):
        custom = pass_attempt(serial_override=True)
        custom["serial"] = "LAB-TEST-0042"
        report = build_report(session([custom]), "LAB-TEST-0042", station="LW-EOL-01", now_utc=NOW)
        self.assertIn("CUSTOM_NUMBER", report["flags"])
        self.assertEqual(report["serial"], "LAB-TEST-0042")
        with self.assertRaises(ValueError):
            ensure_serial_ok("LAB-TEST-0042")

    def test_latest_group_wins_over_earlier_revision(self):
        earlier = attempt("a1", 1, outcome="FAIL", calibration_result="FAIL", test_result="NOT RUN",
                          stages=[stage("Current calibration", "FAIL", before=18.5, after=18.7)],
                          cal=cal_record(), test=None, revision=2, preflight_result={"status": "PASS", "updated_fields": []})
        later = pass_attempt("a2", 2, revision=3)
        report = build_report(session([earlier, later]), SERIAL, station="LW-EOL-01", now_utc=NOW)
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["evidence"]["attempt_ids"], ["a2"])

    def test_missing_serial_raises(self):
        with self.assertRaises(ValueError):
            build_report(session([pass_attempt()]), "WO-S00999-1-001", station="LW", now_utc=NOW)

    def test_running_attempt_is_refused(self):
        with self.assertRaises(ValueError):
            build_report(session([dict(pass_attempt(), outcome="RUNNING")]), SERIAL, station="LW", now_utc=NOW)

    def test_rejects_naive_time(self):
        with self.assertRaises(ValueError):
            build_report(session([pass_attempt()]), SERIAL, station="LW", now_utc=datetime(2026, 11, 13))


class RoundingAndTimeTests(unittest.TestCase):
    def test_volts_and_amps_round_to_three_decimals(self):
        rows = by_name(build_report(session([pass_attempt()]), SERIAL, station="LW-EOL-01", now_utc=NOW))
        self.assertEqual(rows["Current calibration"]["initial"], 18.514)
        self.assertEqual(rows["Current calibration"]["final"], 20.03)
        self.assertEqual(rows["Current calibration"]["required"], 20.0)
        self.assertEqual(rows["Current calibration"]["duration_s"], 31)

    def test_utc_and_ist_rendering(self):
        report = build_report(session([pass_attempt()]), SERIAL, station="LW-EOL-01", now_utc=NOW)
        self.assertEqual(report["finished_at_utc"], "2026-11-13T10:11:00+00:00")
        self.assertEqual(report["finished_at_ist"], "2026-11-13T15:41:00+05:30")
        self.assertEqual(report["generated_at_utc"], "2026-11-13T10:20:00+00:00")
        page = render_html(report)
        self.assertIn("15:41 IST", page)
        self.assertIn("10:11 UTC", page)

    def test_now_is_converted_to_utc_offset(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        report = build_report(session([pass_attempt()]), SERIAL, station="LW-EOL-01",
                              now_utc=datetime(2026, 11, 13, 15, 50, tzinfo=ist))
        self.assertEqual(report["generated_at_utc"], "2026-11-13T10:20:00+00:00")

    def test_note_is_capped_at_25_words(self):
        long = " ".join(f"word{i}" for i in range(40))
        stages = copy.deepcopy(PASS_STAGES)
        stages[2]["detail"] = long
        report = build_report(session([pass_attempt(stage_details=stages)]), SERIAL, station="LW-EOL-01", now_utc=NOW)
        self.assertLessEqual(len(by_name(report)["Charger parameters"]["note"].split()), 25)


class SizeTests(unittest.TestCase):
    def test_notes_drop_first_then_too_large_raises(self):
        stages = copy.deepcopy(PASS_STAGES)
        stages[3]["detail"] = "Calibration note with several words to keep the note non-empty."
        data = session([pass_attempt(stage_details=stages)])
        full = build_report(data, SERIAL, station="LW-EOL-01", now_utc=NOW)
        without_notes = copy.deepcopy(full)
        for row in without_notes["stages"]:
            row["note"] = ""
        limit = len(json.dumps(without_notes, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 5
        self.assertLess(limit, len(json.dumps(full, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
        with mock.patch.object(eol_report, "MAX_BYTES", limit):
            trimmed = build_report(data, SERIAL, station="LW-EOL-01", now_utc=NOW)
        self.assertTrue(all(row["note"] == "" for row in trimmed["stages"]))
        with mock.patch.object(eol_report, "MAX_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "report too large"):
                build_report(data, SERIAL, station="LW-EOL-01", now_utc=NOW)

    def test_default_cap_is_256_kib(self):
        self.assertEqual(eol_report.MAX_BYTES, 256 * 1024)
        report = build_report(session([pass_attempt()]), SERIAL, station="LW-EOL-01", now_utc=NOW)
        self.assertLessEqual(len(json.dumps(report).encode("utf-8")), 256 * 1024)


class InterruptedAttemptTests(unittest.TestCase):
    def interrupted(self, **extra):
        return attempt("a1", 1, outcome="INTERRUPTED", calibration_result="INCOMPLETE", test_result="NOT RUN",
                       stages=[], cal=None, test=None, run_tests=False, preflight_result=None, **extra)

    def test_interrupted_attempt_uses_operator_and_station_recorded_at_begin(self):
        begun = self.interrupted(operator={"id": "OP09", "name": "S. Rao", "role": "lead"}, station="LW-EOL-02")
        report = build_report(session([begun]), SERIAL, station="LW-EOL-01", now_utc=NOW)
        self.assertEqual(report["verdict"], "INCOMPLETE")
        self.assertEqual(report["operator"], {"id": "OP09", "name": "S. Rao", "role": "lead"})
        self.assertEqual(report["station"], "LW-EOL-02")

    def test_older_record_without_the_fields_falls_back_to_session_and_settings(self):
        older = self.interrupted(operator=None, station=None)
        report = build_report(session([older]), SERIAL, station="LW-EOL-01", now_utc=NOW)
        self.assertEqual(report["operator"], {"id": "OP07", "name": "R. Kumar", "role": "operator"})
        self.assertEqual(report["station"], "LW-EOL-01")
        absent = self.interrupted()
        del absent["operator"], absent["station"]
        report = build_report(session([absent]), SERIAL, station="LW-EOL-01", now_utc=NOW)
        self.assertEqual(report["operator"]["id"], "OP07")
        self.assertEqual(report["station"], "LW-EOL-01")


EVENTS = [
    "2026-11-12T10:41:23.717+00:00  Started calibration.",
    "2026-11-12T10:42:00.685+00:00  Current direction probe 1: duty 74.6; baseline 20.61 A.",
    "2026-11-12T10:42:02.942+00:00  Current duty 73.6; measured 20.64 A; requested output step 0.479409 A; estimated 0.3.",
    "2026-11-12T10:42:05.188+00:00  Current duty 72.7; measured 20.36 A; requested output step 0.27 A; estimated 0.27.",
    "2026-11-12T10:42:07.460+00:00  Current duty 72.4; measured 20.11 A; requested output step 0.1 A; estimated 0.09.",
    "2026-11-12T10:42:18.793+00:00  Voltage direction probe 1: duty 82.2; baseline 57.37 V.",
    "2026-11-12T10:42:21.028+00:00  Voltage duty 81.2; measured 57.41 V; requested output step 1.05 V; estimated 0.37.",
    "2026-11-12T10:42:23.572+00:00  Voltage duty 80.4; measured 56.74 V; requested output step 0.55 V; estimated 0.53.",
    "2026-11-12T10:42:30.370+00:00  Voltage duty 80.1; measured 56.07 V; requested output step 0.05 V; estimated 0.07.",
    "2026-11-12T10:42:37.237+00:00  CALIBRATED: Current duty and voltage save requests were accepted.",
]


class CalibrationDetailTests(unittest.TestCase):
    def report(self, record):
        unit = attempt("a1", 1, outcome="PASS", calibration_result="PASS", test_result="PASS",
                       stages=copy.deepcopy(PASS_STAGES), cal=record, test=copy.deepcopy(TEST_PASS_RECORD),
                       preflight_result={"status": "PASS", "updated_fields": []})
        return build_report(session([unit]), SERIAL, station="LW-EOL-01", now_utc=NOW)

    def test_adjustment_counts_per_axis_come_from_recorded_events(self):
        report = self.report(dict(cal_record(20.03, 56.03), events=EVENTS))
        self.assertEqual(report["calibration"]["current_adjustments"], 4)
        self.assertEqual(report["calibration"]["voltage_adjustments"], 4)

    def test_zero_is_a_real_count_when_events_exist_without_adjustments(self):
        report = self.report(dict(cal_record(20.03, 56.03), events=[EVENTS[0]]))
        self.assertEqual((report["calibration"]["current_adjustments"], report["calibration"]["voltage_adjustments"]), (0, 0))

    def test_missing_events_leave_counts_null_and_the_report_says_not_recorded(self):
        report = self.report(cal_record(20.03, 56.03))
        self.assertIsNone(report["calibration"]["current_adjustments"])
        self.assertIsNone(report["calibration"]["voltage_adjustments"])
        page = render_html(report)
        self.assertIn("Current duty adjustments</span><b>not recorded</b>", page)
        self.assertIn("Voltage duty adjustments</span><b>not recorded</b>", page)

    def test_counts_are_shown_in_the_html(self):
        page = render_html(self.report(dict(cal_record(20.03, 56.03), events=EVENTS)))
        self.assertIn("Current duty adjustments</span><b>4</b>", page)
        self.assertIn("Voltage duty adjustments</span><b>4</b>", page)

    def test_charger_firmware_is_null_because_the_app_never_reads_it(self):
        # Charger observations hold status, fault word and parameters only; the load's firmware is not the charger's.
        record = dict(cal_record(20.03, 56.03), charger_observations={"outputVoltage": 0.8, "statusByte": 0, "faultWord": 0})
        report = self.report(record)
        self.assertIsNone(report["identity"]["charger_fw"])
        self.assertIn("0x002A", report["identity"]["load"])
        page = render_html(report)
        self.assertIn("charger firmware not recorded", page)
        self.assertNotIn("charger firmware 0x", page)


class HtmlTests(unittest.TestCase):
    def test_values_are_escaped(self):
        hostile = dict(pass_attempt(), operator={"id": "OP1", "name": "<script>alert(1)</script>", "role": "operator"})
        report = build_report(session([hostile]), SERIAL, station="<script>x</script>", now_utc=NOW)
        page = render_html(report)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>x</script>", page)

    def test_no_external_resources(self):
        page = render_html(build_report(session([pass_attempt()]), SERIAL, station="LW-EOL-01", now_utc=NOW))
        for needle in ("http://", "https://", "<link", "<img", "src=", "@import", "url("):
            self.assertNotIn(needle, page)
        self.assertIn("prefers-color-scheme", page)
        self.assertIn("@media print", page)
        self.assertLessEqual(len(page.encode("utf-8")), 256 * 1024)

    def test_failure_is_shown_in_html(self):
        cutoff = [stage("End-current cutoff test", "FAIL", after=1.25, detail="Late.")]
        test = {"results": [{"name": "End-current cutoff test", "status": "FAIL", "measured": 1.25,
                             "observed_interval_a": [1.2, 1.3]}], "total_can_tx_count": 0}
        failing = attempt("a1", 1, outcome="FAIL", calibration_result="PASS", test_result="FAIL",
                          stages=copy.deepcopy(PASS_STAGES[:7]) + cutoff, cal=cal_record(20.03049, 56.03),
                          test=test, preflight_result={"status": "PASS", "updated_fields": []})
        page = render_html(build_report(session([failing]), SERIAL, station="LW-EOL-01", now_utc=NOW))
        self.assertIn("Why it failed", page)
        self.assertIn("1.200–1.300 A", page)


class _FakeRecipe:
    def copy(self):
        return self


class CaptureFieldTests(unittest.TestCase):
    def test_station_and_variant_flag_defaults_and_validation(self):
        flow = ProductionFlow(_FakeRecipe(), SERIAL, OPERATOR, 1, None, None)
        self.assertEqual(flow.station, "UNKNOWN STATION")
        self.assertIsNone(flow.production_ready)
        flow = ProductionFlow(_FakeRecipe(), SERIAL, OPERATOR, 1, None, None,
                              station="  LW-EOL-01 ", production_ready=False)
        self.assertEqual(flow.station, "LW-EOL-01")
        self.assertIs(flow.production_ready, False)
        with self.assertRaises(ValueError):
            ProductionFlow(_FakeRecipe(), SERIAL, OPERATOR, 1, None, None, production_ready="yes")

    def test_operator_and_load_identity_capture(self):
        flow = ProductionFlow(_FakeRecipe(), SERIAL, OPERATOR, 1, None, None)
        self.assertEqual(flow._operator_identity(), {"id": "OP07", "name": "R. Kumar", "role": "operator"})
        self.assertEqual(ProductionFlow(_FakeRecipe(), SERIAL, "Tester", 1, None, None)._operator_identity(),
                         {"id": None, "name": "Tester", "role": None})
        self.assertIsNone(flow._load_identity())
        flow.calibration = {"load_probe": {"model_code": 0x42, "firmware_code": 0x2A, "port": "COM11", "extra": 1}}
        self.assertEqual(flow._load_identity(), {"model_code": 0x42, "firmware_code": 0x2A, "port": "COM11"})


if __name__ == "__main__":
    unittest.main()
