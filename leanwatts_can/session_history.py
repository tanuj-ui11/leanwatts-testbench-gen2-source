"""Session reports with read-only access to earlier individual run records."""
from __future__ import annotations

import json
import html
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import theme
from .session_page import local_time, number


def export_html_reports(paths, destination):
    """Copy summary and its companion, preserving links if the user renames it."""
    source = Path(paths["html"]).resolve()
    target = Path(destination)
    if source == target.resolve():
        return
    summary = source.read_text(encoding="utf-8")
    detail = Path(paths["detail_html"]) if paths.get("detail_html") else None
    if detail is not None and detail.is_file():
        if target.resolve() == detail.resolve():
            raise ValueError("Choose a new summary filename rather than replacing the detailed report.")
        detail_target = target.with_name(target.stem + ".detail.html")
        summary = summary.replace('href="' + html.escape(detail.name) + '"',
                                  'href="' + quote(detail_target.name, safe="") + '"')
        detailed = detail.read_text(encoding="utf-8").replace(
            'href="' + html.escape(source.name) + '"', 'href="' + quote(target.name, safe="") + '"')
        detail_target.write_text(detailed, encoding="utf-8")
    target.write_text(summary, encoding="utf-8")


class SessionHistoryPage(QWidget):
    def __init__(self, data_dir, store, parent=None, *, outbox=None, station_name=lambda: ""):
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self.store = store
        self.outbox = outbox
        self.station_name = station_name
        self._records = []
        self._shown = []
        self._unit_session = None
        self._unit_rows = []
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 10, 0, 0)
        row = QHBoxLayout()
        self.kind = QComboBox()
        self.kind.addItem("Session reports", "sessions")
        self.kind.addItem("Earlier unit reports", "legacy")
        self.kind.currentIndexChanged.connect(self.refresh)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search operator, session, variant or charger serial")
        self.search.textChanged.connect(self._filter)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row.addWidget(self.kind)
        row.addWidget(self.search, 1)
        row.addWidget(refresh)
        body.addLayout(row)

        card = theme.Card("History reports", "One report per operator session")
        card.body.setContentsMargins(18, 12, 18, 12)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Started", "Session / unit", "Operator", "Chargers", "Pass / fail", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._select)
        theme.style_table(self.table, row_h=36)
        card.body.addWidget(self.table, 2)
        self.summary = QLabel("Select a session to see its chargers.")
        self.summary.setWordWrap(True)
        card.body.addWidget(self.summary)
        self.units = QTableWidget(0, 8)
        self.units.setHorizontalHeaderLabels(["Time", "Serial", "Variant", "Calibration", "Post-test", "Result", "Elapsed", "Dashboard"])
        self.units.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.units.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.units.setMinimumHeight(110)
        self.units.itemSelectionChanged.connect(self._sync_actions)
        theme.style_table(self.units, row_h=31)
        card.body.addWidget(self.units, 1)
        body.addWidget(card, 1)
        actions = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        actions.addWidget(self.status, 1)
        self.generate_button = QPushButton("Generate report")
        self.generate_button.setToolTip("Build this serial's EoL report and queue it for the dashboard. Nothing is sent automatically.")
        self.generate_button.clicked.connect(self._generate)
        self.generate_button.setEnabled(False)
        self.open_button = QPushButton("Open complete report")
        self.open_button.setObjectName("Primary")
        self.open_button.clicked.connect(self._open)
        self.export_button = QPushButton("Export report")
        self.export_button.clicked.connect(self._export)
        actions.addWidget(self.generate_button)
        actions.addWidget(self.open_button)
        actions.addWidget(self.export_button)
        body.addLayout(actions)
        self.refresh()

    def refresh(self, *args):
        try:
            if self.kind.currentData() == "sessions":
                self._records = self.store.list_sessions()
            else:
                self._records = []
                folder = self.data_dir / "reports"
                for path in sorted(folder.glob("*.json"), reverse=True):
                    try:
                        record = json.loads(path.read_text(encoding="utf-8"))
                        if isinstance(record, dict) and record.get("run_id"):
                            record["_legacy_json"] = str(path)
                            self._records.append(record)
                    except (OSError, ValueError, TypeError):
                        continue
            self.status.setText("")
            self._filter()
        except Exception as exc:
            self.status.setText("Could not read report history: " + str(exc))

    def _filter(self, *args):
        query = self.search.text().strip().casefold()
        def searchable(record):
            operator = record.get("operator", {})
            if isinstance(operator, dict):
                operator = " ".join(str(operator.get(key, "")) for key in ("id", "name"))
            parts = [str(record.get(key, "")) for key in ("session_id", "run_id", "unit_serial", "started_at", "status", "outcome")]
            parts += [str(operator), str(record.get("recipe", {}).get("name", ""))]
            for unit in record.get("units", []):
                parts += [str(unit.get(key, "")) for key in ("serial", "recipe_name", "outcome")]
            return " ".join(parts).casefold()
        self._shown = [record for record in self._records
                       if not query or query in searchable(record)]
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._shown))
        for row, record in enumerate(self._shown):
            if self.kind.currentData() == "sessions":
                units = record.get("unit_summaries", record.get("units", []))
                operator = record.get("operator", {})
                passed = sum(unit.get("outcome") == "PASS" for unit in units)
                calibrated = sum(unit.get("calibration_completed", unit.get("outcome") == "CALIBRATED") for unit in units)
                failed = sum(unit.get("outcome") in ("FAIL", "ERROR", "INTERRUPTED", "ABORTED", "STOPPED") for unit in units)
                incomplete = sum(unit.get("outcome") == "INCOMPLETE" for unit in units)
                values = (local_time(record.get("started_at")), record.get("session_id", ""),
                          f"{operator.get('name') or operator.get('id', '')}", len(units),
                          f"{passed} / {failed}" + (f" · {calibrated} calibrated" if calibrated else "")
                          + (f" · {incomplete} incomplete" if incomplete else ""),
                          record.get("status", "") + (" · INCOMPLETE" if incomplete else ""))
            else:
                operator = record.get("operator", "")
                if isinstance(operator, dict):
                    operator = operator.get("name") or operator.get("id", "")
                values = (local_time(record.get("started_at")), record.get("unit_serial", record.get("run_id", "")),
                          operator, 1, "—", record.get("outcome", record.get("state", "")))
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
        self.table.blockSignals(False)
        self.units.setRowCount(0)
        self._unit_rows, self._unit_session = [], None
        self.summary.setText(f"{len(self._shown)} report(s). Select a row to view its chargers.")
        self.open_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self._sync_actions()

    def _dashboard_status(self, session, unit):
        if self.outbox is None or session is None or not unit.get("serial"):
            return ""
        item = self.outbox.status_for(session.get("session_id", ""), str(unit["serial"]))
        if item is None:
            return "Not generated"
        if item.stale:
            return "Sent (server kept a newer report)"
        return item.status.capitalize()

    def refresh_status(self, *args):
        """Re-read the dashboard column (after the outbox sends or a report is queued)."""
        for row, unit in enumerate(self._unit_rows):
            cell = self.units.item(row, 7)
            if cell is not None:
                cell.setText(self._dashboard_status(self._unit_session, unit))

    def _sync_actions(self, *args):
        self.generate_button.setEnabled(self.kind.currentData() == "sessions" and self.outbox is not None
                                        and 0 <= self.units.currentRow() < len(self._unit_rows))

    def _selected(self):
        index = self.table.currentRow()
        return self._shown[index] if 0 <= index < len(self._shown) else None

    def _select(self):
        record = self._selected()
        if record is None:
            return
        session = record if self.kind.currentData() == "sessions" else None
        if self.kind.currentData() == "sessions":
            units = record.get("unit_summaries", record.get("units", []))
            self.summary.setText(f"Session {record.get('session_id', '')} · {record.get('status', '')} · "
                                 + self.store.MODES.get(record.get("session_mode"), "Both independently") + " · "
                                 f"Started {local_time(record.get('started_at'))} · "
                                 f"Closed {local_time(record.get('closed_at'))}")
        else:
            units = [{"started_at": record.get("started_at"), "serial": record.get("unit_serial", ""),
                      "recipe_name": record.get("recipe", {}).get("name", ""),
                      "calibration_result": record.get("outcome", "") if record.get("operation") == "calibration" else "—",
                      "test_result": record.get("outcome", "") if record.get("operation") == "test" else "—",
                      "outcome": record.get("outcome", record.get("state", "")),
                      "elapsed_seconds": record.get("elapsed_seconds")}]
            self.summary.setText("Earlier individual unit report · " + str(record.get("run_id", "")))
        self._unit_session, self._unit_rows = session, list(units)
        self.units.setRowCount(len(units))
        for row, unit in enumerate(units):
            values = (local_time(unit.get("started_at")), unit.get("serial", unit.get("unit_serial", "")),
                      unit.get("recipe_name", "") + (f" · {unit['attempt_count']} attempts" if unit.get("attempt_count", 0) > 1 else ""), unit.get("calibration_result", "NOT RUN"),
                      unit.get("test_result", "NOT RUN"), unit.get("outcome", ""),
                      number(unit.get("duration_seconds", unit.get("elapsed_seconds")), "s"),
                      self._dashboard_status(session, unit))
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(unit.get("failure_reason") or unit.get("detail", "")))
                self.units.setItem(row, col, cell)
        self.open_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self._sync_actions()

    def _generate(self):
        try:
            session = self._unit_session
            index = self.units.currentRow()
            if session is None or not 0 <= index < len(self._unit_rows):
                raise ValueError("Select a charger serial first.")
            if self.outbox is None:
                raise ValueError("The dashboard outbox is not available.")
            unit = self._unit_rows[index]
            serial = str(unit.get("serial", ""))
            attempts = [item for item in session.get("units", []) if item.get("serial") == serial]
            if unit.get("outcome") == "RUNNING" or any(item.get("outcome") == "RUNNING" for item in attempts):
                raise ValueError("Wait for this charger to finish before generating its report.")
            if unit.get("simulation") or unit.get("serial_override") or any(
                    item.get("simulation") or item.get("serial_override") for item in attempts):
                raise ValueError("Simulation runs and Other-number serials cannot be sent to the dashboard.")
            station = str(self.station_name() or "").strip()
            if not station:
                raise ValueError("Enter the station name in Settings > Dashboard first.")
            attempt_ids = [item for item in (unit.get("attempt_ids") or [a.get("attempt_id") for a in attempts]) if item]
            if not attempt_ids:
                raise ValueError("This charger has no recorded attempt to report.")
            # Lazy import: report building stays off the module-import path and off the instrument thread.
            from . import eol_report
            from .outbox import report_client_id
            from .outbox_service import report_body
            from .version import VERSION
            generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
            report = eol_report.build_report(session=session, serial=serial, station=station,
                                             now_utc=datetime.now().astimezone())
            html_text = eol_report.render_html(report)
            body = report_body(report, html_text, station=station, generated_at=generated_at)
            client_id = report_client_id(station, session["session_id"], serial, attempt_ids[-1])
            item = self.outbox.write_pending(body, html_text, client_id, session_id=session["session_id"])
            if item.status == "dead":
                item = self.outbox.requeue(client_id)
                message = "Report queued again for the dashboard."
            elif item.status == "sent":
                message = "This report version was already sent to the dashboard."
            else:
                message = "Report queued. It will be sent to the dashboard when the connection is available."
            self.status.setText(f"{serial}: {message}")
        except Exception as exc:
            self.status.setText("Report not generated: " + str(exc))
        self.refresh_status()

    def _paths(self):
        record = self._selected()
        if record is None:
            raise ValueError("Select a report first.")
        if self.kind.currentData() == "sessions":
            return self.store.report_paths(record["session_id"])
        path = Path(record["_legacy_json"]).resolve()
        folder = (self.data_dir / "reports").resolve()
        if path.parent != folder:
            raise ValueError("Invalid earlier report path.")
        return {extension: str(path.with_suffix("." + extension)) for extension in ("json", "csv", "html")}

    def _open(self):
        try:
            path = Path(self._paths()["html"])
            if not path.is_file():
                raise ValueError("This older record has no printable report. Its JSON record can be exported.")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
        except Exception as exc:
            self.status.setText(str(exc))

    def _export(self):
        try:
            paths = self._paths()
            available = [(kind, Path(path)) for kind, path in paths.items() if Path(path).is_file()]
            if not available:
                raise ValueError("No report files are available to export.")
            options = {"html": "Summary and detailed reports (*.html)", "csv": "Summary spreadsheet (*.csv)", "json": "Detailed record (*.json)"}
            default = next((path for kind, path in available if kind == "html"), available[0][1])
            filters = ";;".join(options[kind] for kind, _ in available if kind in options)
            destination, chosen = QFileDialog.getSaveFileName(self, "Export session report", str(default.name), filters)
            if not destination:
                return
            kind = next((key for key, label in options.items() if label == chosen), default.suffix.lstrip("."))
            source = Path(paths[kind]).resolve()
            target = Path(destination)
            if target.suffix.lower() != "." + kind:
                target = target.with_suffix("." + kind)
            if source != target.resolve():
                if kind == "html":
                    export_html_reports(paths, target)
                else:
                    shutil.copyfile(source, target)
            self.status.setText("Report exported: " + str(target)
                + (" · detailed HTML included" if kind == "html" and paths.get("detail_html") and Path(paths["detail_html"]).is_file() else ""))
        except Exception as exc:
            self.status.setText("Export failed: " + str(exc))
