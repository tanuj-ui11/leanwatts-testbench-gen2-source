"""TEMPORARY STUB (branch feat/eol-t5-outbox only). T3 owns the real eol_report module; replace this file when T3 merges.

Provides the two functions the Generate report button calls, with the spec 3.4 keys that
outbox_service.report_body reads. Verdict logic here is simplified and must not ship.
"""
from __future__ import annotations

import html as _html
from datetime import datetime, timezone


def build_report(*, session, serial, station, software_version, generated_at=None):
    from .session_store import SessionStore
    summary = next((item for item in SessionStore.summarize(session) if item.get("serial") == serial), None)
    if summary is None:
        raise ValueError("No finished attempts for this serial.")
    outcome = summary.get("outcome")
    if summary.get("simulation"):
        verdict = "SIMULATION"
    elif outcome == "PASS":
        verdict = "PASS"
    elif outcome in ("FAIL", "ERROR", "STOPPED", "ABORTED", "INTERRUPTED"):
        verdict = "FAIL"
    else:
        verdict = "INCOMPLETE"
    operator = session.get("operator") if isinstance(session.get("operator"), dict) else {}
    finished = summary.get("finished_at") or generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "schema": "leanwatts-eol-report/1",
        "report_id": "EOL-" + str(summary.get("group_id", ""))[:16],
        "serial": serial,
        "verdict": verdict,
        "calibration": summary.get("calibration_result", "NOT RUN"),
        "test": summary.get("test_result", "NOT RUN"),
        "failure": {"stage": "", "observed": "", "required": ""} if verdict == "PASS" else
                   {"stage": "", "observed": "", "required": "", "reason": summary.get("failure_reason", "")},
        "flags": [],
        "operator": {"id": str(operator.get("id", "")), "name": str(operator.get("name", "")), "role": str(operator.get("role", ""))},
        "station": station,
        "software": {"name": "Leanwatts Testbench Gen2", "version": software_version},
        "started_at_utc": summary.get("started_at") or "",
        "finished_at_utc": finished,
        "elapsed_seconds": None,
        "stages": [],
        "identity": {},
        "evidence": {"session_id": session.get("session_id", ""), "attempt_ids": list(summary.get("attempt_ids", []))},
    }


def render_html(report):
    rows = "".join(f"<tr><th>{_html.escape(str(key))}</th><td>{_html.escape(str(value))}</td></tr>"
                   for key, value in report.items() if not isinstance(value, (dict, list)))
    return ("<!doctype html><meta charset=\"utf-8\"><title>EoL report (stub)</title>"
            "<h1>EoL report (stub)</h1><table>" + rows + "</table>")
