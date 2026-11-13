"""Data-only test programs; loading a file never executes code or opens hardware."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path


MAX_PROGRAM_BYTES = 128 * 1024
WORKFLOWS = {"full_cycle", "calibration_only", "test_only"}
START_METHODS = {"external_restart"}
DEFAULT_FILENAME = "04-ocv-current-fixed-cutoff.json"


def _object(value, allowed, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f"Unknown {label} field: {', '.join(sorted(unknown))}.")
    return value


def _number(value, low, high, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label} must be a finite number between {low:g} and {high:g}.")
    return float(value)


def _text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must contain 1–{maximum} characters.")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError(f"{label} contains control characters.")
    return value.strip()


def validate_program(value):
    """Return a fresh normalized program. Recipe/hardware limits remain separate."""
    data = _object(value, {"schema", "name", "description", "workflow", "baseline", "testing"}, "program")
    if type(data.get("schema")) is not int or data["schema"] != 1:
        raise ValueError("This app supports test-program schema 1.")
    workflow = data.get("workflow")
    if not isinstance(workflow, str) or workflow not in WORKFLOWS:
        raise ValueError("Choose workflow full_cycle, calibration_only or test_only.")
    baseline = _object(data.get("baseline", {}),
        {"engage_voltage", "ramp_timeout_seconds", "settle_seconds", "minimum_ramp_seconds"}, "baseline")
    raw_testing = data.get("testing", {})
    if isinstance(raw_testing, dict) and any(key in raw_testing for key in
            ("control_hold_seconds", "cutoff_dwell_seconds", "cutoff_step_a", "cutoff_observation_seconds")):
        raise ValueError("This earlier test procedure is retired in 1.1.11. Select 04-ocv-current-fixed-cutoff.json for settled OCV first and the fixed 5→0.5 A ramp. Your file is preserved.")
    testing = _object(raw_testing,
        {"start_method", "procedure", "restart_timeout_seconds"}, "testing")
    method = testing.get("start_method", "external_restart")
    if isinstance(method, str) and method in ("normal", "calibration_start_only"):
        raise ValueError("Earlier CAN-start test methods are retired. Select 04-ocv-current-fixed-cutoff.json. This file is preserved; tests send no charger commands or parameter changes.")
    if not isinstance(method, str) or method not in START_METHODS:
        raise ValueError("testing.start_method must be external_restart.")
    if testing.get("procedure") != "ocv_current_fixed_cutoff_v1":
        raise ValueError("Select 04-ocv-current-fixed-cutoff.json or set testing.procedure to ocv_current_fixed_cutoff_v1. Earlier test procedures are preserved but cannot run unchanged.")
    engage = baseline.get("engage_voltage")
    ramp = _number(baseline.get("ramp_timeout_seconds", 60), 15, 180, "Current ramp timeout")
    settle = _number(baseline.get("settle_seconds", 3), 1, 10, "Current settling time")
    minimum = _number(baseline.get("minimum_ramp_seconds", 18), 18, 60, "Minimum current ramp observation")
    if minimum + settle >= ramp:
        raise ValueError("Current ramp timeout must exceed minimum ramp observation plus settling time.")
    result = dict(schema=1, name=_text(data.get("name"), "Program name", 120),
        description=_text(data.get("description"), "Program description", 2000), workflow=workflow,
        baseline=dict(engage_voltage=None if engage is None else _number(engage, 0.1, 500, "Load engagement voltage"),
            ramp_timeout_seconds=ramp, settle_seconds=settle, minimum_ramp_seconds=minimum),
        testing=dict(start_method=method,
            procedure="ocv_current_fixed_cutoff_v1",
            restart_timeout_seconds=_number(testing.get("restart_timeout_seconds", 120), 30, 300, "Operator AC restart window")))
    return copy.deepcopy(result)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON field: {key}.")
        value[key] = item
    return value


def load_program(path):
    """Snapshot bytes, hash and validated settings together to avoid later file edits."""
    path = Path(path)
    if path.suffix.lower() != ".json" or not path.is_file():
        raise ValueError("Choose a .json test-program file.")
    if path.stat().st_size > MAX_PROGRAM_BYTES:
        raise ValueError("Test program exceeds 128 KB.")
    with path.open("rb") as handle:
        raw = handle.read(MAX_PROGRAM_BYTES + 1)
    if len(raw) > MAX_PROGRAM_BYTES:
        raise ValueError("Test program exceeds 128 KB.")
    try:
        source = raw.decode("utf-8")
        value = json.loads(source.removeprefix("\ufeff"), object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON numbers are not allowed.")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Program must contain valid UTF-8 JSON: " + str(exc)) from exc
    return dict(path=path.resolve(), name=path.name, program=validate_program(value), error="",
                sha256=hashlib.sha256(raw).hexdigest(), source=source)


def ensure_programs(folder):
    """Install missing examples only; never replace a station's edited program."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    bundled = Path(__file__).with_name("test_programs")
    for source in sorted(p for p in bundled.iterdir() if p.is_file() and p.suffix.lower() in {".json", ".txt"}):
        target = folder / source.name
        try:
            with target.open("xb") as handle:
                handle.write(source.read_bytes())
        except FileExistsError:
            pass
    return folder


def list_programs(folder):
    """Keep broken files visible with a useful error; never execute their contents."""
    folder = Path(folder)
    entries = []
    if not folder.is_dir():
        return entries
    for path in sorted((p for p in folder.iterdir() if p.suffix.lower() == ".json" and p.is_file()),
                       key=lambda item: (item.name.casefold(), item.name)):
        try:
            entries.append(load_program(path))
        except (OSError, ValueError, RecursionError) as exc:
            entries.append(dict(path=path.resolve(), name=path.name, program=None, error=str(exc), sha256=None, source=None))
    return entries
