"""Sends queued EoL reports one at a time from a QTimer on the GUI thread. No worker threads, no instrument access."""
from __future__ import annotations

import time

from PySide6.QtCore import QObject, QTimer, Signal

from .dashboard_client import DashboardError

RESULTS = {"PASS": "pass", "FAIL": "fail", "INCOMPLETE": "incomplete"}
DEAD_KINDS = ("rejected", "too_large", "not_found")


def report_body(report, html, *, station, generated_at):
    """Map the report JSON (spec 3.4) to the bench POST body (contract). html is sent as report_html."""
    if not isinstance(report, dict) or report.get("verdict") not in RESULTS:
        raise ValueError("Only a PASS, FAIL or INCOMPLETE report can be sent.")
    stages = report.get("stages") if isinstance(report.get("stages"), list) else []
    operator = report.get("operator") if isinstance(report.get("operator"), dict) else {}
    software = report.get("software") if isinstance(report.get("software"), dict) else {}
    identity = report.get("identity") if isinstance(report.get("identity"), dict) else {}
    name = str(operator.get("name", "")).strip()
    who = f"{name} ({operator.get('id', '')})" if name else str(operator.get("id", ""))
    return {
        "serial": report.get("serial", ""),
        "result": RESULTS[report["verdict"]],
        "tested_at": report.get("finished_at_utc", ""),
        "generated_at": generated_at,
        "station": report.get("station") or station,
        "operator": who,
        "tool_version": software.get("version", ""),
        "firmware_version": identity.get("charger_fw"),
        "summary": {"stages": len(stages), "failed": sum(1 for s in stages if isinstance(s, dict) and s.get("status") == "FAIL")},
        "report": report,
    }


class OutboxService(QObject):
    changed = Signal()

    def __init__(self, outbox, client_provider, *, busy_check=lambda: False, clock=time.time,
                 interval_ms=10000, parent=None):
        super().__init__(parent)
        self.outbox = outbox
        self._client_provider = client_provider
        self._busy_check = busy_check
        self._clock = clock
        self.paused_message = ""
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._on_timer)

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def resume(self):
        """Called after the operator saves the dashboard settings."""
        self.paused_message = ""
        self.changed.emit()

    def _on_timer(self):
        summary = self.tick(self._clock())
        if summary["sent"] or summary["failed"] or summary["dead"]:
            self.changed.emit()

    def tick(self, now):
        """Send each due item in order. Network and server errors back off and stop this tick;
        an auth error pauses the queue; a definitive refusal dead-letters that one item."""
        summary = {"sent": 0, "failed": 0, "dead": 0, "stopped": ""}
        if self.paused_message:
            summary["stopped"] = "paused"
            return summary
        if self._busy_check():
            summary["stopped"] = "busy"
            return summary
        due = self.outbox.due_items(now)
        if not due:
            return summary
        try:
            client = self._client_provider()
        except ValueError as exc:
            self.paused_message = str(exc)
            summary["stopped"] = "paused"
            return summary
        for item in due:
            if self._busy_check():
                summary["stopped"] = "busy"
                break
            try:
                body, html = self.outbox.load_body(item)
            except ValueError as exc:
                self.outbox.mark_failed(item, str(exc), now)
                summary["failed"] += 1
                continue
            try:
                result = client.post_report(dict(body, report_html=html), client_report_id=item.client_report_id)
            except DashboardError as exc:
                if exc.kind == "auth":
                    self.outbox.note_error(item, exc.message)
                    self.paused_message = exc.message
                    summary["stopped"] = "paused"
                    break
                if exc.kind in DEAD_KINDS:
                    self.outbox.mark_dead(item, exc.message, now)
                    summary["dead"] += 1
                    continue
                self.outbox.mark_failed(item, exc.message, now)
                summary["failed"] += 1
                summary["stopped"] = "backoff"
                break
            except Exception:
                self.outbox.mark_failed(item, "Unexpected error while sending; it will be retried.", now)
                summary["failed"] += 1
                summary["stopped"] = "backoff"
                break
            self.outbox.mark_sent(item, result, now)
            summary["sent"] += 1
        return summary
