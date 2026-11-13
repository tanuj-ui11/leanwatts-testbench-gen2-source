"""Session-local starting trims; recorded commands are never charger readbacks."""
from __future__ import annotations

import copy
import math
from dataclasses import replace


OFFSET = 2.0  # displayed trim/duty units, not amperes or volts


def _trim(value):
    return (type(value) in (int, float) and 0 <= value <= 1000 and math.isfinite(value)
            and abs(value * 10 - round(value * 10)) < 1e-7)


def select_session_seed(session, recipe, revision):
    """Use the first completed, successful physical unit of this exact recipe.

    The anchor stays fixed for the session: the offset is never accumulated.
    Failed post-tests exclude that unit too. A resumed open session can retain
    its anchor, but closed sessions and separate variants cannot contribute.
    """
    if not isinstance(session, dict) or session.get("status") != "OPEN":
        return None
    expected = recipe.to_dict() if hasattr(recipe, "to_dict") else recipe
    units = session.get("units")
    if not isinstance(units, list):
        return None
    for unit in units:
        if (not isinstance(unit, dict) or unit.get("outcome") not in ("CALIBRATED", "PASS")
                or unit.get("calibration_result") != "PASS" or unit.get("simulation") is not False
                or not unit.get("finished_at") or unit.get("is_running")
                or unit.get("recipe_revision") != revision or unit.get("recipe") != expected):
            continue
        record = unit.get("calibration")
        if (not isinstance(record, dict) or record.get("is_running") is not False
                or record.get("simulation") is not False
                or record.get("outcome") != "CALIBRATED — PERSISTENCE UNVERIFIED"
                or record.get("stop_outcome") != "Output below approved shutdown thresholds; load OFF confirmed"
                or not isinstance(record.get("preflight_result"), dict)
                or record["preflight_result"].get("status") != "PASS"):
            continue
        results = record.get("results")
        if not isinstance(results, list):
            continue
        statuses = {item.get("name"): item.get("status") for item in results
                    if isinstance(item, dict)}
        if any(statuses.get(axis.title() + " calibration") != "SAVE REQUEST ACCEPTED"
               for axis in ("current", "voltage")):
            continue
        duties = {axis + "_duty": record.get(axis + "_duty") for axis in ("current", "voltage")}
        if not all(_trim(value) for value in duties.values()):
            continue
        return dict(method="session_reference", session_id=session.get("session_id"),
                    source_attempt_id=unit.get("attempt_id"), source_serial=unit.get("serial"),
                    recipe_revision=revision, offset=OFFSET, **duties)
    return None


def apply_session_seed(settings, seed):
    """Return new run settings, preserving station defaults and every other limit."""
    error = settings.validate(calibration=True)
    if error:
        raise ValueError(error)
    metadata = dict(method="configured", current_initial=settings.current_initial,
                    voltage_initial=settings.voltage_initial, offset=0.0, clamped=[])
    if not seed:
        metadata["note"] = "Configured starting trims; no successful reference for this variant in this session."
        return settings, metadata
    if (not isinstance(seed, dict) or seed.get("method") != "session_reference"
            or any(not _trim(seed.get(axis + "_duty")) for axis in ("current", "voltage"))):
        metadata["note"] = "Configured starting trims; the session reference is invalid."
        return settings, metadata
    if any(not getattr(settings, axis + "_min") <= seed[axis + "_duty"] <= getattr(settings, axis + "_max")
           for axis in ("current", "voltage")):
        metadata["note"] = "Configured starting trims; the reference is outside the current approved duty bounds."
        return settings, metadata
    metadata.update(copy.deepcopy(seed))
    metadata["offset"] = OFFSET
    updates = {}
    for axis in ("current", "voltage"):
        requested = round(seed[axis + "_duty"] + OFFSET, 1)
        value = round(min(getattr(settings, axis + "_max"), max(getattr(settings, axis + "_min"), requested)), 1)
        updates[axis + "_initial"] = value
        if value != requested:
            metadata["clamped"].append(axis)
    metadata.update(updates)
    metadata["note"] = "First successful charger in this session plus 2 trim points; not a charger readback."
    return replace(settings, **updates), metadata


def starting_point_text(metadata):
    if not metadata:
        return ""
    prefix = (f"Session reference {metadata.get('source_serial', '')} +2"
              if metadata.get("method") == "session_reference" else "Configured start")
    return (f"{prefix} · current trim {metadata['current_initial']:.1f}, "
            f"voltage trim {metadata['voltage_initial']:.1f}"
            + (" · capped at approved bounds" if metadata.get("clamped") else ""))
