"""Reports and summaries must not change when raw evidence is kept outside the session JSON.

`golden/session_equivalence.json` was produced by the code that stored every sample inline
(`UPDATE_GOLDEN=1` regenerates it). Every output a person or the dashboard can see has to stay
byte-for-byte identical; only the raw acquisition lists (samples, can_trace, trace_events) may live
in per-attempt sidecar files, and they must round-trip exactly.
"""
import contextlib
import copy
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from leanwatts_can import eol_report, session_store
from leanwatts_can.session_store import SessionStore

GOLDEN = Path(__file__).with_name("golden") / "session_equivalence.json"
HEAVY = ("samples", "can_trace", "trace_events")
OPERATOR = {"id": "OP07", "name": "R. Kumar", "role": "operator", "enabled": True}
RECIPE = {"name": "Gen2 Ruchira V1", "cc_current": 20.0, "current_tolerance": 0.1,
          "end_voltage": 56.0, "voltage_tolerance": 0.1, "end_current": 1.5, "end_current_tolerance": 0.1,
          "current_cal_load_voltage": 48.0, "voltage_cal_load_current": 1.0, "device_settings": {"batteryType": 1}}
NOW = datetime(2026, 11, 13, 10, 20, tzinfo=timezone.utc)
CAL_OUTCOME = "CALIBRATED — PERSISTENCE UNVERIFIED"
STOP = "Output below approved shutdown thresholds; load OFF confirmed"


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 13, 15, 37, 0, tzinfo=tz)


class _Uuid:
    def __init__(self, counter):
        self.hex = format(counter, "032x")


def _heavy(tag, count=12):
    return {"samples": [{"phase": "cal_current", "voltage": 50 + i / 10, "current": 19 + i / 100, "tag": tag, "elapsed_seconds": i}
                        for i in range(count)],
            "can_trace": [{"dir": "RX", "id": 0x18FF50E5 + i, "data": "00" * 8, "tag": tag} for i in range(count // 2)],
            "trace_events": [{"event": "frame", "n": i, "tag": tag} for i in range(count * 2)]}


def _events(*lines):
    return [f"10:{i:02d}:00 {line}" for i, line in enumerate(lines)]


def _stage(name, status, before=None, after=None, unit="A", detail=""):
    return {"name": name, "status": status, "before": before, "after": after, "unit": unit,
            "duration_seconds": 12.5, "detail": detail}


def calibration(tag, ok=True, with_heavy=True):
    status = "SAVE REQUEST ACCEPTED" if ok else "FAIL"
    record = {"schema": "leanwatts-automation/1", "is_running": False, "simulation": False,
              "outcome": CAL_OUTCOME if ok else "FAIL — current did not settle", "stop_outcome": STOP,
              "current_duty": 74.5, "voltage_duty": 82.1,
              "results": [{"name": "Current calibration", "status": status, "measured": 20.03, "duration_seconds": 31},
                          {"name": "Voltage calibration", "status": status, "measured": 56.03, "duration_seconds": 24,
                           "detail": "" if ok else "Voltage plateau not found"}],
              "events": _events("Current duty 73.6; step", "Current duty 74.0; step", "Voltage direction probe 1: up",
                                "Voltage duty 82.0; step", "ERROR: x" if not ok else "done"),
              "preflight_result": {"status": "PASS", "updated_fields": ["endVoltage"], "before": {"endVoltage": 55.0},
                                   "after": {"endVoltage": 56.0}, "desired": {"endVoltage": 56.0}},
              "baseline_result": {"status": "PASS", "open_voltage": {"status": "PASS", "measured": 55.2},
                                  "current": {"status": "PASS", "measured": 18.5}},
              "direction_detection": {"current": "up"}, "stage_details": []}
    if with_heavy:
        record.update(_heavy(tag))
    return record


def test_record(tag, ok=True, with_heavy=True):
    status = "PASS" if ok else "FAIL"
    record = {"schema": "leanwatts-automation/1", "is_running": False, "simulation": False,
              "outcome": "PASS" if ok else "FAIL", "external_restart": True, "test_cleanup_verified": True,
              "total_can_tx_count": 0, "can_tx_count": 0,
              "results": [{"name": "Open-circuit voltage", "status": "PASS", "measured": 56.04, "duration_seconds": 9},
                          {"name": "Full current", "status": "PASS", "measured": 20.03, "duration_seconds": 21},
                          {"name": "End current / observed shutdown", "status": status, "measured": 1.45,
                           "duration_seconds": 46, "observed_interval_a": [1.6, 1.8] if not ok else [1.4, 1.5],
                           "detail": "" if ok else "Output never shut down at the cutoff"}],
              "events": _events("test started", "cutoff step"), "stage_details": []}
    if with_heavy:
        record.update(_heavy(tag))
    return record


def cal_result(tag, outcome="CALIBRATED", ok=True, simulation=False, stage_status=None):
    cal = calibration(tag, ok=ok)
    stages = [_stage("Baseline current", "DONE", after=18.5), _stage("Current calibration", stage_status or ("PASS" if ok else "FAIL"), 18.5, 20.03),
              _stage("Voltage calibration", stage_status or ("PASS" if ok else "FAIL"), 55.2, 56.03, "V")]
    return {"outcome": outcome, "calibration_result": ("SIMULATION" if simulation else "PASS" if ok else "FAIL"),
            "test_result": "NOT RUN", "simulation": simulation, "calibration": cal, "test": None,
            "stage_details": stages, "preflight_result": cal["preflight_result"], "baseline_result": cal["baseline_result"],
            "starting_point": {"method": "configured", "current_initial": 70.0, "voltage_initial": 80.0,
                               "offset": 0.0, "clamped": [], "note": "x"}, "persistence_verified": False,
            "detail": "Calibration " + outcome, "elapsed_seconds": 91.0}


def test_only_result(tag, outcome="PASS", simulation=False):
    ok = outcome == "PASS"
    stages = [_stage("Open-circuit voltage test", "PASS", after=56.04, unit="V"),
              _stage("Full-current test", "PASS", after=20.03),
              _stage("End-current cutoff test", "PASS" if ok else "FAIL", after=1.45)]
    return {"outcome": outcome, "calibration_result": "NOT RUN", "test_result": "SIMULATION" if simulation else "PASS" if ok else "FAIL",
            "simulation": simulation, "calibration": None, "test": test_record(tag, ok=ok), "stage_details": stages,
            "detail": "Test " + outcome, "elapsed_seconds": 70.0}


def run(store, serial, kind, result, **begin):
    attempt = store.begin_unit(serial, copy.deepcopy(RECIPE), 3, run_tests=kind == "test",
                               run_calibration=kind == "cal", station="LW-EOL-01", **begin)
    if result is not None:
        store.finish_unit(attempt, result)
    return attempt


@contextlib.contextmanager
def deterministic():
    counter = iter(range(1, 10000))
    stamp = iter(range(10000))
    fixed_now = lambda: "2026-11-13T15:%02d:%02d+05:30" % divmod(next(stamp), 60)
    with mock.patch.object(session_store, "datetime", _FixedDatetime), \
            mock.patch.object(session_store.uuid, "uuid4", lambda: _Uuid(next(counter))), \
            mock.patch.object(session_store, "now_text", fixed_now):
        yield


def scenario(name, folder):
    mode = {"calibration_only": "calibration_only", "test_only": "test_only",
            "simulation": "independent"}.get(name, "both_required")
    store = SessionStore(folder)
    store.start(OPERATOR, mode=mode)
    S1, S2 = "WO-S00430-1-001", "WO-S00430-1-002"
    if name == "pass":
        run(store, S1, "cal", cal_result("a"))
        run(store, S1, "test", test_only_result("b"))
        run(store, S2, "cal", cal_result("c"))
        run(store, S2, "test", test_only_result("d"))
    elif name == "cutoff_fail":
        run(store, S1, "cal", cal_result("a"))
        run(store, S1, "test", test_only_result("b", outcome="FAIL"))
    elif name == "stopped_interrupted":
        run(store, S1, "cal", cal_result("a", outcome="STOPPED", ok=False, stage_status="STOPPED"))
        store.begin_unit(S1, copy.deepcopy(RECIPE), 3, run_tests=False, run_calibration=True)  # left running
        store = SessionStore(folder)  # restart: crash recovery marks it interrupted
    elif name == "calibration_only":
        run(store, S1, "cal", cal_result("a"))
        run(store, S2, "cal", cal_result("b", ok=False, outcome="FAIL"))
    elif name == "test_only":
        run(store, S1, "test", test_only_result("a"))
    elif name == "simulation":
        run(store, S1, "cal", cal_result("a", outcome="SIMULATION", simulation=True))
    elif name == "repeated_serial":
        run(store, S1, "cal", cal_result("a", outcome="FAIL", ok=False))
        run(store, S1, "cal", cal_result("b"))
        run(store, S1, "test", test_only_result("c"))
    else:
        raise AssertionError(name)
    return store


SCENARIOS = ("pass", "cutoff_fail", "stopped_interrupted", "calibration_only", "test_only", "simulation", "repeated_serial")


def strip_heavy(value):
    """Remove raw acquisition lists from any nested evidence (used on both sides of a comparison)."""
    if isinstance(value, dict):
        return {k: strip_heavy(v) for k, v in value.items() if k not in HEAVY}
    if isinstance(value, list):
        return [strip_heavy(v) for v in value]
    return value


def with_evidence(store, data):
    hydrate = getattr(store, "hydrate_session", None)
    return hydrate(data) if hydrate else data


def outputs(store, folder):
    """Everything visible outside the store for one scenario."""
    session_id = (store.active() or store.list_sessions()[0])["session_id"]
    if store.active():
        store.close()
    paths = store.report_paths(session_id)
    files = {key: re.sub(r"base64,[A-Za-z0-9+/=]+", "base64,LOGO", Path(path).read_bytes().decode("utf-8"))
             for key, path in paths.items() if key != "json"}
    reopened = SessionStore(folder)
    loaded = reopened.list_sessions()[0]
    serials = sorted({unit["serial"] for unit in loaded["units"]})
    eol = {}
    for serial in serials:
        try:
            report = eol_report.build_report(session=loaded, serial=serial, station="ST1", now_utc=NOW)
            eol[serial] = {"json": report, "html": eol_report.render_html(report)}
        except ValueError as exc:
            eol[serial] = {"error": str(exc)}
    full = with_evidence(reopened, loaded)
    return {"files": files, "summaries": SessionStore.summarize(loaded),
            "summaries_evidence": SessionStore.summarize(loaded, evidence=True),
            "summaries_evidence_full": SessionStore.summarize(full, evidence=True),
            "eol": eol, "units_full": full["units"], "completion_state": loaded["completion_state"]}


def golden_view(result):
    """Json-normalised view with the evidence-bearing summary taken from the fully hydrated session."""
    view = dict(result)
    view["summaries_evidence"] = result["summaries_evidence_full"]
    view.pop("summaries_evidence_full")
    return json.loads(json.dumps(view, sort_keys=True))


class EquivalenceTests(unittest.TestCase):
    def test_outputs_match_the_inline_evidence_golden(self):
        current = {}
        for name in SCENARIOS:
            with tempfile.TemporaryDirectory() as folder, deterministic():
                store = scenario(name, folder)
                result = outputs(store, folder)
                # Light summaries must equal the full ones except for the raw lists.
                self.assertEqual(strip_heavy(result["summaries_evidence"]), strip_heavy(result["summaries_evidence_full"]), name)
                current[name] = golden_view(result)
        if os.environ.get("UPDATE_GOLDEN"):
            GOLDEN.write_text(json.dumps(current, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        for name in SCENARIOS:
            for key in expected[name]:
                self.assertEqual(current[name][key], expected[name][key], f"{name}: {key}")
            self.assertEqual(set(current[name]), set(expected[name]))

    def test_golden_files_are_present_and_cover_every_scenario(self):
        expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(set(expected), set(SCENARIOS))
        self.assertTrue(all(expected[name]["files"]["csv"] for name in SCENARIOS))


if __name__ == "__main__":
    unittest.main()
