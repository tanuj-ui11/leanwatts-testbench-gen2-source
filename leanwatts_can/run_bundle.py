"""Incremental, shareable evidence for one explicitly started diagnostic run."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import threading
import uuid
import zipfile

from .version import VERSION


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def csv_value(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


class RunBundle:
    """Never operates instruments. CSV/JSONL are flushed while a run is active."""

    COLUMNS = ("timestamp", "monotonic", "direction", "operation", "phase", "action",
               "can_id", "extended", "remote", "error", "dlc", "data_hex", "outcome",
               "capture_id", "event_id", "correlates_to", "timestamp_us")

    def __init__(self, folder, payload, recipe, revision, operator, *, simulation=False):
        source = str(payload["program_source"])
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if digest != payload["program_sha256"]:
            raise ValueError("Test program contents changed before the run was frozen.")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        self.folder = Path(folder).resolve() / run_id
        self.folder.mkdir(parents=True, exist_ok=False)
        self._lock = threading.Lock()
        self._closed = False
        self._exported = None
        self.event_count = 0
        self.frame_count = 0
        self.trace_errors = []
        self.metadata = dict(schema=1, run_id=run_id, application="Leanwatts Testbench Gen2",
            version=VERSION, started_at=utc_now(), serial=payload["serial"],
            serial_override=payload["serial_override"], operator=operator,
            recipe_revision=revision, program_filename=Path(payload["program_path"]).name,
            program_sha256=digest, program=payload["program"], simulation=simulation,
            production_acceptance=False, status="RUNNING", timestamp_timezone="UTC")
        (self.folder / "program.json").write_bytes(source.encode("utf-8"))
        write_json(self.folder / "recipe.json", recipe.to_dict())
        write_json(self.folder / "run.json", self.metadata)
        self._events = (self.folder / "can-events.jsonl").open("x", encoding="utf-8", newline="\n")
        self._csv = (self.folder / "can-trace.csv").open("x", encoding="utf-8-sig", newline="")
        self._writer = csv.DictWriter(self._csv, fieldnames=self.COLUMNS)
        self._writer.writeheader()
        self._csv.flush()

    def append(self, event):
        with self._lock:
            if self._closed:
                raise RuntimeError("CAN evidence file is already closed.")
            try:
                self._events.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                self._events.flush()
                row = {key: csv_value(event.get(key)) for key in self.COLUMNS}
                identifier = event.get("id")
                row["can_id"] = f"0x{identifier:08X}" if isinstance(identifier, int) else str(identifier or "")
                row["data_hex"] = " ".join(f"{b:02X}" for b in event.get("data", []))
                self._writer.writerow(row)
                self._csv.flush()
                self.event_count += 1
                self.frame_count += event.get("direction") in ("TX", "RX")
            except Exception as exc:
                self.trace_errors.append(str(exc))
                raise

    def finish(self, result):
        with self._lock:
            if self._exported is not None and self._exported.is_file():
                return self._exported
            if not self._closed:
                self._events.close()
                self._csv.close()
                self._closed = True
            # Closing the capture streams is not successful report export. A
            # failed export can be retried using the preserved evidence folder.
            return self._finish_closed(result)

    def _finish_closed(self, result):
        write_json(self.folder / "result.json", result)
        self.metadata.update(status=result.get("outcome", "ERROR"), finished_at=utc_now(),
            can_events=self.event_count, can_frames=self.frame_count, trace_errors=self.trace_errors,
            capture_scope="Frames received and send attempts made by this application while its CAN handles were open. CAN reopen gaps are not captured.",
            persistence_verified=False, normal_operation_verified=False)
        write_json(self.folder / "run.json", self.metadata)
        sample_rows = []
        activities = []
        for stage in ("calibration", "test"):
            record = result.get(stage) or {}
            for sample in record.get("samples", []):
                sample_rows.append(dict(process=stage, **sample))
            for entry in record.get("events", []):
                activities.append(dict(process=stage, entry=entry))
        columns = ["process", "timestamp", "phase", "voltage", "current"]
        for row in sample_rows:
            columns.extend(key for key in row if key not in columns)
        with (self.folder / "load-readings.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for row in sample_rows:
                writer.writerow({key: csv_value(json.dumps(value) if isinstance(value, (dict, list)) else value)
                                 for key, value in row.items()})
        write_json(self.folder / "activity.json", activities)
        self._summary(result)
        (self.folder / "READ-ME.txt").write_text(
            "Leanwatts Testbench Gen2 diagnostic run\n\n"
            "Send this complete ZIP for analysis. program.json is the exact program used.\n"
            "can-trace.csv includes TX attempts, RX frames and TX_RESULT rows. TX_RESULT is\n"
            "a driver/API result, never a charger acknowledgement. capture_id plus event_id\n"
            "uniquely identifies an event; correlates_to references an event in that same capture.\n"
            "Correlation IDs join attempts\n"
            "and outcomes. can-events.jsonl preserves full raw evidence and adapter timestamps.\n"
            "Capture covers this app's open CAN handles; hardware reopen gaps are not captured.\n"
            "load-readings.csv and result.json include load measurements, stage timing, parameter\n"
            "readbacks, calibration and test results. Monotonic timestamps share the running\n"
            "application's clock; CAN events also carry UTC timestamps.\n"
            "A test program may program parameters and save calibration during its calibration\n"
            "phase. Its testing phase blocks parameter, trim and calibration-save commands.\n"
            "Calibration-mode checks do not certify normal charging or cutoff behaviour.\n"
            "Simulation has no hardware trace and must not be treated as production evidence.\n",
            encoding="utf-8")
        destination = self.folder.with_suffix(".zip")
        if destination.exists():
            raise FileExistsError("A result ZIP already exists at " + str(destination))
        partial = self.folder.with_suffix(".zip.partial")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self.folder.iterdir()):
                if path.is_file():
                    archive.write(path, path.name)
        partial.rename(destination)
        self._exported = destination
        return destination

    def _summary(self, result):
        columns = ("Stage", "Initial", "Final", "Unit", "Seconds", "Result", "Detail")
        rows = []
        for stage in result.get("stage_details", []):
            rows.append([stage.get(key, "") for key in
                ("name", "before", "after", "unit", "duration_seconds", "status", "detail")])
        with (self.folder / "summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow([csv_value(value) for value in
                             ["Charger serial", self.metadata["serial"], "Overall", result.get("outcome", "")]])
            writer.writerow(columns)
            writer.writerows([[csv_value(value) for value in row] for row in rows])
        escape = lambda value: html.escape("—" if value is None else str(value))
        table = "".join("<tr>" + "".join(f"<td>{escape(v)}</td>" for v in row) + "</tr>" for row in rows)
        (self.folder / "summary.html").write_text(
            '<!doctype html><meta charset="utf-8"><title>Leanwatts diagnostic run</title>'
            '<style>body{font:15px Arial;margin:28px;color:#182235}table{border-collapse:collapse;width:100%}'
            'th,td{padding:9px;border:1px solid #ccd3de;text-align:left}th{background:#edf2f8}p{line-height:1.5}</style>'
            f'<h1>Leanwatts Testbench Gen2 · diagnostic run</h1><p>Charger: {escape(self.metadata["serial"])}'
            f'<br>Program: {escape(self.metadata["program_filename"])}<br>Result: <b>{escape(result.get("outcome"))}</b></p>'
            f'<p>{escape(result.get("detail", ""))}</p>'
            '<p>Engineering trial. Normal-operation cutoff and calibration persistence are unverified. '
            'This run does not count as a production acceptance result.</p><table><tr>' +
            "".join(f"<th>{x}</th>" for x in columns) + "</tr>" + table + "</table>", encoding="utf-8")
