"""Work-order serial list: parsing, scan normalisation and warn-only classification.

Warnings never block a unit (spec decision D5). The list is held in memory only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .serial_numbers import split_serial_parts, strip_scan_noise


OK = "OK"
NOT_ON_LIST = "NOT_ON_LIST"
OTHER_WORK_ORDER = "OTHER_WORK_ORDER"
REJECTED = "REJECTED"
REPLACED = "REPLACED"
ALREADY_PASSED_REMOTE = "ALREADY_PASSED_REMOTE"
ALREADY_DONE_LOCAL = "ALREADY_DONE_LOCAL"
CODES = (OK, NOT_ON_LIST, OTHER_WORK_ORDER, REJECTED, REPLACED, ALREADY_PASSED_REMOTE, ALREADY_DONE_LOCAL)


def split_serial(text):
    """Return (work_order, token, line_seq) or None. token is None for the legacy shape WO-S00125-001."""
    parts = split_serial_parts(text)
    if parts is None:
        return None
    work_order, token, seq = parts
    return work_order, token, int(seq)


def normalise_scan(text):
    """Strip scanner framing; upper-case only a lower-case serial that has a serial shape."""
    value = strip_scan_noise(text)
    if value and value == value.lower() and split_serial_parts(value.upper()) is not None:
        value = value.upper()
    return value


@dataclass(frozen=True)
class WorklistItem:
    serial: str
    seq: int | None = None
    line_token: str | None = None
    line_seq: int | None = None
    product: str | None = None
    is_buffer_stock: bool = False
    rejected: bool = False
    reworked: bool = False
    replaced_by_serial: str | None = None
    report: dict | None = None

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or not isinstance(data.get("serial"), str) or not data["serial"].strip():
            raise ValueError("Each serial in the work-order list needs a serial number.")
        report = data.get("report")
        return cls(
            serial=data["serial"].strip(),
            seq=data.get("seq"),
            line_token=data.get("line_token"),
            line_seq=data.get("line_seq"),
            product=data.get("product"),
            is_buffer_stock=bool(data.get("is_buffer_stock", False)),
            rejected=bool(data.get("rejected", False)),
            reworked=bool(data.get("reworked", False)),
            replaced_by_serial=data.get("replaced_by_serial"),
            report=dict(report) if isinstance(report, dict) else None,
        )

    def to_dict(self):
        return {
            "serial": self.serial,
            "seq": self.seq,
            "line_token": self.line_token,
            "line_seq": self.line_seq,
            "product": self.product,
            "is_buffer_stock": self.is_buffer_stock,
            "rejected": self.rejected,
            "reworked": self.reworked,
            "replaced_by_serial": self.replaced_by_serial,
            "report": dict(self.report) if self.report else None,
        }


@dataclass(frozen=True)
class Classification:
    code: str
    message: str
    blocking: bool = False
    item: WorklistItem | None = None

    def __post_init__(self):
        if self.code not in CODES:
            raise ValueError(f"Unknown worklist code {self.code!r}.")
        if self.blocking is not False:
            raise ValueError("Worklist checks warn only and never block a unit.")


class Worklist:
    """One work order's serials, as downloaded from the bench API (GET /work-orders/{ref}/serials)."""

    def __init__(self, wo_number, work_order_id=None, as_of=None, serials=()):
        self.wo_number = wo_number
        self.work_order_id = work_order_id
        self.as_of = as_of
        self.serials = tuple(serials)
        self._by_serial = {}
        for item in self.serials:
            if item.serial in self._by_serial:
                raise ValueError(f"{item.serial} appears twice in the work-order list.")
            self._by_serial[item.serial] = item

    @classmethod
    def from_contract(cls, payload):
        if not isinstance(payload, dict):
            raise ValueError("The dashboard returned no work-order list.")
        wo_number = payload.get("wo_number")
        if not isinstance(wo_number, str) or re.fullmatch(r"WO-S[0-9]{5}", wo_number) is None:
            raise ValueError("The work-order list has no valid work-order number.")
        serials = payload.get("serials")
        if not isinstance(serials, list):
            raise ValueError("The work-order list has no serial list.")
        return cls(wo_number=wo_number, work_order_id=payload.get("work_order_id"), as_of=payload.get("as_of"),
                   serials=[WorklistItem.from_dict(entry) for entry in serials])

    @classmethod
    def from_dict(cls, data):
        return cls.from_contract(data)

    def to_dict(self):
        return {"wo_number": self.wo_number, "work_order_id": self.work_order_id, "as_of": self.as_of,
                "serials": [item.to_dict() for item in self.serials]}

    def lookup(self, scan):
        """Exact match after normalisation; the short form WO-S00220-036 only when exactly one serial ends -036."""
        value = normalise_scan(scan)
        item = self._by_serial.get(value)
        if item is not None:
            return item
        matches = self._short_matches(value)
        return matches[0] if len(matches) == 1 else None

    def short_form_ambiguous(self, scan):
        return len(self._short_matches(normalise_scan(scan))) > 1

    def passed_remote(self):
        """Serials whose latest dashboard report is a pass, as of the download."""
        return {item.serial for item in self.serials
                if isinstance(item.report, dict) and item.report.get("result") == "pass"}

    def _short_matches(self, value):
        parts = split_serial(value)
        # Only the legacy shape with this list's own work order; the work order is never guessed.
        if parts is None or parts[1] is not None or parts[0] != self.wo_number:
            return []
        tail = value.rsplit("-", 1)[1]
        return [item for item in self.serials
                if item.serial.endswith("-" + tail) and split_serial(item.serial) is not None
                and split_serial(item.serial)[0] == self.wo_number]


def _result(code, message, item=None):
    return Classification(code=code, message=message, blocking=False, item=item)


def classify(scan, worklist, *, already_passed_remote=None, already_done_local=None):
    """Compare one scan with the work-order list. Every code is a warning; none blocks."""
    value = normalise_scan(scan)
    wo = worklist.wo_number
    if not value:
        return _result(NOT_ON_LIST, "Scan or type a charger serial number.")
    parts = split_serial(value)
    if parts is not None and parts[0] != wo:
        return _result(OTHER_WORK_ORDER, f"{value} belongs to work order {parts[0]}, not {wo}.")
    item = worklist.lookup(value)
    if item is None:
        if worklist.short_form_ambiguous(value):
            return _result(NOT_ON_LIST, f"{value} matches more than one serial on {wo}. Scan the full serial number.")
        return _result(NOT_ON_LIST, f"{value} is not on work order {wo}.")
    if item.replaced_by_serial:
        return _result(REPLACED, f"This charger was replaced by {item.replaced_by_serial}.", item)
    if item.rejected:
        return _result(REJECTED, f"{item.serial} is marked rejected on {wo}.", item)
    if item.serial in (already_passed_remote or ()):
        return _result(ALREADY_PASSED_REMOTE, f"{item.serial} already has a passing report on the dashboard.", item)
    if item.serial in (already_done_local or ()):
        return _result(ALREADY_DONE_LOCAL, f"{item.serial} was already completed on this bench.", item)
    return _result(OK, f"{item.serial} is on work order {wo}.", item)
