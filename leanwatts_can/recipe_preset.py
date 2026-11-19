"""Read a recipe preset from a file chosen by the operator.

Accepted files (UTF-8, at most 256 KB):
  * JSON: one recipe object, or ``{"recipe": {...}}`` as stored in profiles.json.
  * CSV / TXT: two columns, ``field,value`` (one per line; ``#`` starts a comment).
    Charger settings and cutoff-test settings use their own names, e.g. ``ratedVoltage``,
    ``start_current``. The recipe name is ``name``.
The result is only a draft for the recipe editor. Nothing is saved or sent to hardware here.
"""
from __future__ import annotations

import csv
import io
import json
import math
from pathlib import Path

from .core import Recipe

MAX_BYTES = 256 * 1024
TOP_NUMBERS = ("cc_current", "end_voltage", "end_current", "max_voltage", "max_current",
               "programmed_cc_current", "programmed_end_voltage", "programmed_end_current")
PROGRAMMED = {"ccCurrent": "programmed_cc_current", "endVoltage": "programmed_end_voltage",
              "endCurrent": "programmed_end_current"}
MAIN = {"ccCurrent": "cc_current", "endVoltage": "end_voltage", "endCurrent": "end_current"}
CHARGER_KEYS = ("batteryType", "maxChargeMinutes", "ratedVoltage", "startVoltage", "temperatureLimit",
                "rechargeVoltage", "derating110", "derating220", "blindCharge", "adjustVoltageMax",
                "adjustCurrentMax", "adjustVoltageMin", "adjustCurrentMin")
CUTOFF_KEYS = ("start_current", "minimum_current", "step_current", "dwell_seconds", "off_voltage",
               "off_current", "confirm_seconds", "max_seconds")


class PresetError(ValueError):
    pass


def _number(key, value):
    if isinstance(value, bool):
        raise PresetError(f"{key} must be a number.")
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise PresetError(f"{key} must be a number, found {value!r}.") from None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PresetError(f"{key} must be a finite number.")
    return value


def _from_table(text):
    rows = {}
    for number, row in enumerate(csv.reader(io.StringIO(text)), 1):
        row = [cell.strip() for cell in row]
        if not row or not any(row) or row[0].startswith("#"):
            continue
        if len(row) < 2 or not row[0]:
            raise PresetError(f"Line {number}: use two columns, field and value.")
        if row[0] in rows:
            raise PresetError(f"Line {number}: {row[0]} appears twice.")
        rows[row[0]] = row[1]
    if rows.get("field") == "value":  # optional header line
        rows.pop("field")
    data, device, cutoff = {}, {}, {}
    for key, value in rows.items():
        if key == "name":
            data["name"] = value
        elif key in CHARGER_KEYS:
            device[key] = value
        elif key in CUTOFF_KEYS:
            cutoff[key] = value
        elif key in PROGRAMMED:  # the charger value; also the required target unless that is given
            data[PROGRAMMED[key]] = value
            data.setdefault(MAIN[key], value)
        else:
            data[key] = value
    if device:
        data["device_settings"] = device
    if cutoff:
        data["termination_test"] = {"enabled": True, **cutoff}
    return data


def load_preset(path):
    """Return ``(recipe, ignored_keys)``; raise PresetError with a plain message if unusable."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PresetError(f"The file could not be opened: {exc.strerror or exc}.") from None
    if size == 0 or size > MAX_BYTES:
        raise PresetError("The preset file is empty or larger than 256 KB.")
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PresetError("The preset file must be UTF-8 text.") from None
    if path.suffix.lower() == ".json" or text.lstrip().startswith(("{", "[")):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise PresetError(f"The preset is not valid JSON: {exc}.") from None
        if isinstance(data, list):
            if len(data) != 1:
                raise PresetError(f"The file holds {len(data)} recipes. Import one recipe per file.")
            data = data[0]
        if isinstance(data, dict) and isinstance(data.get("recipe"), dict):
            data = data["recipe"]
    else:
        data = _from_table(text)
    if not isinstance(data, dict):
        raise PresetError("The preset must describe one recipe.")
    known = {"name", "device_settings", "termination_test", *TOP_NUMBERS}
    ignored = sorted(str(key) for key in data if key not in known)
    clean = {}
    if "name" in data:
        if not isinstance(data["name"], str):
            raise PresetError("name must be text.")
        clean["name"] = data["name"].strip()
    for key in TOP_NUMBERS:
        if key in data and data[key] is not None:
            clean[key] = float(_number(key, data[key]))
    device = data.get("device_settings", {})
    if not isinstance(device, dict):
        raise PresetError("device_settings must be a set of named numbers.")
    clean["device_settings"] = {str(k): float(_number(k, v)) for k, v in device.items()}
    cutoff = data.get("termination_test")
    if cutoff is not None:
        if not isinstance(cutoff, dict):
            raise PresetError("termination_test must be a set of named values.")
        clean["termination_test"] = {"enabled": bool(cutoff.get("enabled", True)),
            **{str(k): float(_number(k, v)) for k, v in cutoff.items() if k != "enabled"}}
    if not any(key in clean for key in TOP_NUMBERS) and not clean["device_settings"]:
        raise PresetError("The file has no recipe values.")
    return Recipe.from_dict(clean), ignored
