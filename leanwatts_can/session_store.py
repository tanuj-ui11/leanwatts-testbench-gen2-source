"""Local operator credentials and durable production-session records. No instrument I/O."""
from __future__ import annotations

import copy
import base64
import csv
import hashlib
import hmac
import html
import io
import json
import math
import os
import secrets
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from .version import VERSION


LOCAL_ZONE = timezone(timedelta(hours=5, minutes=30), "IST")
TEST_STAGE_ALIASES = {
    "open-circuit voltage": "Open-circuit voltage test", "open circuit voltage": "Open-circuit voltage test",
    "open-circuit voltage test": "Open-circuit voltage test", "ocv": "Open-circuit voltage test",
    "cc current": "Full-current test", "full current": "Full-current test",
    "full current test": "Full-current test", "full-current test": "Full-current test",
    "normal output / end voltage": "End-voltage test", "end voltage": "End-voltage test",
    "end voltage at cc load": "End-voltage test", "end voltage test": "End-voltage test",
    "end-voltage test": "End-voltage test", "end current": "End-current cutoff test",
    "end current / observed shutdown": "End-current cutoff test",
    "end-current cutoff test": "End-current cutoff test",
}


def test_stage_name(value):
    return TEST_STAGE_ALIASES.get(str(value).lower(), str(value))


def is_test_stage(value):
    return str(value).lower() in TEST_STAGE_ALIASES or "test" in str(value).lower()


FAILURE_STATUSES = {"FAIL", "FAILED", "ERROR", "STOPPED", "ABORTED", "INTERRUPTED",
                    "INCOMPLETE", "INDETERMINATE", "NOT TESTED", "HOLD"}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def stage_failure_reason(item, recipe=None):
    """Explain failed evidence without promoting a last reading to a valid plateau."""
    if not isinstance(item, dict) or str(item.get("status", "")).upper() not in FAILURE_STATUSES:
        return ""
    recipe = recipe if isinstance(recipe, dict) else {}
    name = str(item.get("name", ""))
    canonical = test_stage_name(name)
    expected = {"Open-circuit voltage test": ("end_voltage", "voltage_tolerance", "V"),
                "End-voltage test": ("end_voltage", "voltage_tolerance", "V"),
                "Full-current test": ("cc_current", "current_tolerance", "A"),
                "End-current cutoff test": ("end_current", "end_current_tolerance", "A"),
                "Current calibration": ("cc_current", "current_tolerance", "A"),
                "Voltage calibration": ("end_voltage", "voltage_tolerance", "V")}.get(canonical)
    target = item.get("target", item.get("required"))
    tolerance = item.get("tolerance")
    unit = str(item.get("unit", ""))
    if expected:
        if not _finite(target):
            target = recipe.get(expected[0])
        if not _finite(tolerance):
            tolerance = recipe.get(expected[1])
        unit = unit or expected[2]
    pieces = []
    note = next((str(item[key]).strip() for key in ("detail", "note", "error") if item.get(key)), "")
    if note:
        pieces.append(note)
    if _finite(target):
        required = f"Required {target:g}"
        if _finite(tolerance):
            required += f" ±{tolerance:g}"
        pieces.append(required + (" " + unit if unit else "") + ".")
    interval = item.get("observed_interval_a")
    measured = item.get("measured", item.get("after"))
    if isinstance(interval, (list, tuple)) and len(interval) == 2 and all(_finite(v) for v in interval):
        pieces.append(f"Observed cutoff interval {interval[0]:g}–{interval[1]:g} A.")
    elif _finite(measured):
        pieces.append(f"Observed {measured:g}" + (" " + unit if unit else "") + ".")
    elif not note:
        pieces.append("No final measurement was recorded.")
    if not note and str(item.get("status", "")).upper() in ("STOPPED", "ABORTED"):
        pieces.insert(0, "The process was stopped before this check finished.")
    return " ".join(dict.fromkeys(pieces)) or "Failure reason was not recorded in this older result."


def failure_reason(record):
    """Human-readable reason for an unsuccessful attempt, including older records."""
    if not isinstance(record, dict) or str(record.get("outcome", "")).upper() not in FAILURE_STATUSES:
        return ""
    recipe = record.get("recipe") if isinstance(record.get("recipe"), dict) else {}
    reasons, seen = [], set()
    def add(name, reason):
        if reason:
            value = (str(name) + ": " if name else "") + str(reason).strip()
            if value not in seen:
                reasons.append(value)
                seen.add(value)
    represented = set()
    for key in ("calibration", "test"):
        part = record.get(key) if isinstance(record.get(key), dict) else {}
        before_count = len(reasons)
        for item in part.get("results", []) if isinstance(part.get("results"), list) else []:
            if isinstance(item, dict):
                reason = stage_failure_reason(item, recipe)
                if reason:
                    name = test_stage_name(item.get("name", key.title()))
                    add(name, reason)
                    represented.add(name)
        if str(part.get("outcome", "")).upper() in FAILURE_STATUSES and len(reasons) == before_count:
            stage = next((item for item in reversed(part.get("stage_details", []))
                          if isinstance(item, dict) and str(item.get("status", "")).upper() in FAILURE_STATUSES), None)
            detail = part.get("detail") or part.get("error")
            if not detail:
                detail = next((str(event).split("ERROR:", 1)[-1].strip() for event in reversed(part.get("events", []))
                               if "ERROR:" in str(event)), "")
            if stage:
                item = dict(stage)
                if not (item.get("detail") or item.get("note") or item.get("error")):
                    item["detail"] = detail
                name = test_stage_name(item.get("name", key.title()))
                add(name, stage_failure_reason(item, recipe))
                represented.add(name)
            else:
                add("Testing" if key == "test" else "Calibration", detail)
    for item in record.get("stage_details", []) if isinstance(record.get("stage_details"), list) else []:
        if isinstance(item, dict):
            name = test_stage_name(item.get("name", "Stage"))
            if name not in represented:
                add(name, stage_failure_reason(item, recipe))
    if not reasons:
        add("", record.get("failure_reason") or record.get("detail") or record.get("error"))
    if not reasons:
        outcome = str(record.get("outcome", "")).upper()
        add("", "The application closed before this attempt finished." if outcome == "INTERRUPTED" else
            "Stopped by the operator before all selected checks finished." if outcome in ("STOPPED", "ABORTED") else
            "Failure reason was not recorded in this older result.")
    return "\n".join(reasons)


def now_text():
    return datetime.now(LOCAL_ZONE).isoformat(timespec="seconds")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class OperatorStore:
    ITERATIONS = 600_000

    def __init__(self, data_dir):
        self.path = Path(data_dir) / "operators.json"
        self._lock = threading.RLock()
        self._read()

    def _read(self):
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema") != 1 or not isinstance(data.get("operators"), list):
            raise ValueError("Operator settings are unreadable; the original file was preserved.")
        return data["operators"]

    @staticmethod
    def _public(record):
        return {key: record[key] for key in ("id", "name", "role", "enabled")}

    def list_operators(self):
        with self._lock:
            return [self._public(item) for item in self._read()]

    def has_accounts(self):
        return bool(self.list_operators())

    def authenticate(self, operator_id, password):
        with self._lock:
            return self._public(self._authenticate_record(self._read(), operator_id, password))

    @staticmethod
    def _authenticate_record(records, operator_id, password):
        record = next((item for item in records if item["id"] == str(operator_id).strip()), None)
        if record is None or not record.get("enabled"):
            raise ValueError("ID or password is incorrect, or this operator is disabled.")
        derived = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"),
            bytes.fromhex(record["salt"]), record["iterations"]).hex()
        if not hmac.compare_digest(derived, record["password_hash"]):
            raise ValueError("ID or password is incorrect, or this operator is disabled.")
        return record

    def _set_password(self, record, password):
        if not isinstance(password, str) or len(password) < 4:
            raise ValueError("Use a password with at least four characters.")
        salt = secrets.token_bytes(16)
        record.update(salt=salt.hex(), iterations=self.ITERATIONS,
            password_hash=hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, self.ITERATIONS).hex())

    @staticmethod
    def _password_self_managed(record):
        # Existing lead records predate this flag; role alone must protect them.
        return record.get("role") == "lead" or record.get("self_managed_password") is True

    def reset_operator_password(self, actor_id, actor_password, target_id, new_password):
        """Authenticated leads/admins may change only an operator's password."""
        with self._lock:
            records = self._read()
            actor = self._authenticate_record(records, actor_id, actor_password)
            if actor["role"] not in ("admin", "lead"):
                raise ValueError("A production lead or administrator must approve this password reset.")
            target = next((item for item in records if item["id"] == str(target_id).strip()), None)
            if target is None or target["role"] != "operator":
                raise ValueError("Password resets are only available for operator accounts.")
            if self._password_self_managed(target):
                raise ValueError("This account requires its current password. Use Change own password.")
            self._set_password(target, new_password)
            atomic_json(self.path, {"schema": 1, "operators": records})
            return self._public(target)

    def change_own_password(self, operator_id, current_password, new_password):
        """Changing one's password always requires the account's current password."""
        with self._lock:
            records = self._read()
            record = self._authenticate_record(records, operator_id, current_password)
            self._set_password(record, new_password)
            if self._password_self_managed(record):
                record["self_managed_password"] = True
            atomic_json(self.path, {"schema": 1, "operators": records})
            return self._public(record)

    def upsert_operator(self, operator_id, name, password, enabled=True, role="operator"):
        operator_id, name = str(operator_id).strip(), str(name).strip()
        if not operator_id or len(operator_id) > 80 or not name or len(name) > 120:
            raise ValueError("Enter an operator ID and name.")
        if role not in ("admin", "lead", "operator") or type(enabled) is not bool:
            raise ValueError("Choose an operator, production lead or administrator role.")
        with self._lock:
            records = self._read()
            index = next((n for n, item in enumerate(records) if item["id"] == operator_id), -1)
            if not records and (role != "admin" or not enabled):
                raise ValueError("The first account must be an enabled administrator.")
            record = copy.deepcopy(records[index]) if index >= 0 else {}
            if password:
                if index >= 0 and self._password_self_managed(record):
                    raise ValueError("A production lead must use Change own password and enter their current password. Leave this password field blank to keep it.")
                self._set_password(record, str(password))
            elif index < 0:
                raise ValueError("Enter a password for the new operator.")
            if role == "lead" or self._password_self_managed(record):
                record["self_managed_password"] = True
            record.update(id=operator_id, name=name, enabled=enabled, role=role)
            if index < 0:
                records.append(record)
            else:
                records[index] = record
            if not any(item["role"] == "admin" and item["enabled"] for item in records):
                raise ValueError("Keep at least one enabled administrator.")
            atomic_json(self.path, {"schema": 1, "operators": records})
            return self._public(record)

    def delete_operator(self, operator_id):
        with self._lock:
            records = [item for item in self._read() if item["id"] != operator_id]
            if not any(item["role"] == "admin" and item["enabled"] for item in records):
                raise ValueError("The last enabled administrator cannot be deleted.")
            atomic_json(self.path, {"schema": 1, "operators": records})


class SessionStore:
    """One aggregate report per session; attempts are durable before hardware starts."""
    MODES = {"calibration_only": "Parameters + Calibration only", "test_only": "Test only",
             "independent": "Both independently", "both_required": "Both mandatorily"}
    def __init__(self, data_dir):
        self.folder = Path(data_dir) / "sessions"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.pointer = self.folder / "active.json"
        self._lock = threading.RLock()
        self._active = None
        if self.pointer.exists():
            pointer = json.loads(self.pointer.read_text(encoding="utf-8"))
            self._active = self._load(pointer["session_id"])
            if self._active.get("status") != "OPEN":
                self._active = None
            elif any(unit.get("outcome") == "RUNNING" for unit in self._active["units"]):
                for unit in self._active["units"]:
                    if unit.get("outcome") == "RUNNING":
                        unit.update(outcome="INTERRUPTED", calibration_result="INCOMPLETE",
                            test_result="NOT RUN", finished_at=now_text(),
                            detail="Application closed before this attempt finished. No PASS was recorded.")
                self._save()

    def _path(self, session_id, suffix="json"):
        if not isinstance(session_id, str) or not session_id or any(c not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_" for c in session_id):
            raise ValueError("Invalid session identifier.")
        return self.folder / (session_id + "." + suffix)

    def _load(self, session_id):
        data = json.loads(self._path(session_id).read_text(encoding="utf-8"))
        if data.get("schema") != "leanwatts-session/1" or data.get("session_id") != session_id:
            raise ValueError("Invalid session record; original data was retained.")
        # Existing sessions retain their original independent workflow.
        data.setdefault("session_mode", "independent")
        if data["session_mode"] not in self.MODES:
            raise ValueError("Unknown session mode; the original session was preserved.")
        data["unit_summaries"] = self.summarize(data)
        return data

    def active(self):
        with self._lock:
            return copy.deepcopy(self._active)

    def start(self, operator, mode="independent"):
        with self._lock:
            if self._active is not None:
                raise ValueError("Close or resume the current session first.")
            if not isinstance(operator, dict) or not operator.get("id") or not operator.get("enabled"):
                raise ValueError("Log in with an enabled operator ID.")
            if mode not in self.MODES:
                raise ValueError("Choose one of the four session workflows.")
            identity = {key: operator.get(key) for key in ("id", "name", "role", "enabled")}
            session_id = datetime.now(LOCAL_ZONE).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
            self._active = dict(schema="leanwatts-session/1", session_id=session_id,
                application="Leanwatts Testbench Gen2", application_version=VERSION,
                operator=identity, started_at=now_text(), closed_at=None, status="OPEN", units=[],
                timezone="Asia/Kolkata", session_mode=mode, report_paths=self.report_paths(session_id))
            self._save()
            atomic_json(self.pointer, {"session_id": session_id})
            return self.active()

    @staticmethod
    def _unit_key(serial, recipe, revision):
        payload = json.dumps([str(serial).strip(), revision, recipe], sort_keys=True,
                             ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def summarize(cls, data, *, evidence=False):
        """Combine attempts for the same serial and frozen recipe; retain every audit attempt."""
        groups = {}
        for attempt in data.get("units", []):
            key = cls._unit_key(attempt.get("serial", ""), attempt.get("recipe", {}),
                                attempt.get("recipe_revision"))
            groups.setdefault(key, []).append(attempt)
        summaries = []
        both = data.get("session_mode", "independent") == "both_required"
        for key, attempts in groups.items():
            first, last = attempts[0], attempts[-1]
            cal_index = next((i for i in range(len(attempts) - 1, -1, -1)
                              if attempts[i].get("run_calibration", True)), None)
            cal = attempts[cal_index] if cal_index is not None else None
            # Starting calibration again invalidates tests of the previous calibration.
            tests = [item for i, item in enumerate(attempts)
                     if item.get("run_tests", True) and (cal_index is None or i >= cal_index)]
            test = tests[-1] if tests else None
            old_test = not tests and any(item.get("run_tests", True) for item in attempts)
            cal_ok = bool(cal and cal.get("calibration_result") == "PASS"
                          and cal.get("outcome") in ("CALIBRATED", "PASS")
                          and cal.get("simulation") is False)
            test_ok = bool(test and test.get("test_result") == "PASS"
                           and test.get("outcome") == "PASS" and test.get("simulation") is False)
            running = last.get("outcome") == "RUNNING"
            complete = cal_ok and test_ok if both else (cal_ok or cal is None) and (test_ok or test is None)
            complete = bool(complete and not running and (cal or test))
            if running:
                outcome = "RUNNING"
            elif both:
                outcome = "PASS" if complete else "INCOMPLETE"
            elif last.get("simulation"):
                outcome = "SIMULATION"
            elif complete:
                outcome = "PASS" if test_ok else "CALIBRATED"
            else:
                outcome = last.get("outcome", "INCOMPLETE")
                if outcome in ("PASS", "CALIBRATED"):
                    outcome = "INCOMPLETE"
            if both and not complete:
                next_step = "Parameters + Calibration" if not cal_ok else "Test"
                detail = f"{next_step} required for this charger before the next unit."
                if last.get("detail"):
                    detail += " " + str(last["detail"])
            else:
                next_step, detail = "", str(last.get("detail", ""))
            failures = [str(item.get("outcome")) for item in attempts
                        if item.get("outcome") in ("FAIL", "ERROR", "STOPPED", "ABORTED", "INTERRUPTED")]
            if failures:
                detail += " Attempt history: " + ", ".join(failures) + "."
            summary = {field: copy.deepcopy(first.get(field)) for field in
                       ("serial", "serial_override", "recipe", "recipe_name", "recipe_revision", "started_at")}
            summary.update(group_id=key, sequence=len(summaries) + 1,
                finished_at=last.get("finished_at"), attempt_ids=[a.get("attempt_id") for a in attempts],
                attempt_count=len(attempts), calibration_attempt_id=cal.get("attempt_id") if cal else None,
                test_attempt_id=test.get("attempt_id") if test else None,
                calibration_completed=cal_ok, test_completed=test_ok,
                calibration_result=cal.get("calibration_result", "NOT RUN") if cal else "NOT RUN",
                test_result=test.get("test_result", "NOT RUN") if test else "INVALIDATED" if old_test else "NOT RUN",
                outcome=outcome, last_attempt_outcome=last.get("outcome"), completion_state="COMPLETE" if complete else "INCOMPLETE",
                next_step=next_step, detail=detail, run_calibration=cal is not None, run_tests=test is not None,
                workflow_mode="separate_processes" if cal and test else "parameters_calibration" if cal else "test_only",
                test_skipped=test is None, simulation=last.get("simulation", False))
            summary["failure_reason"] = ""
            if outcome in FAILURE_STATUSES:
                causes = list(dict.fromkeys(reason for attempt in (cal, test) if attempt
                                             for reason in [failure_reason(attempt)] if reason))
                summary["failure_reason"] = "\n".join(causes) or (
                    "Calibration was rerun; Test must be repeated." if old_test else
                    f"{next_step} has not been completed for this charger." if next_step else failure_reason(last))
            if evidence:
                if cal:
                    for field in ("calibration", "preflight_result", "baseline_result", "starting_point", "persistence_verified"):
                        if field in cal:
                            summary[field] = copy.deepcopy(cal[field])
                if test:
                    summary["test"] = copy.deepcopy(test.get("test"))
                summary["stage_details"] = []
                if cal:
                    summary["stage_details"].extend(copy.deepcopy(item) for item in cal.get("stage_details", [])
                        if not is_test_stage(item.get("name", "")))
                if test:
                    actual_names = {item.get("name") for item in (test.get("test") or {}).get("results", [])}
                    summary["stage_details"].extend(copy.deepcopy(item) for item in test.get("stage_details", [])
                        if is_test_stage(item.get("name", "")) or item.get("name") in actual_names)
            summaries.append(summary)
        return summaries

    def pending_unit(self):
        with self._lock:
            if not self._active or self._active.get("session_mode") != "both_required":
                return None
            return next((item for item in self.summarize(self._active)
                         if item["completion_state"] != "COMPLETE"), None)

    def readiness(self, serial, recipe, revision, *, run_calibration, run_tests):
        """The store enforces the same pairing guard as the UI, including after restart."""
        with self._lock:
            reason = self.process_readiness(run_calibration=run_calibration, run_tests=run_tests)
            if reason:
                return reason
            if self._active.get("session_mode") != "both_required":
                return ""
            if run_calibration and run_tests:
                return "Choose Parameters + Calibration or Test separately."
            recipe_data = recipe.to_dict() if hasattr(recipe, "to_dict") else recipe
            key = self._unit_key(serial, recipe_data, revision)
            pending = self.pending_unit()
            if pending and key != pending["group_id"]:
                return f"Finish {pending['next_step']} for {pending['serial']} with its frozen variant, or close this session as incomplete."
            existing = next((item for item in self.summarize(self._active) if item["group_id"] == key), None)
            if run_tests and (not existing or existing["calibration_result"] != "PASS"
                              or existing.get("next_step") == "Parameters + Calibration"
                              or existing.get("simulation")):
                return "Both mandatorily: complete Parameters + Calibration for this charger before Test."
            return ""

    def process_readiness(self, *, run_calibration, run_tests):
        """Session mode is enforced even when a caller bypasses the buttons."""
        with self._lock:
            if not self._active:
                return "Start an operator session first."
            mode = self._active.get("session_mode", "independent")
            if mode == "calibration_only" and run_tests:
                return "This session is Parameters + Calibration only. Start a new session to run Test."
            if mode == "test_only" and run_calibration:
                return "This session is Test only. Start a new session to run calibration."
            return ""

    def begin_unit(self, serial, recipe, revision, *, run_tests=True, run_calibration=True, serial_override=False):
        from .serial_numbers import validate_serial
        error = validate_serial(str(serial), allow_custom=serial_override)
        if error:
            raise ValueError(error)
        if type(run_tests) is not bool or type(run_calibration) is not bool or not (run_tests or run_calibration):
            raise ValueError("Choose calibration, testing, or calibration and testing.")
        with self._lock:
            if self._active is None:
                raise ValueError("Start an operator session first.")
            if any(unit.get("outcome") == "RUNNING" for unit in self._active["units"]):
                raise ValueError("Finish or stop the current charger first.")
            serial = str(serial).strip()
            if not serial or len(serial) > 160:
                raise ValueError("Scan or enter the charger serial number.")
            recipe_data = recipe.to_dict() if hasattr(recipe, "to_dict") else copy.deepcopy(recipe)
            reason = self.readiness(serial, recipe_data, revision,
                                    run_calibration=run_calibration, run_tests=run_tests)
            if reason:
                raise ValueError(reason)
            attempt_id = uuid.uuid4().hex
            self._active["units"].append(dict(attempt_id=attempt_id,
                sequence=len(self._active["units"]) + 1, serial=serial, serial_override=serial_override,
                started_at=now_text(), finished_at=None, recipe_name=recipe_data.get("name", ""),
                recipe_revision=revision, recipe=recipe_data, calibration_result="RUNNING" if run_calibration else "NOT RUN",
                test_result="NOT RUN", outcome="RUNNING", stage_details=[], run_tests=run_tests,
                run_calibration=run_calibration,
                workflow_mode="test_only" if not run_calibration else "parameters_calibration_test" if run_tests else "parameters_calibration",
                test_skipped=not run_tests))
            self._save()
            return attempt_id

    def finish_unit(self, attempt_id, result):
        with self._lock:
            if self._active is None:
                raise ValueError("No session is open.")
            unit = next((item for item in self._active["units"] if item["attempt_id"] == attempt_id), None)
            if unit is None or unit.get("outcome") != "RUNNING":
                raise ValueError("This attempt is missing or already recorded.")
            incoming = copy.deepcopy(result)
            if incoming.get("is_running") or incoming.get("outcome") == "RUNNING":
                raise ValueError("An unfinished process cannot be stored as a completed result.")
            calibrating = unit.get("run_calibration", True)
            if incoming.get("outcome") == "PASS" and (incoming.get("calibration_result") != ("PASS" if calibrating else "NOT RUN")
                    or incoming.get("test_result") != "PASS" or incoming.get("simulation")
                    or not unit.get("run_tests", True)
                    or (not calibrating and incoming.get("calibration") is not None)):
                raise ValueError("PASS requires all selected physical stages; test-only results must not claim calibration.")
            if incoming.get("outcome") == "CALIBRATED" and (incoming.get("calibration_result") != "PASS"
                    or incoming.get("test_result") != "NOT RUN" or incoming.get("simulation")
                    or not calibrating or unit.get("run_tests", True) or incoming.get("test") is not None):
                raise ValueError("CALIBRATED requires completed calibration with post-tests not selected or run.")
            for key in ("run_calibration", "run_tests", "workflow_mode", "test_skipped", "serial_override"):
                if key in incoming and key in unit and incoming[key] != unit[key]:
                    raise ValueError("The recorded workflow cannot change after starting a charger.")
            protected = {key: unit[key] for key in ("attempt_id", "sequence", "serial", "started_at", "recipe", "recipe_name", "recipe_revision")}
            unit.update(incoming)
            unit.update(protected)
            unit["finished_at"] = incoming.get("finished_at") or now_text()
            unit["failure_reason"] = failure_reason(unit)
            self._save()
            return copy.deepcopy(unit)

    def close(self):
        with self._lock:
            if self._active is None:
                raise ValueError("No session is open.")
            if any(unit.get("outcome") == "RUNNING" for unit in self._active["units"]):
                raise ValueError("Stop or finish the active charger before closing the session.")
            self._active.update(status="CLOSED", closed_at=now_text())
            self._save()
            result = self.active()
            self._active = None
            self.pointer.unlink(missing_ok=True)
            return result

    def list_sessions(self):
        with self._lock:
            result = []
            for path in sorted(self.folder.glob("*.json"), reverse=True):
                if path.name == "active.json":
                    continue
                try:
                    data = self._load(path.stem)
                    result.append(data)
                except (ValueError, KeyError, OSError):
                    continue
            return result

    def report_paths(self, session_id):
        paths = {suffix: str(self._path(session_id, suffix).resolve()) for suffix in ("json", "csv", "html")}
        paths["detail_html"] = str(self._path(session_id, "detail.html").resolve())
        return paths

    def _save(self):
        data = self._active
        data["unit_summaries"] = self.summarize(data)
        data["completion_state"] = ("INCOMPLETE" if data.get("session_mode") == "both_required"
            and any(unit["completion_state"] != "COMPLETE" for unit in data["unit_summaries"]) else "COMPLETE")
        atomic_json(self._path(data["session_id"]), data)
        # JSON is the durable source of truth. Derived readable reports are
        # refreshed after each charger and again when the session closes.
        self._write_reports(data)

    def _write_reports(self, data):
        """One charger per row; full acquisition evidence remains in the JSON."""
        from .session_seed import starting_point_text
        summary_units = self.summarize(data, evidence=True)

        def mapping(value):
            return value if isinstance(value, dict) else {}

        def rows(value):
            return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

        def number(value, unit=""):
            try:
                valid = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                valid = False
            if not valid:
                return "—"
            return f"{value:.4g}" + (" " + unit if unit else "")

        def local_time(value):
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(LOCAL_ZONE).strftime("%d %b %Y %H:%M:%S")
            except (ValueError, TypeError):
                return str(value or "—")

        def stage(unit, name):
            return next((item for item in rows(unit.get("stage_details")) if item.get("name") == name), {})

        def parameter_changes(unit):
            if unit.get("run_calibration") is False or unit.get("workflow_mode") == "test_only":
                return "NOT RUN"
            preflight = mapping(unit.get("preflight_result")) or mapping(mapping(unit.get("calibration")).get("preflight_result"))
            if not preflight:
                return "NOT RUN"
            before, after = mapping(preflight.get("before")), mapping(preflight.get("after"))
            desired = mapping(preflight.get("desired"))
            updated = preflight.get("updated_fields", [])
            updated = updated if isinstance(updated, list) else []
            keys = list(dict.fromkeys(list(before) + list(after) + updated))
            labels = {"ccCurrent": "CC current (A)", "endVoltage": "End voltage (V)",
                "endCurrent": "End current (A)", "batteryType": "Battery type",
                "ratedVoltage": "Rated voltage (V)", "startVoltage": "Start voltage (V)",
                "temperatureLimit": "Temperature limit (°C)", "maxChargeMinutes": "Charge time (min)",
                "derating110": "110 V derating (%)", "derating220": "220 V derating (%)",
                "blindCharge": "Blind charge", "rechargeVoltage": "Recharge voltage (V)",
                "adjustCurrentMin": "Adjustable current lower (A)", "adjustCurrentMax": "Adjustable current (A)",
                "adjustVoltageMin": "Adjustable voltage lower (V)", "adjustVoltageMax": "Adjustable voltage (V)"}
            changes = []
            for key in keys:
                if key in before and key in after and before[key] == after[key]:
                    continue
                if key not in updated and (key not in before or key not in after):
                    continue
                old = number(before[key]) if key in before else "Not returned"
                new = number(after[key]) if key in after else "Unconfirmed"
                if key not in after and key in desired:
                    new += " (requested " + number(desired[key]) + ")"
                changes.append(labels.get(key, key) + ": " + old + " → " + new)
            status = str(preflight.get("status", "UNCONFIRMED"))
            if changes:
                return "\n".join(changes) + ("\n" + status if status != "PASS" else "")
            return "Checked; unchanged" if status == "PASS" else status + "; update unconfirmed"

        def calibration_cell(unit, axis):
            if unit.get("run_calibration") is False or unit.get("workflow_mode") == "test_only":
                return "NOT RUN"
            record = mapping(unit.get("calibration"))
            baseline = mapping(unit.get("baseline_result")) or mapping(record.get("baseline_result"))
            point = mapping(baseline.get("current" if axis == "current" else "open_voltage"))
            name = axis.title() + " calibration"
            detail = stage(unit, name)
            result = next((item for item in rows(record.get("results")) if item.get("name") == name), {})
            final = result.get("measured", detail.get("after"))
            status = str(result.get("status", detail.get("status", "NOT RUN")))
            if status == "SAVE REQUEST ACCEPTED":
                status = "PASS"
            measure_unit = "A" if axis == "current" else "V"
            if point.get("status") == "PASS":
                old = number(point.get("measured"), measure_unit)
                origin = "baseline" if axis == "current" else "baseline OCV"
            elif baseline:
                old, origin = "Unavailable", "baseline"
            elif detail.get("before") is not None:
                old, origin = number(detail.get("before"), measure_unit), "first trimmed reading"
            else:
                old, origin = "—", "baseline unavailable"
            recipe = mapping(unit.get("recipe"))
            if axis == "current":
                old_load = point.get("load_setpoint", point.get("setpoint", recipe.get("cc_test_load_voltage", recipe.get("current_cal_load_voltage"))))
                new_load = recipe.get("current_cal_load_voltage")
                conditions = "CV " + number(old_load, "V") + " → CV " + number(new_load, "V")
            else:
                conditions = "load OFF → CC " + number(recipe.get("voltage_cal_load_current"), "A")
            duration = result.get("duration_seconds", detail.get("duration_seconds"))
            text = old + " → " + number(final, measure_unit)
            return text + "\n" + origin + "; " + conditions + "\n" + status + " · " + number(duration, "s")

        def test_name(value):
            return test_stage_name(value)

        tests = ["Open-circuit voltage test", "Full-current test", "End-current cutoff test"]
        for unit in data["units"]:
            for item in rows(mapping(unit.get("test")).get("results")):
                name = test_name(item.get("name", ""))
                if name and name not in tests:
                    tests.append(name)

        def test_cell(unit, name):
            items = rows(mapping(unit.get("test")).get("results"))
            details = rows(unit.get("stage_details"))
            item = next((item for item in items if test_name(item.get("name")) == name), {})
            detail = next((item for item in details if test_name(item.get("name")) == name), {})
            missing_status = "INVALIDATED" if unit.get("test_result") == "INVALIDATED" else "NOT RUN"
            status = str(item.get("status", detail.get("status", missing_status)))
            value = item.get("measured", detail.get("after"))
            duration = item.get("duration_seconds", detail.get("duration_seconds"))
            text = status + " · " + number(duration, "s")
            if value is not None:
                text += "\n" + number(value, item.get("unit", detail.get("unit", "")))
            evidence = dict(detail, **item)
            if str(evidence.get("status", "")).upper() in FAILURE_STATUSES and not any(evidence.get(key) for key in ("detail", "note", "error")):
                test_record = mapping(unit.get("test"))
                evidence["detail"] = test_record.get("detail") or test_record.get("error", "")
            reason = stage_failure_reason(evidence, unit.get("recipe"))
            if reason:
                text += "\n" + reason
            return text

        headers = ["S. No.", "Time (IST)", "Charger serial / variant", "Parameters changed (old → new)",
            "Current calibration (old → new)", "Voltage calibration (old → new)", *tests, "Overall", "Failure reason"]
        csv_buffer = io.StringIO(newline="")
        writer = csv.writer(csv_buffer)
        writer.writerow(["Session", "Operator ID", "Operator", *headers, "Starting trims"])
        def csv_cell(value):
            text = str(value if value is not None else "")
            return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text
        esc = lambda value: html.escape(str(value if value is not None else ""))
        table_rows = []
        for unit in summary_units:
            values = [unit["sequence"], local_time(unit.get("started_at")),
                str(unit.get("serial", "")) + ("\nCUSTOM NUMBER" if unit.get("serial_override") else "")
                + "\n" + str(unit.get("recipe_name", "")) + " · r" + str(unit.get("recipe_revision", ""))
                + (f"\n{unit['attempt_count']} attempts" if unit["attempt_count"] > 1 else ""),
                parameter_changes(unit), calibration_cell(unit, "current"), calibration_cell(unit, "voltage"),
                *[test_cell(unit, name) for name in tests], unit.get("outcome", "NOT RUN"), unit.get("failure_reason") or "—"]
            starting = starting_point_text(unit.get("starting_point"))
            writer.writerow([csv_cell(value) for value in [data["session_id"], data["operator"]["id"], data["operator"]["name"], *values, starting]])
            rendered = [esc(value).replace("\n", "<br>") for value in values]
            if starting:
                rendered[2] += "<br><small>" + esc(starting) + "</small>"
            if unit.get("detail") and unit.get("outcome") not in ("PASS", "CALIBRATED"):
                rendered[-2] += "<br><small>" + esc(unit["detail"]) + "</small>"
            table_rows.append("<tr>" + "".join("<td>" + value + "</td>" for value in rendered) + "</tr>")
        record_version = esc(data.get("application_version", "1.0"))
        logo_path = Path(__file__).resolve().parent / "assets" / "leanwatts-logo-light.png"
        logo = ("<img alt='Leanwatts' height='36' src='data:image/png;base64,"
                + base64.b64encode(logo_path.read_bytes()).decode("ascii") + "'>") if logo_path.exists() else ""
        headings = "".join("<th>" + esc(title) + "</th>" for title in headers)
        detailed_name = self._path(data["session_id"], "detail.html").name
        page = f"""<!doctype html><html><meta charset="utf-8"><title>Leanwatts Testbench Gen2 {record_version} · Session {esc(data['session_id'])}</title>
<style>body{{font:12px Arial;color:#172b35;margin:22px}}h1{{font-size:22px;margin:0}}table{{width:100%;border-collapse:collapse;table-layout:fixed}}th,td{{text-align:left;vertical-align:top;padding:7px;border:1px solid #dbe3e5;overflow-wrap:anywhere}}th{{background:#edf4f3;font-size:11px}}th:first-child{{width:3%}}th:nth-child(4){{width:13%}}th:last-child{{width:20%}}small{{color:#607078;font-size:10px}}p{{line-height:1.5}}@media print{{@page{{size:A4 landscape;margin:10mm}}body{{margin:0;font-size:10px}}button{{display:none}}thead{{display:table-header-group}}tr{{break-inside:avoid}}th,td{{padding:5px}}}}</style>
<button onclick="window.print()">Print / Save PDF</button> <a href="{esc(detailed_name)}">Open detailed report</a><div style="display:flex;align-items:center;gap:20px;margin-top:16px">{logo}<h1>Session report · Gen2 {record_version}</h1></div>
<p>Session: {esc(data['session_id'])} · {esc(data['status'])} · {esc(self.MODES.get(data.get('session_mode'), 'Both independently'))} · {esc(data.get('completion_state', ''))} · Operator: {esc(data['operator']['name'])} ({esc(data['operator']['id'])})<br>Started: {esc(local_time(data['started_at']))} IST · Closed: {esc(local_time(data.get('closed_at')))} IST</p>
<table><thead><tr>{headings}</tr></thead><tbody>{''.join(table_rows)}</tbody></table>
<p><small>One summary row combines attempts for the same charger and recipe revision. Starting calibration again invalidates earlier tests; every attempt remains in the detailed report. Both mandatorily requires successful calibration followed by successful testing before the next charger. Test-only PASS covers tests only; CALIBRATED is not a test PASS. Open-circuit voltage is measured with the load OFF; earlier loaded end-voltage results remain separate. Old values are pre-calibration baselines where available; measurement conditions are shown in each cell. Calibration PASS means the target was reached and save request accepted; persistence after restart is unverified. Starting trims are recorded commands, not charger-memory readbacks. See the detailed report for stage evidence and activity.</small></p></html>"""
        details = []
        for unit in data["units"]:
            stage_rows = []
            for item in rows(unit.get("stage_details")):
                display = dict(item)
                display["detail"] = stage_failure_reason(item, unit.get("recipe")) or item.get("detail") or item.get("note", "")
                stage_rows.append("<tr>" + "".join("<td>" + esc(display.get(key)) + "</td>" for key in
                    ("name", "before", "after", "unit", "duration_seconds", "status", "detail")) + "</tr>")
            preflight = mapping(unit.get("preflight_result")) or mapping(mapping(unit.get("calibration")).get("preflight_result"))
            parameter_rows = []
            before, after, desired = (mapping(preflight.get(key)) for key in ("before", "after", "desired"))
            for key in dict.fromkeys(list(before) + list(desired) + list(after)):
                parameter_rows.append("<tr>" + "".join("<td>" + esc(value) + "</td>" for value in
                    (key, before.get(key, "Not returned"), desired.get(key, "Not specified"), after.get(key, "Not returned"))) + "</tr>")
            activity = []
            for key, title in (("calibration", "Calibration"), ("test", "Tests")):
                record = mapping(unit.get(key))
                events = record.get("events")
                if isinstance(events, list) and events:
                    activity.append("<h3>" + title + " activity</h3><pre>" + esc("\n".join(str(item) for item in events)) + "</pre>")
            provenance = {key: unit.get(key) for key in ("workflow_mode", "starting_point", "baseline_result", "persistence_verified") if key in unit}
            custom_label = " (custom number)" if unit.get("serial_override") else ""
            details.append(f"<section><h2>#{esc(unit['sequence'])} · {esc(unit.get('serial'))}{custom_label} · {esc(unit.get('outcome'))}</h2>"
                + "<p>" + esc(local_time(unit.get("started_at"))) + " IST · " + esc(unit.get("recipe_name")) + "<br>" + esc(unit.get("detail", "")) + "</p>"
                + ("<p><strong>Failure reason:</strong> " + esc(failure_reason(unit)).replace("\n", "<br>") + "</p>" if failure_reason(unit) else "")
                + "<table><thead><tr><th>Stage</th><th>Before</th><th>Final</th><th>Unit</th><th>Seconds</th><th>Result</th><th>Detail</th></tr></thead><tbody>"
                + "".join(stage_rows) + "</tbody></table>"
                + ("<h3>Charger parameter readback</h3><table><tr><th>Parameter</th><th>Before</th><th>Required</th><th>After</th></tr>" + "".join(parameter_rows) + "</table>" if parameter_rows else "")
                + "<h3>Baseline and starting trims</h3><pre>" + esc(json.dumps(provenance, ensure_ascii=False, indent=2)) + "</pre>"
                + "".join(activity) + "</section>")
        detailed = f"""<!doctype html><html><meta charset="utf-8"><title>Detailed session report · {esc(data['session_id'])}</title>
<style>body{{font:13px Arial;color:#172b35;margin:26px}}h1{{font-size:23px}}h2{{font-size:18px}}h3{{font-size:14px}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;vertical-align:top;padding:7px;border:1px solid #dbe3e5}}th{{background:#edf4f3}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f7;padding:12px;font-size:11px}}section{{margin-top:28px}}@media print{{button{{display:none}}tr{{break-inside:avoid}}}}</style>
<button onclick="window.print()">Print / Save PDF</button> <a href="{esc(self._path(data['session_id'], 'html').name)}">Back to summary report</a>
<h1>Detailed session report · Gen2 {record_version}</h1><p>{esc(data['session_id'])} · {esc(data['status'])}<br>Operator: {esc(data['operator']['name'])} ({esc(data['operator']['id'])})</p>
{''.join(details)}<p>Calibration save requests do not verify persistence after restart. Baseline and starting commands are recorded separately from calibration measurements. The JSON file remains the complete audit record including acquisitions and CAN traces.</p></html>"""
        for suffix, content in (("csv", csv_buffer.getvalue()), ("html", page), ("detail.html", detailed)):
            path = self._path(data["session_id"], suffix)
            temp = path.with_suffix(path.suffix + ".tmp")
            temp.write_text(content, encoding="utf-8-sig" if suffix == "csv" else "utf-8", newline="")
            os.replace(temp, path)
