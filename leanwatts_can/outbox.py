"""Durable file outbox for EoL reports. Files only: no network, no instruments.

Layout under the outbox root:
  pending/<id>.json        immutable payload (POST body + report HTML)
  pending/<id>.state.json  small mutable state (attempts, next try, last error, identity)
  sent/ and dead/          the same two files after a final outcome
Every write is temp file + os.replace. A payload is only ever moved, never rewritten or deleted on error.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "leanwatts-outbox/1"
FOLDERS = ("pending", "sent", "dead")
BACKOFF_FIRST = 15.0
BACKOFF_MAX = 900.0
JITTER = 0.2
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_HTML_CHARS = 256 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{7,79}")


def report_client_id(station, session_id, serial, attempt_id):
    """Stable UUID-shaped id for one report version; the same value is the Idempotency-Key."""
    material = json.dumps([station, session_id, serial, attempt_id], ensure_ascii=False)
    return str(uuid.UUID(hex=hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]))


def _checked_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("The outbox report id is invalid.")
    return value


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class Item:
    client_report_id: str
    folder: str
    serial: str
    session_id: str
    created_at: str
    attempts: int
    next_attempt_at: float
    last_error: str
    server: dict

    @property
    def status(self):
        if self.folder == "sent":
            return "sent"
        if self.folder == "dead":
            return "dead"
        return "failed" if self.attempts else "pending"


class Outbox:
    def __init__(self, root, *, clock=time.time, rand=random.random):
        self.root = Path(root)
        self._clock = clock
        self._rand = rand
        # Parsed state per folder and id, valid while the files' (mtime, size) are unchanged.
        self._cache = {folder: {} for folder in FOLDERS}
        for folder in FOLDERS:
            (self.root / folder).mkdir(parents=True, exist_ok=True)

    def _path(self, folder, client_report_id, suffix=".json"):
        return self.root / folder / (_checked_id(client_report_id) + suffix)

    def _state_defaults(self, client_report_id, payload, now):
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        return {"schema": SCHEMA, "client_report_id": client_report_id,
                "serial": str(body.get("serial", "")), "session_id": str(payload.get("session_id", "")),
                "created_at": str(payload.get("created_at", "")), "attempts": 0,
                "next_attempt_at": now, "last_error": "", "server": {}}

    def _read_state(self, folder, client_report_id):
        """State is small and read for every listing. A missing or damaged state is rebuilt from the
        payload with next_attempt_at=0 so the report is retried, never lost."""
        path = self._path(folder, client_report_id, ".state.json")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and state.get("schema") == SCHEMA:
                return state
        except (OSError, ValueError):
            pass
        payload = json.loads(self._path(folder, client_report_id).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            raise ValueError("The queued report is invalid.")
        return self._state_defaults(client_report_id, payload, 0.0)

    def _item(self, folder, client_report_id):
        state = self._read_state(folder, client_report_id)
        server = state.get("server") if isinstance(state.get("server"), dict) else {}
        return Item(client_report_id=client_report_id, folder=folder,
                    serial=str(state.get("serial", "")), session_id=str(state.get("session_id", "")),
                    created_at=str(state.get("created_at", "")), attempts=int(state.get("attempts", 0) or 0),
                    next_attempt_at=float(state.get("next_attempt_at", 0.0) or 0.0),
                    last_error=str(state.get("last_error", "")), server=server)

    def _forget(self, folder, client_report_id):
        self._cache[folder].pop(client_report_id, None)

    def _list(self, folder):
        """Items of one folder. One directory scan; only files whose (mtime, size) changed are parsed again."""
        payloads, states = {}, {}
        try:
            with os.scandir(self.root / folder) as entries:
                for entry in entries:
                    name = entry.name
                    if not name.endswith(".json"):
                        continue
                    try:
                        status = entry.stat()
                    except OSError:
                        continue
                    signature = (status.st_mtime_ns, status.st_size)
                    if name.endswith(".state.json"):
                        states[name[:-len(".state.json")]] = signature
                    else:
                        payloads[name[:-len(".json")]] = signature
        except OSError:
            return []
        cache, items = self._cache[folder], []
        for client_report_id, payload_signature in payloads.items():
            signature = (payload_signature, states.get(client_report_id))
            cached = cache.get(client_report_id)
            if cached is not None and cached[0] == signature:
                items.append(cached[1])
                continue
            try:
                item = self._item(folder, client_report_id)
            except (OSError, ValueError, TypeError):
                # Unreadable files stay where they are for inspection; they are never deleted here.
                cache.pop(client_report_id, None)
                continue
            cache[client_report_id] = (signature, item)
            items.append(item)
        for gone in set(cache) - set(payloads):
            del cache[gone]
        return sorted(items, key=lambda item: (item.created_at, item.client_report_id))

    def pending_items(self):
        return self._list("pending")

    def sent_items(self):
        return self._list("sent")

    def dead_items(self):
        return self._list("dead")

    def due_items(self, now):
        return [item for item in self.pending_items() if item.next_attempt_at <= now]

    def load_body(self, item):
        """Return (body, html) for sending. Raises ValueError when the payload is unreadable."""
        payload = json.loads(self._path(item.folder, item.client_report_id).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA or not isinstance(payload.get("body"), dict):
            raise ValueError("The queued report is invalid.")
        return payload["body"], payload.get("html")

    def _find(self, client_report_id):
        for folder in FOLDERS:
            if self._path(folder, client_report_id).is_file():
                return self._item(folder, client_report_id)
        return None

    def write_pending(self, body, html, client_report_id, *, session_id="", now=None):
        """Queue one report. An id already pending, sent or dead is returned unchanged and never queued twice."""
        client_report_id = _checked_id(client_report_id)
        if not isinstance(body, dict) or not isinstance(body.get("serial"), str) or not body["serial"]:
            raise ValueError("The report has no serial number.")
        if html is not None and (not isinstance(html, str) or len(html) > MAX_HTML_CHARS):
            raise ValueError("The report HTML is missing or too large.")
        existing = self._find(client_report_id)
        if existing is not None:
            return existing
        now = self._clock() if now is None else now
        created = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
        payload = {"schema": SCHEMA, "client_report_id": client_report_id, "session_id": str(session_id),
                   "created_at": created, "body": body, "html": html}
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ValueError("The report is too large to queue.")
        self._forget("pending", client_report_id)
        _atomic_write(self._path("pending", client_report_id), encoded)
        state = self._state_defaults(client_report_id, payload, now)
        state["serial"] = body["serial"]
        _atomic_write(self._path("pending", client_report_id, ".state.json"),
                      json.dumps(state, ensure_ascii=False).encode("utf-8"))
        return self._item("pending", client_report_id)

    def _write_state(self, item, folder, state):
        self._forget(folder, item.client_report_id)
        _atomic_write(self._path(folder, item.client_report_id, ".state.json"),
                      json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def _state_of(self, item):
        state = self._read_state(item.folder, item.client_report_id)
        state.update(attempts=item.attempts, next_attempt_at=item.next_attempt_at,
                     last_error=item.last_error, server=dict(item.server), serial=item.serial,
                     session_id=item.session_id, created_at=item.created_at)
        return state

    def _move(self, item, source, target, state):
        """Commit point is the payload move. Duplicates of an already-finished id collapse to one file."""
        if not self._path(source, item.client_report_id).is_file():
            raise ValueError("The queued report is no longer in " + source + ".")
        self._write_state(item, target, state)
        self._forget(source, item.client_report_id)
        self._forget(target, item.client_report_id)
        os.replace(self._path(source, item.client_report_id), self._path(target, item.client_report_id))
        self._path(source, item.client_report_id, ".state.json").unlink(missing_ok=True)
        return self._item(target, item.client_report_id)

    def mark_sent(self, item, result=None, now=None):
        now = self._clock() if now is None else now
        result = result if isinstance(result, dict) else {}
        server = {key: result[key] for key in ("id", "duplicate", "replaced") if key in result}
        state = dict(self._state_of(item), last_error="", server=server,
                     sent_at=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)))
        return self._move(item, "pending", "sent", state)

    def mark_dead(self, item, message, now=None):
        now = self._clock() if now is None else now
        state = dict(self._state_of(item), last_error=str(message)[:300],
                     dead_at=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)))
        return self._move(item, "pending", "dead", state)

    def backoff_seconds(self, attempts):
        """Wait after the given failed attempt count: 15 s doubling to 15 min, plus up to 20 % jitter."""
        base = min(BACKOFF_FIRST * (2 ** min(max(attempts - 1, 0), 10)), BACKOFF_MAX)
        return base * (1 + JITTER * self._rand())

    def mark_failed(self, item, message, now=None):
        now = self._clock() if now is None else now
        attempts = item.attempts + 1
        state = dict(self._state_of(item), attempts=attempts, last_error=str(message)[:300],
                     next_attempt_at=now + self.backoff_seconds(attempts))
        self._write_state(item, "pending", state)
        return self._item("pending", item.client_report_id)

    def note_error(self, item, message):
        """Record an error without consuming a retry (used while the dashboard key is refused)."""
        state = dict(self._state_of(item), last_error=str(message)[:300])
        self._write_state(item, "pending", state)
        return self._item("pending", item.client_report_id)

    def requeue(self, client_report_id, now=None):
        """Move a dead-lettered report back to pending for an explicit operator resend."""
        now = self._clock() if now is None else now
        item = self._find(client_report_id)
        if item is None or item.folder != "dead":
            raise ValueError("Only a dead report can be sent again.")
        if self._path("pending", client_report_id).is_file():
            raise ValueError("This report is already waiting to be sent.")
        state = dict(self._state_of(item), attempts=0, next_attempt_at=now, last_error="", dead_at=None)
        return self._move(item, "dead", "pending", state)

    def status_map(self):
        """{(session_id, serial): most recent item} for the whole outbox from one scan of the three folders."""
        best = {}
        for folder in FOLDERS:
            for item in self._list(folder):
                key = (item.session_id, item.serial)
                current = best.get(key)
                if current is None or (item.created_at, item.client_report_id) > (current.created_at, current.client_report_id):
                    best[key] = item
        return best

    def status_for(self, session_id, serial):
        """Most recent outbox item for one serial of one session, or None."""
        return self.status_map().get((session_id, serial))
