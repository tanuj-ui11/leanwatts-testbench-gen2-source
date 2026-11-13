"""Session reports with read-only access to earlier individual run records."""
from __future__ import annotations

import json
import html
import shutil
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
    def __init__(self, data_dir, store, parent=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self.store = store
        self._records = []
        self._shown = []
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
        self.units = QTableWidget(0, 7)
        self.units.setHorizontalHeaderLabels(["Time", "Serial", "Variant", "Calibration", "Post-test", "Result", "Elapsed"])
        self.units.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.units.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.units.setMinimumHeight(110)
        theme.style_table(self.units, row_h=31)
        card.body.addWidget(self.units, 1)
        body.addWidget(card, 1)
        actions = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        actions.addWidget(self.status, 1)
        self.open_button = QPushButton("Open complete report")
        self.open_button.setObjectName("Primary")
        self.open_button.clicked.connect(self._open)
        self.export_button = QPushButton("Export report")
        self.export_button.clicked.connect(self._export)
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
        self.summary.setText(f"{len(self._shown)} report(s). Select a row to view its chargers.")
        self.open_button.setEnabled(False)
        self.export_button.setEnabled(False)

    def _selected(self):
        index = self.table.currentRow()
        return self._shown[index] if 0 <= index < len(self._shown) else None

    def _select(self):
        record = self._selected()
        if record is None:
            return
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
        self.units.setRowCount(len(units))
        for row, unit in enumerate(units):
            values = (local_time(unit.get("started_at")), unit.get("serial", unit.get("unit_serial", "")),
                      unit.get("recipe_name", "") + (f" · {unit['attempt_count']} attempts" if unit.get("attempt_count", 0) > 1 else ""), unit.get("calibration_result", "NOT RUN"),
                      unit.get("test_result", "NOT RUN"), unit.get("outcome", ""),
                      number(unit.get("duration_seconds", unit.get("elapsed_seconds")), "s"))
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(unit.get("failure_reason") or unit.get("detail", "")))
                self.units.setItem(row, col, cell)
        self.open_button.setEnabled(True)
        self.export_button.setEnabled(True)

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
