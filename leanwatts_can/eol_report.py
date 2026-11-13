"""Per-serial end-of-line report: the leanwatts-eol-report/1 envelope and a standalone HTML view.

Pure functions only: no instrument, serial, CAN or network access. Times are stored as UTC with
an offset; IST (+05:30) is rendered for people. Volts and amps are rounded to 3 decimals, once.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
from datetime import datetime, timedelta, timezone

from .serial_numbers import validate_serial
from .session_store import SessionStore, stage_failure_reason, test_stage_name
from .version import VERSION


SCHEMA = "leanwatts-eol-report/1"
MAX_BYTES = 256 * 1024
IST = timezone(timedelta(hours=5, minutes=30), "IST")
SOFTWARE_NAME = "Leanwatts Testbench Gen2"
UNKNOWN_STATION = "UNKNOWN STATION"
NOTE_WORDS = 25

# Fixed row order from spec 3.1 item 5: (label, engine names after test_stage_name, unit, recipe required key, recipe tolerance key).
STAGES = (
    ("Baseline open voltage", ("Baseline open voltage",), "V", None, None),
    ("Baseline current", ("Baseline current",), "A", None, None),
    ("Charger parameters", ("Charger parameters",), "", None, None),
    ("Current calibration", ("Current calibration",), "A", "cc_current", "current_tolerance"),
    ("Voltage calibration", ("Voltage calibration",), "V", "end_voltage", "voltage_tolerance"),
    ("Open-circuit voltage", ("Open-circuit voltage test",), "V", "end_voltage", "voltage_tolerance"),
    ("Full current", ("Full-current test",), "A", "cc_current", "current_tolerance"),
    ("End-current cutoff", ("End-current cutoff test",), "A", "end_current", "end_current_tolerance"),
)
FAILING_STAGE_STATUSES = {"FAIL", "FAILED", "ERROR", "ABORTED", "STOPPED"}
FAILING_OUTCOMES = {"FAIL", "FAILED", "ERROR", "ABORTED", "STOPPED"}
FLAG_LABELS = {
    "SIMULATION": "SIMULATION",
    "DRAFT_VARIANT": "DRAFT VARIANT",
    "PERSISTENCE_UNVERIFIED": "Persistence after AC power cycle not verified",
    "DIAGNOSTIC": "DIAGNOSTIC",
    "CUSTOM_NUMBER": "CUSTOM NUMBER",
}
CALIBRATION_LABELS = (
    ("parameters", "Charger parameters"), ("parameters_written", "Parameters written"),
    ("current_duty", "Current duty"), ("voltage_duty", "Voltage duty"),
    ("calibration_load_cv_v", "Calibration load CV (V)"), ("calibration_load_cc_a", "Calibration load CC (A)"),
)
TEST_LABELS = (
    ("open_circuit_voltage_v", "Open-circuit voltage (V), load OFF"), ("full_current_a", "Full current (A)"),
    ("cutoff_interval_a", "Cutoff observed (A)"), ("application_can_commands", "Application CAN commands"),
)


def ensure_serial_ok(serial):
    """Return the serial when it is a standard charger number; raise ValueError otherwise."""
    error = validate_serial(serial, allow_custom=False)
    if error:
        raise ValueError(error)
    return serial.strip()


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _num(value):
    return round(float(value), 3) if _finite(value) else None


def _window_text(window, unit):
    if not window:
        return None
    return f"{window[0]:.3f}–{window[1]:.3f} {unit}".rstrip()


def _utc(text):
    if not text:
        return None
    value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)  # Session timestamps are written in the station zone.
    return value.astimezone(timezone.utc)


def _iso(moment, zone):
    return moment.astimezone(zone).isoformat(timespec="seconds") if moment is not None else None


def _aware_utc(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("now_utc must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _note(text, limit=NOTE_WORDS):
    return " ".join(str(text or "").split()[:limit])


def _find(records, names):
    """Last matching record: a real stage replaces earlier NOT RUN placeholders."""
    match = None
    for item in records:
        if isinstance(item, dict) and test_stage_name(item.get("name", "")) in names:
            match = item
    return match


def _recipe_sha256(recipe):
    if not isinstance(recipe, dict) or not recipe:
        return None
    try:
        payload = json.dumps(recipe, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _latest_group(session, serial):
    summaries = [item for item in SessionStore.summarize(session, evidence=True) if item.get("serial") == serial]
    if not summaries:
        raise ValueError("No attempts for this serial are recorded in the session.")
    order = {unit.get("attempt_id"): index for index, unit in enumerate(session.get("units", []))}
    return max(summaries, key=lambda item: order.get((item.get("attempt_ids") or [None])[-1], -1))


def _stage_rows(summary, recipe):
    """Return (rows, meta). meta holds the observed text and cutoff interval used by failure."""
    stage_items = summary.get("stage_details") or []
    results = [item for record in (summary.get("calibration"), summary.get("test"))
               if isinstance(record, dict) for item in (record.get("results") or []) if isinstance(item, dict)]
    rows, meta = [], []
    for label, names, unit, required_key, tolerance_key in STAGES:
        item = _find(stage_items, names) or {}
        result = _find(results, names) or {}
        status = str(item.get("status") or result.get("status") or "NOT RUN").upper()
        if status == "SAVE REQUEST ACCEPTED":
            status = "PASS"
        required = _num(recipe.get(required_key)) if required_key else None
        tolerance = _num(recipe.get(tolerance_key)) if tolerance_key else None
        window = ([_num(required - tolerance), _num(required + tolerance)]
                  if required is not None and tolerance is not None else None)
        measured = result.get("measured") if _finite(result.get("measured")) else item.get("after")
        final = _num(measured)
        duration = item.get("duration_seconds") if _finite(item.get("duration_seconds")) else result.get("duration_seconds")
        evidence = {**result, **item}
        interval = evidence.get("observed_interval_a")
        interval = ([_num(interval[0]), _num(interval[1])]
                    if isinstance(interval, (list, tuple)) and len(interval) == 2 and all(_finite(v) for v in interval)
                    else None)
        note = (stage_failure_reason(evidence, recipe) if status in FAILING_STAGE_STATUSES and item
                else item.get("detail") or item.get("note") or result.get("detail") or "")
        rows.append({"name": label, "required": required, "tolerance": tolerance, "window": window,
                     "unit": unit, "initial": _num(item.get("before")), "final": final,
                     "duration_s": int(round(duration)) if _finite(duration) else None,
                     "status": status, "note": _note(note)})
        if interval:
            observed = _window_text(interval, unit)
        elif final is not None:
            observed = f"{final:.3f} {unit}".rstrip()
        else:
            observed = "No value recorded"
        meta.append({"observed": observed, "interval": interval})
    return rows, meta


def build_report(session, serial, *, station, now_utc):
    """Build the leanwatts-eol-report/1 envelope for one serial from its latest attempts."""
    if not isinstance(session, dict) or session.get("schema") != "leanwatts-session/1":
        raise ValueError("Not a Testbench session record.")
    serial = str(serial).strip()
    generated = _aware_utc(now_utc)
    summary = _latest_group(session, serial)
    units = {unit.get("attempt_id"): unit for unit in session.get("units", [])}
    attempts = [units[attempt_id] for attempt_id in summary["attempt_ids"] if attempt_id in units]
    if not attempts:
        raise ValueError("No attempts for this serial are recorded in the session.")
    first, last = attempts[0], attempts[-1]
    if last.get("outcome") == "RUNNING":
        raise ValueError("This charger is still running; finish it before generating a report.")

    recipe = summary.get("recipe") if isinstance(summary.get("recipe"), dict) else {}
    rows, meta = _stage_rows(summary, recipe)
    simulation = any(bool(item.get("simulation")) for item in attempts) or bool(summary.get("simulation"))
    calibration_verdict = str(summary.get("calibration_result") or "NOT RUN")
    test_verdict = str(summary.get("test_result") or "NOT RUN")
    failing = [index for index, row in enumerate(rows) if row["status"] in FAILING_STAGE_STATUSES]
    last_outcome = str(summary.get("last_attempt_outcome") or "").upper()

    if simulation:
        verdict = "SIMULATION"
    elif failing or last_outcome in FAILING_OUTCOMES:
        verdict = "FAIL"
    elif (calibration_verdict == "PASS" and test_verdict == "PASS"
          and summary.get("calibration_completed") and summary.get("test_completed")):
        verdict = "PASS"
    else:
        verdict = "INCOMPLETE"

    failure = None
    if failing:
        index = failing[0]
        failure = {"stage": rows[index]["name"], "observed": meta[index]["observed"],
                   "required": _window_text(rows[index]["window"], rows[index]["unit"])}
    elif verdict == "FAIL":
        reason = _note(str(summary.get("failure_reason") or "").split("\n")[0]) or "No reason recorded"
        failure = {"stage": "Process", "observed": reason, "required": None}

    session_operator = session.get("operator") if isinstance(session.get("operator"), dict) else {}
    attempt_operator = last.get("operator") if isinstance(last.get("operator"), dict) else session_operator
    operator = {key: attempt_operator.get(key) for key in ("id", "name", "role")}
    attempt_station = str(last.get("station") or "").strip()
    station_value = (attempt_station if attempt_station and attempt_station != UNKNOWN_STATION
                     else str(station or "").strip() or UNKNOWN_STATION)
    production_ready = last.get("production_ready") if type(last.get("production_ready")) is bool else None
    variant = {"name": summary.get("recipe_name") or None, "revision": summary.get("recipe_revision"),
               "recipe_sha256": _recipe_sha256(recipe), "production_ready": production_ready}

    flags = []
    if simulation:
        flags.append("SIMULATION")
    if any(item.get("serial_override") for item in attempts):
        flags.append("CUSTOM_NUMBER")
    if production_ready is False:
        flags.append("DRAFT_VARIANT")
    if calibration_verdict == "PASS":
        flags.append("PERSISTENCE_UNVERIFIED")
    if any(item.get("diagnostic_only") for item in attempts):
        flags.append("DIAGNOSTIC")

    calibration_record = summary.get("calibration") if isinstance(summary.get("calibration"), dict) else {}
    test_record = summary.get("test") if isinstance(summary.get("test"), dict) else {}
    preflight = summary.get("preflight_result") if isinstance(summary.get("preflight_result"), dict) else {}
    written = preflight.get("updated_fields")
    calibration = {
        "parameters": preflight.get("status"),
        "parameters_written": list(written) if isinstance(written, list) else [],
        "current_duty": calibration_record.get("current_duty") if _finite(calibration_record.get("current_duty")) else None,
        "voltage_duty": calibration_record.get("voltage_duty") if _finite(calibration_record.get("voltage_duty")) else None,
        "calibration_load_cv_v": _num(recipe.get("current_cal_load_voltage")),
        "calibration_load_cc_a": _num(recipe.get("voltage_cal_load_current")),
    }
    tests = {
        "open_circuit_voltage_v": rows[5]["final"],
        "full_current_a": rows[6]["final"],
        "cutoff_interval_a": meta[7]["interval"],
        "application_can_commands": test_record.get("total_can_tx_count") if type(test_record.get("total_can_tx_count")) is int else None,
    }

    probe = next((item for item in (last.get("load_identity"), calibration_record.get("load_probe"),
                                    test_record.get("load_probe")) if isinstance(item, dict)), {})
    adapter = next((item for item in (last.get("can_adapter"), calibration_record.get("can_adapter"),
                                      test_record.get("can_adapter")) if isinstance(item, dict)), {})
    model, firmware = probe.get("model_code"), probe.get("firmware_code")
    load_text = None
    if type(model) is int and type(firmware) is int:
        load_text = f"model 0x{model:04X} · firmware 0x{firmware:04X}" + (f" · port {probe['port']}" if probe.get("port") else "")
    adapter_text = (f"index {adapter['index']} · {adapter['bitrate']} bit/s"
                    if type(adapter.get("index")) is int and type(adapter.get("bitrate")) is int else None)
    identity = {"load": load_text, "charger_fw": None, "can_adapter": adapter_text}

    elapsed = [item.get("elapsed_seconds") for item in attempts if _finite(item.get("elapsed_seconds"))]
    start, end = _utc(first.get("started_at")), _utc(last.get("finished_at") or last.get("started_at"))
    attempt_ids = [str(item) for item in summary["attempt_ids"]]
    report = {
        "schema": SCHEMA,
        "report_id": "EOL-" + serial + "-" + (attempt_ids[-1][:8] if attempt_ids and attempt_ids[-1] else "x"),
        "serial": serial,
        "verdict": verdict,
        "calibration_verdict": calibration_verdict,
        "test_verdict": test_verdict,
        "failure": failure,
        "flags": flags,
        "variant": variant,
        "operator": operator,
        "station": station_value,
        "software": {"name": SOFTWARE_NAME,
                     "version": str(last.get("application_version") or session.get("application_version") or VERSION)},
        "started_at_utc": _iso(start, timezone.utc),
        "finished_at_utc": _iso(end, timezone.utc),
        "finished_at_ist": _iso(end, IST),
        "elapsed_seconds": int(round(sum(elapsed))) if elapsed else None,
        "stages": rows,
        "calibration": calibration,
        "tests": tests,
        "identity": identity,
        "evidence": {"session_id": session.get("session_id"), "attempt_ids": attempt_ids},
        "supersedes": None,
        "generated_at_utc": _iso(generated, timezone.utc),
    }
    return _fit(report)


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _fit(report):
    """Drop stage notes first when the JSON is over the cap; refuse if it still does not fit."""
    if len(_encoded(report)) <= MAX_BYTES:
        return report
    for row in report["stages"]:
        row["note"] = ""
    if len(_encoded(report)) > MAX_BYTES:
        raise ValueError("report too large")
    return report


_CSS = """
:root{--bg:#f4f5f8;--card:#fff;--ink:#16181d;--mute:#5b6270;--line:#e3e6ec;--pass:#14804a;--passbg:#e5f5ec;--fail:#b3261e;--failbg:#fdebea;--warn:#8a5a00;--warnbg:#fff3d6;--na:#6b7280;--nabg:#eef0f3;--brand:#5b3fd6}
:root[data-theme="dark"]{--bg:#0f1115;--card:#171a21;--ink:#eceef3;--mute:#9aa3b2;--line:#2a2f3a;--pass:#46c785;--passbg:#12301f;--fail:#ff8a80;--failbg:#3a1614;--warn:#f2c14e;--warnbg:#3a2d0c;--na:#9aa3b2;--nabg:#232834;--brand:#9d8cff}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1115;--card:#171a21;--ink:#eceef3;--mute:#9aa3b2;--line:#2a2f3a;--pass:#46c785;--passbg:#12301f;--fail:#ff8a80;--failbg:#3a1614;--warn:#f2c14e;--warnbg:#3a2d0c;--na:#9aa3b2;--nabg:#232834;--brand:#9d8cff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.page{max-width:900px;margin:0 auto;padding:16px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-bottom:14px;overflow-wrap:anywhere}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:.04em;color:var(--brand);font-size:13px}.serial{font:700 28px/1.15 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;margin:6px 0 2px}
.sub{color:var(--mute)}.verdict{text-align:right}.badge{display:inline-block;border-radius:999px;padding:3px 12px;font-weight:700;font-size:13px;letter-spacing:.03em}
.big{font-size:22px;padding:8px 22px}.PASS{background:var(--passbg);color:var(--pass)}.FAIL{background:var(--failbg);color:var(--fail)}.NA{background:var(--nabg);color:var(--na)}.WARN{background:var(--warnbg);color:var(--warn)}
.flags{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.reason{margin-top:14px;border-left:4px solid var(--fail);background:var(--failbg);padding:10px 14px;border-radius:6px}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px 22px;margin-top:16px}
.meta div span{display:block;font-size:12px;color:var(--mute)}.meta div b{font-weight:600}
h2{font-size:15px;margin:0 0 10px}table{width:100%;border-collapse:collapse}th{font-size:12px;color:var(--mute);font-weight:600;text-align:left;border-bottom:1px solid var(--line);padding:6px 8px}
td{padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}.num{font-variant-numeric:tabular-nums}.r{text-align:right}
tr.bad td{background:var(--failbg)}.note{color:var(--mute);font-size:12.5px;margin-top:2px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:700px){.two{grid-template-columns:1fr}}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 16px}.kv span{color:var(--mute)}
.sign{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:6px}.sign div{border-top:1px solid var(--line);padding-top:6px;color:var(--mute);font-size:12.5px;min-height:46px}
.foot{color:var(--mute);font-size:12px}code{font:12px ui-monospace,Menlo,Consolas,monospace}
@media print{@page{size:A4;margin:12mm}body{background:#fff;color:#000}.card{break-inside:avoid;border-color:#ccc}}
"""


def _stamp(iso_text, zone_label):
    try:
        moment = datetime.fromisoformat(str(iso_text))
        return moment.strftime("%d %b %Y %H:%M") + " " + zone_label
    except (TypeError, ValueError):
        return "—"


def _show(value):
    if value is None:
        return "—"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "none"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_html(report):
    """Standalone HTML in the mock-up look. Every value is escaped; no external resources."""
    def e(value):
        return html.escape("" if value is None else str(value), quote=True)

    verdict = str(report.get("verdict", ""))
    verdict_class = {"PASS": "PASS", "FAIL": "FAIL", "SIMULATION": "WARN"}.get(verdict, "NA")
    cal_verdict = str(report.get("calibration_verdict", ""))
    test_verdict = str(report.get("test_verdict", ""))
    serial = str(report.get("serial", ""))
    match = re.fullmatch(r"WO-(S\d{5})-(BS\d+|\d+)-(\d{3})", serial)
    wo_line = (f"Work order {match.group(1)} · line {match.group(2)} · unit {match.group(3)}" if match else serial)
    variant = report.get("variant") or {}
    if variant.get("name"):
        wo_line += f" · variant {variant['name']}" + (f" r{variant['revision']}" if variant.get("revision") is not None else "")
    failure = report.get("failure")
    reason = ""
    if failure:
        required = failure.get("required") or "—"
        reason = (f'<div class="reason"><b>Why it failed:</b> {e(failure.get("stage"))}: observed '
                  f'{e(failure.get("observed"))}; required {e(required)}.</div>')
    flags = "".join(f'<span class="badge WARN">{e(FLAG_LABELS.get(flag, flag))}</span>' for flag in report.get("flags", []))
    operator = report.get("operator") or {}
    operator_text = " ".join(part for part in (operator.get("name"), f"({operator['id']})" if operator.get("id") else None) if part) or "—"
    elapsed = report.get("elapsed_seconds")
    total = f"{elapsed // 60} min {elapsed % 60} s" if isinstance(elapsed, int) else "—"
    finished = (f"{_stamp(report.get('finished_at_ist'), 'IST')} · {_stamp(report.get('finished_at_utc'), 'UTC')}")
    software = report.get("software") or {}
    attempts = len((report.get("evidence") or {}).get("attempt_ids") or [])

    def measure(row):
        unit = row.get("unit") or ""
        if row.get("initial") is None and row.get("final") is None:
            return "—"
        if row.get("initial") is None:
            return f"{row['final']:.3f} {unit}".rstrip()
        final = "—" if row.get("final") is None else f"{row['final']:.3f}"
        return f"{row['initial']:.3f} → {final} {unit}".rstrip()

    def required(row):
        if row.get("required") is None:
            return "—"
        unit = row.get("unit") or ""
        text = f"{row['required']:.3f} {unit}".rstrip()
        if row.get("tolerance") is not None:
            text += f" ± {row['tolerance']:.3f}"
        if row.get("window"):
            text += f" ({row['window'][0]:.3f}–{row['window'][1]:.3f})"
        return text

    rows = []
    for row in report.get("stages", []):
        status = str(row.get("status", ""))
        failing = status in ("FAIL", "FAILED", "ERROR", "ABORTED", "STOPPED")
        badge = "FAIL" if failing else "PASS" if status == "PASS" else "NA"
        note = f'<div class="note">{e(row.get("note"))}</div>' if row.get("note") else ""
        duration = f"{row['duration_s']} s" if row.get("duration_s") is not None else "—"
        rows.append(f'<tr class="{"bad" if failing else ""}"><td><b>{e(row.get("name"))}</b></td>'
                    f'<td class="num">{e(required(row))}</td><td class="num"><b>{e(measure(row))}</b>{note}</td>'
                    f'<td class="num r">{e(duration)}</td><td><span class="badge {badge}">{e(status)}</span></td></tr>')

    def kv(section, labels):
        data = report.get(section) or {}
        return "".join(f"<span>{e(label)}</span><b>{e(_show(data.get(key)))}</b>" for key, label in labels)

    calibration_kv = kv("calibration", CALIBRATION_LABELS)
    tests_kv = kv("tests", TEST_LABELS)
    identity = report.get("identity") or {}
    identity_text = " · ".join(part for part in (
        f"load {identity['load']}" if identity.get("load") else None,
        f"charger firmware {identity['charger_fw']}" if identity.get("charger_fw") else "charger firmware not captured",
        f"CAN adapter {identity['can_adapter']}" if identity.get("can_adapter") else None) if part)
    evidence = report.get("evidence") or {}
    attempt_text = ", ".join(str(item)[:12] for item in evidence.get("attempt_ids") or []) or "—"
    sha = variant.get("recipe_sha256") or "—"

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>EoL test report {e(serial)}</title><style>{_CSS}</style></head><body><div class="page">
<div class="card"><div class="top"><div><div class="brand">LEANWATTS · END-OF-LINE TEST REPORT</div>
<div class="serial">{e(serial)}</div><div class="sub">{e(wo_line)}</div></div>
<div class="verdict"><span class="badge {verdict_class} big">{e(verdict)}</span><div class="sub" style="margin-top:8px">Calibration <span class="badge {'PASS' if cal_verdict == 'PASS' else 'FAIL' if cal_verdict == 'FAIL' else 'NA'}">{e(cal_verdict)}</span> &nbsp;Test <span class="badge {'PASS' if test_verdict == 'PASS' else 'FAIL' if test_verdict == 'FAIL' else 'NA'}">{e(test_verdict)}</span></div></div></div>
{reason}<div class="flags">{flags}</div>
<div class="meta"><div><span>Tested by</span><b>{e(operator_text)}</b></div><div><span>Finished (IST · UTC)</span><b>{e(finished)}</b></div>
<div><span>Total time</span><b>{e(total)}</b></div><div><span>Station</span><b>{e(report.get('station'))}</b></div>
<div><span>Software</span><b>{e(software.get('name'))} {e(software.get('version'))}</b></div><div><span>Attempts for this serial</span><b>{attempts}</b></div></div></div>
<div class="card"><h2>Results by stage</h2><table><thead><tr><th>Stage</th><th>Required</th><th>Measured / result</th><th class="r">Time</th><th>Status</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<div class="two"><div class="card"><h2>Calibration</h2><div class="kv">{calibration_kv}</div></div><div class="card"><h2>Tests</h2><div class="kv">{tests_kv}</div></div></div>
<div class="card"><h2>Sign-off</h2><div class="sign"><div>Tested by · {e(operator_text)} · {e(finished)}</div><div>Reviewed by · name · date · disposition (release / rework / hold)</div></div></div>
<div class="card foot"><b>Traceability.</b> Report <code>{e(report.get('report_id'))}</code> · session <code>{e(evidence.get('session_id'))}</code> · attempts <code>{e(attempt_text)}</code> · recipe sha256 <code>{e(sha)}</code> · {e(identity_text)}.<br>Calibration PASS means the target was reached and the save request was accepted by the charger; retention after an AC power cycle is not verified. Test stages used an operator AC restart and sent no CAN commands.</div></div></body></html>"""
    if len(doc.encode("utf-8")) > MAX_BYTES:
        raise ValueError("report too large")
    return doc
