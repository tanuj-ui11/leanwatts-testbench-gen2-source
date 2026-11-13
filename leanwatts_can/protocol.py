"""Offline JK charger codec recovered from the supplied vendor executable.

No device, DLL, USB, CAN, serial or network access exists in this module.
These mappings describe what that executable sends, not a bench-verified
firmware specification. Representable ranges are not safe operating limits.
See reverse_engineering/protocol-report.md and charger-protocol.js.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

COMMAND_ID = 0x1806E550
BATTERY_NOT_CONNECTED = 0x0020


def describe_charger_fault(word: int) -> str:
    """Label the supplied application's default fault-word mapping only.

    SetTEXTBox IL_1c17..1c35 tests err & 32 and displays 电池未接.
    err comes from 18FF52E5 bytes 4/5 (IL_2110..2157). This is not
    1806B4C4's separate, active-low charge-status bit at IL_1d56.
    Other bits retain their raw value rather than gaining unverified labels.
    """
    if type(word) is not int or not 0 <= word <= 0xFFFF:
        raise ValueError("Charger fault word must be an unsigned 16-bit integer.")
    if not word:
        return "Charger CAN fault word 0x0000 (clear)"
    labels = []
    if word & BATTERY_NOT_CONNECTED:
        labels.append("Battery not connected")
    other = word & ~BATTERY_NOT_CONNECTED
    if other:
        labels.append(f"other fault bits 0x{other:04X}")
    return f"Charger CAN fault word 0x{word:04X}: " + "; ".join(labels)


CONFIG: dict[str, dict[str, Any]] = {
    "batteryType": {"selector": 0x01, "scale": 1, "input": "textBox1", "response": 0x18FF53E5, "offset": 0},
    "ratedVoltage": {"selector": 0x02, "scale": 10, "input": "textBox2", "response": 0x18FF53E5, "offset": 2},
    "startVoltage": {"selector": 0x03, "scale": 10, "input": "textBox3", "response": 0x18FF53E5, "offset": 4},
    "temperatureLimit": {"selector": 0x04, "scale": 1, "input": "textBox4", "response": 0x18FF53E5, "offset": 6},
    "maxChargeMinutes": {"selector": 0x05, "scale": 1, "input": "textBox7", "response": 0x18FF54E5, "offset": 0},
    "endVoltage": {"selector": 0x06, "scale": 10, "input": "textBox8", "response": 0x18FF54E5, "offset": 2},
    "ccCurrent": {"selector": 0x07, "scale": 10, "input": "textBox6", "response": 0x18FF54E5, "offset": 4},
    "derating110": {"selector": 0x08, "scale": 1, "input": "textBox9", "response": 0x18FF54E5, "offset": 6},
    "derating220": {"selector": 0x09, "scale": 1, "input": "textBox10", "response": 0x18FF55E5, "offset": 0},
    "endCurrent": {"selector": 0x0A, "scale": 10, "input": "textBox5", "response": 0x18FF55E5, "offset": 2},
    "blindCharge": {"selector": 0x0C, "scale": 1, "input": "textBox71", "response": 0x18FF55E5, "offset": 5, "width": 1},
    "rechargeVoltage": {"selector": 0x0D, "scale": 10, "input": "textBox73", "response": 0x18FF55E5, "offset": 6},
    "adjustCurrentMin": {"selector": 0x0E, "scale": 10, "input": "textBox82", "response": 0x18FF56E5, "offset": 6},
    "adjustCurrentMax": {"selector": 0x0F, "scale": 10, "input": "textBox78", "response": 0x18FF56E5, "offset": 4},
    "adjustVoltageMin": {"selector": 0x10, "scale": 10, "input": "textBox76", "response": 0x18FF56E5, "offset": 2},
    "adjustVoltageMax": {"selector": 0x11, "scale": 10, "input": "textBox80", "response": 0x18FF56E5, "offset": 0},
}

# Original timer's descending counter gives this non-sequential selector order.
WRITE_ORDER = (
    "adjustVoltageMin", "adjustVoltageMax", "adjustCurrentMin", "adjustCurrentMax",
    "rechargeVoltage", "blindCharge", "batteryType", "ratedVoltage", "startVoltage",
    "temperatureLimit", "maxChargeMinutes", "endVoltage", "ccCurrent", "derating110",
    "derating220", "endCurrent",
)


def _frame(data: list[int]) -> dict[str, Any]:
    return {"id": COMMAND_ID, "extended": True, "remote": False, "dlc": len(data), "data": data}


def _scaled(value: float, scale: int, maximum: int = 32767) -> int:
    # bool is an int in Python, but is not an explicit numeric charger setting.
    if type(value) not in (int, float):
        raise ValueError("A finite nonnegative number is required.")
    try:
        finite = math.isfinite(value)
        product = value * scale
        if not finite or value < 0 or not math.isfinite(product):
            raise ValueError("A finite nonnegative number is required.")
        raw = round(product)
    except OverflowError as exc:
        raise ValueError("Value is outside the codec range.") from exc
    if abs(product - raw) > 1e-7 or raw > maximum:
        raise ValueError("Value is outside the codec range or supported precision.")
    return raw


def _word(selector: int, raw: int) -> dict[str, Any]:
    return _frame([0, selector, (raw >> 8) & 255, raw & 255])


def command(name: str) -> dict[str, Any]:
    """Encode an evidenced action. No acknowledgement is invented for saves."""
    commands = {
        "workStart": [0, 0x14, 0, 0],
        "shutdown": [0, 0x14, 0, 0x80],
        "calibrationStart": [0, 0x14, 0, 1],
        "saveVoltage": [0, 0x17],
        "saveFullCurrent": [0, 0x18],
        "saveHalfCurrent": [0, 0x19],
    }
    if not isinstance(name, str) or name not in commands:
        raise ValueError("Unknown command; no bytes generated.")
    return _frame(commands[name])


def encode_config(name: str, value: float) -> dict[str, Any]:
    if not isinstance(name, str) or name not in CONFIG:
        raise ValueError(f"Unknown configuration field: {name!r}.")
    spec = CONFIG[name]
    maximum = 255 if spec.get("width") == 1 else 32767
    try:
        raw = _scaled(value, spec["scale"], maximum)
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}") from exc
    return _word(spec["selector"], raw)


def encode_all_config(values: Mapping[str, float]) -> list[dict[str, Any]]:
    """Require all sixteen explicit values before returning the complete plan."""
    if not isinstance(values, Mapping):
        raise ValueError("A complete variant mapping is required.")
    unknown = [str(key) for key in values if key not in CONFIG]
    if unknown:
        raise ValueError("Unknown variant fields: " + ", ".join(unknown))
    missing = [name for name in WRITE_ORDER if name not in values or values[name] is None]
    if missing:
        raise ValueError("Missing required variant fields: " + ", ".join(missing))
    return [encode_config(name, values[name]) for name in WRITE_ORDER]


def encode_duty(axis: str, value: float) -> dict[str, Any]:
    """Encode the displayed duty × 10, within the original UI range only.

    The 0..1000 range is not a safe hardware limit and does not establish PWM
    units. Original application edits sent both axes three times; this function
    encodes one frame and does not infer or schedule the other axis.
    """
    if axis not in ("voltage", "current"):
        raise ValueError("Unknown calibration axis.")
    return _word(0x15 if axis == "voltage" else 0x16, _scaled(value, 10, 10000))


def poll(group: int) -> dict[str, Any]:
    if type(group) is not int or not 1 <= group <= 6:
        raise ValueError("Poll group must be an integer from 1 to 6.")
    return _word(0x0B, group)


def decode(frame: Mapping[str, Any]) -> dict[str, Any]:
    """Decode exact eight-byte extended data frames; unknown IDs remain unknown."""
    if not isinstance(frame, Mapping) or frame.get("extended") is not True or frame.get("remote") is not False:
        raise ValueError("Expected an explicit extended CAN data frame.")
    frame_id, dlc, data = frame.get("id"), frame.get("dlc"), frame.get("data")
    if (type(frame_id) is not int or not 0 <= frame_id <= 0x1FFFFFFF
            or type(dlc) is not int or dlc != 8
            or not isinstance(data, (list, tuple, bytes, bytearray)) or len(data) != 8
            or any(type(x) is not int or not 0 <= x <= 255 for x in data)):
        raise ValueError("Expected a valid 8-byte classic CAN receive frame.")

    def u16(offset: int) -> int:
        return (data[offset] << 8) | data[offset + 1]

    def i16(offset: int) -> int:
        raw = u16(offset)
        return raw - 65536 if raw >= 32768 else raw

    params = {
        name: (data[spec["offset"]] if spec.get("width") == 1 else i16(spec["offset"])) / spec["scale"]
        for name, spec in CONFIG.items() if spec["response"] == frame_id
    }
    if params:
        return {"kind": "configurationReadback", "id": frame_id, "values": params}
    if frame_id == 0x18FF50E5:
        values = {"outputVoltage": u16(0) / 10, "outputCurrent": u16(2) / 10, "statusByte": data[4]}
    elif frame_id == 0x18FF51E5:
        values = {"inputVoltage": i16(0), "pfcVoltage": i16(2), "maxPower": i16(4), "workMinutes": i16(6)}
    elif frame_id == 0x18FF52E5:
        values = {"protectionTemperature": i16(0), "ambientTemperature": i16(2), "faultWord": u16(4)}
    else:
        return {"kind": "unknown", "id": frame_id}
    return {"kind": "telemetry", "id": frame_id, "values": values}


def verify(expected: Mapping[str, float], frames: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare a complete expected variant with supplied readback frames offline.

    Later list entries replace earlier observations for that response group.
    The caller must establish capture ordering and freshness; this function
    does not prove a write was transmitted, acknowledged or persisted.
    """
    encode_all_config(expected)
    observed: dict[str, float] = {}
    for frame in frames:
        decoded = decode(frame)
        if decoded["kind"] == "configurationReadback":
            observed.update(decoded["values"])
    missing = [name for name in WRITE_ORDER if name not in observed]
    mismatches = {
        name: {"expected": expected[name], "actual": observed[name]}
        for name in WRITE_ORDER
        if name in observed and not math.isclose(expected[name], observed[name], rel_tol=0, abs_tol=1e-8)
    }
    return {
        "matched": not missing and not mismatches,
        "missing": missing, "mismatches": mismatches, "observed": observed,
        "scope": "Offline value comparison only; freshness, transmission, save acknowledgement and persistence are unverified.",
    }
