"""Work-order dropdown data: validate the dashboard list, build display entries, keep an offline copy.

Pure Python, no Qt, no network. The cache file holds work-order numbers, customers, counts and state only;
it never holds a serial number, an operator name or a sign-off note.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .session_store import atomic_json

SCHEMA = 1
CACHE_NAME = "work_orders.json"
MAX_ROWS = 500
MAX_TEXT = 200
MAX_COUNT = 1_000_000
SIGNOFF_DAYS = 14
STATES = ("open", "signed_off", "past")
PAST_HEADING = "Past work orders"
CUSTOMER_SHOWN = 18
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
_COUNT_FIELDS = ("total", "tested", "passed", "failed", "untested")


def valid_reference(value):
    """True for text that can be a work-order number. Used to enable Sign off; the server still decides."""
    return isinstance(value, str) and _REFERENCE.fullmatch(value.strip()) is not None


def _clean(value, limit=MAX_TEXT):
    if not isinstance(value, str):
        return ""
    text = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    return text[:limit]


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNT:
        raise ValueError("count")
    return value


def _stamp(value, *, required=False):
    """Return the ISO text unchanged when it parses with a timezone; None when absent. ValueError otherwise."""
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("time")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("time")
    return value


def _parse_signoff(value, *, keep_private=True):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("signoff")
    result = {"signed_off_at": _stamp(value.get("signed_off_at"), required=True)}
    if keep_private:
        result["operator"] = _clean(value.get("operator"), 120)
        note = _clean(value.get("note"), 1000)
        result["note"] = note or None
        station = _clean(value.get("station"), 120)
        result["station"] = station or None
    for key in ("untested_count", "total_units"):
        result[key] = _count(value[key]) if value.get(key) is not None else None
    return result


@dataclass(frozen=True)
class WorkOrder:
    wo_number: str
    order: str
    customer: str
    total: int
    tested: int
    passed: int
    failed: int
    untested: int
    last_report_at: str | None
    state: str
    signoff: dict | None


def parse_row(raw, *, keep_private=True):
    """Validate one row of the contract. Raises ValueError for a malformed row."""
    if not isinstance(raw, dict):
        raise ValueError("row")
    number = raw.get("wo_number")
    if not isinstance(number, str) or not _REFERENCE.fullmatch(number.strip()):
        raise ValueError("wo_number")
    state = raw.get("state")
    if state not in STATES:
        raise ValueError("state")
    counts = {name: _count(raw.get(name)) for name in _COUNT_FIELDS}
    if counts["tested"] > counts["total"] or counts["untested"] > counts["total"]:
        raise ValueError("counts")
    signoff = _parse_signoff(raw.get("signoff"), keep_private=keep_private)
    if state != "open" and signoff is None:
        raise ValueError("signoff")
    return WorkOrder(number.strip(), _clean(raw.get("order"), 40), _clean(raw.get("customer")),
                     last_report_at=_stamp(raw.get("last_report_at")), state=state, signoff=signoff, **counts)


def parse_work_orders(payload, *, keep_private=True):
    """Return (rows, skipped, generated_at) from the dashboard payload.

    A payload that is not the expected object raises ValueError. A malformed row is skipped and counted;
    duplicates keep the first; at most MAX_ROWS rows are kept."""
    if not isinstance(payload, dict) or not isinstance(payload.get("work_orders"), list):
        raise ValueError("The dashboard sent a work-order list this app does not understand.")
    try:
        generated = _stamp(payload.get("generated_at"))
    except ValueError:
        generated = None
    rows, seen, skipped = [], set(), 0
    for raw in payload["work_orders"]:
        try:
            row = parse_row(raw, keep_private=keep_private)
        except (ValueError, TypeError, KeyError):
            skipped += 1
            continue
        key = row.wo_number.upper()
        if key in seen:
            skipped += 1
            continue
        if len(rows) >= MAX_ROWS:
            skipped += 1
            continue
        seen.add(key)
        rows.append(row)
    return rows, skipped, generated


def parse_signoff_response(payload):
    """Return (already_signed_off, signoff) from the POST answer. ValueError when it is not understood."""
    if not isinstance(payload, dict) or not isinstance(payload.get("already_signed_off"), bool):
        raise ValueError("The dashboard sent a sign-off answer this app does not understand.")
    try:
        signoff = _parse_signoff(payload.get("signoff"))
    except (ValueError, TypeError, KeyError):
        signoff = None
    if signoff is None:
        raise ValueError("The dashboard sent a sign-off answer this app does not understand.")
    return payload["already_signed_off"], signoff


def with_signoff(rows, wo_number, signoff):
    """Rows with one work order marked signed off (local update after a successful POST)."""
    updated = []
    for row in rows:
        if row.wo_number.upper() == str(wo_number).upper():
            row = WorkOrder(**{**row.__dict__, "state": "signed_off", "signoff": signoff})
        updated.append(row)
    return updated


@dataclass(frozen=True)
class Entry:
    kind: str  # "work_order" or "heading"
    text: str
    wo_number: str = ""
    state: str = ""


def _parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def days_left(row, now):
    """Whole days of the 14 remaining for a signed-off row; 0 on the last day. None when not applicable."""
    if row.signoff is None:
        return None
    at = _parse_time(row.signoff.get("signed_off_at"))
    if at is None:
        return None
    return max(0, SIGNOFF_DAYS - max(0, (now - at).days))


def short_customer(customer):
    customer = customer.strip()
    if len(customer) <= CUSTOMER_SHOWN:
        return customer
    return customer[:CUSTOMER_SHOWN].rstrip() + "…"


def display_text(row, now):
    parts = [row.wo_number]
    if row.customer:
        parts.append(short_customer(row.customer))
    if row.state == "open":
        parts.append("all tested" if row.untested == 0 and row.total else f"{row.untested} untested")
    elif row.state == "signed_off":
        left = days_left(row, now)
        parts.append("signed off" if left is None else
                     "signed off · ends today" if left == 0 else
                     f"signed off · {left} day{'s' if left != 1 else ''} left")
    else:
        parts.append("signed off")
    return " · ".join(parts)


def build_entries(rows, now):
    """Open first, then signed off, then the Past heading and its rows (heading only when there are any)."""
    entries = []
    for state in ("open", "signed_off"):
        entries += [Entry("work_order", display_text(r, now), r.wo_number, state) for r in rows if r.state == state]
    past = [r for r in rows if r.state == "past"]
    if past:
        entries.append(Entry("heading", PAST_HEADING))
        entries += [Entry("work_order", display_text(r, now), r.wo_number, "past") for r in past]
    return entries


def signoff_summary(signoff, local_zone=None):
    """'<date> by <operator>' for the Already signed off message."""
    at = _parse_time(signoff.get("signed_off_at"))
    when = at.astimezone(local_zone).strftime("%d %b %Y") if at else "an earlier date"
    who = signoff.get("operator") or "an operator"
    return f"{when} by {who}"


class WorkOrderCache:
    """<data_dir>/work_orders.json. Reads never raise; a bad or missing file is simply no cache."""

    def __init__(self, data_dir, clock=None):
        self.path = Path(data_dir) / CACHE_NAME
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def save(self, rows, fetched_at=None):
        fetched_at = fetched_at or self._clock().isoformat(timespec="seconds")
        stored = []
        for row in rows:
            signoff = None
            if row.signoff:
                signoff = {"signed_off_at": row.signoff["signed_off_at"],
                           "untested_count": row.signoff.get("untested_count"),
                           "total_units": row.signoff.get("total_units")}
            stored.append({"wo_number": row.wo_number, "order": row.order, "customer": row.customer,
                           **{name: getattr(row, name) for name in _COUNT_FIELDS},
                           "last_report_at": row.last_report_at, "state": row.state, "signoff": signoff})
        atomic_json(self.path, {"schema": SCHEMA, "fetched_at": fetched_at, "work_orders": stored[:MAX_ROWS]})

    def load(self):
        """Return (rows, fetched_at) or None."""
        try:
            if self.path.stat().st_size > 4 * 1024 * 1024:
                return None
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != SCHEMA:
                return None
            fetched = _stamp(data.get("fetched_at"), required=True)
            rows, _skipped, _ = parse_work_orders(data, keep_private=False)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            return None
        return rows, fetched


def age_text(fetched_at, local_zone=None):
    at = _parse_time(fetched_at)
    if at is None:
        return "an unknown time"
    return at.astimezone(local_zone).strftime("%d %b · %H:%M")


__all__ = ["Entry", "WorkOrder", "WorkOrderCache", "build_entries", "parse_work_orders", "parse_signoff_response",
           "valid_reference", "with_signoff", "signoff_summary", "age_text"]
