"""Separate GD-Link programming workspace; never runs as part of calibration."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import threading
import time
import uuid

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QDialog

from .account_settings import LoginDialog
from .gdlink_cli import (discover_programmers, inspect_cli_tool, inspect_firmware,
                        validate_profile, ProgrammingJob, console_process_running)
from .recipe_page import atomic_json
from .serial_numbers import validate_serial
from .version import VERSION
from .workspace_identity import workspace_context, resolve_operator, sign_in


def recipe_fingerprint(recipe):
    return hashlib.sha256(json.dumps(recipe.to_dict(), sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


class FirmwareController(QObject):
    active_changed = Signal(bool)

    def __init__(self, window, page):
        super().__init__(window)
        self.window, self.page = window, page
        self._operator = None
        self.profile_path = window.data_dir / "firmware_profiles.json"
        self._profiles = {}
        self._profile_error = ""
        try:
            if self.profile_path.exists():
                value = json.loads(self.profile_path.read_text(encoding="utf-8"))
                if (not isinstance(value, dict) or value.get("schema") != 1
                        or not isinstance(value.get("profiles"), dict)
                        or any(not isinstance(k, str) or not isinstance(v, dict) for k, v in value["profiles"].items())):
                    raise ValueError("Unsupported firmware profile file.")
                self._profiles = value["profiles"]
        except (ValueError, OSError) as exc:
            self._profile_error = str(exc)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gdlink-programming")
        self._future = None
        self._run_started = None
        self._console_wait = False
        self._kind = None
        self._cancel = threading.Event()
        self._events = queue.Queue()
        self._submitted = threading.Event()
        self._targets = []
        self._closing = False
        self._context_key = None
        self.snapshot = {}
        page.discover_requested.connect(self.discover)
        page.setup_requested.connect(self.setup)
        page.program_requested.connect(self.start)
        page.stop_requested.connect(self.stop)
        page.sign_in_requested.connect(self.sign_in)
        page.context_changed.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    @property
    def busy(self):
        return self._future is not None or self._console_wait

    def _context(self):
        return workspace_context(self.window, self.page, self._operator)

    def sign_in(self):
        if self.busy:
            return
        operator = sign_in(self.window, self.page)
        if operator:
            self._operator = operator
        self.refresh()

    def _set_future(self, kind, future):
        self._kind, self._future = kind, future
        self.page.set_busy(True)
        self.active_changed.emit(True)

    def discover(self):
        if self.busy:
            return
        self.page.set_status("Looking for GD-Link USB adapters. The target chip is not accessed.")
        self._set_future("discover", self._executor.submit(discover_programmers))

    def setup(self):
        if self.busy:
            return
        try:
            recipe, record, _ = self._context()
            target = self.page.selected_target
            if target is None:
                raise ValueError("Connect GD-Link V3 and click Find programmer first.")
            dialog = LoginDialog(self.window.accounts_page.accounts, self.page,
                                 title="Administrator · firmware setup", admin_only=True)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            values = self.page.edit_profile(self._profiles.get(recipe.name), target)
            if values is None:
                return
            self.save_profile(values, recipe, record["revision"], target, dialog.operator)
        except Exception as exc:
            self.page.set_status("Setup not saved: " + str(exc))

    def save_profile(self, values, recipe, revision, target, administrator):
        """Persist only an inspected image and explicitly approved production setup."""
        if not isinstance(administrator, dict) or administrator.get("role") != "admin":
            raise ValueError("An administrator must approve firmware settings.")
        if target is None:
            raise ValueError("Select an identified GD-Link programmer.")
        tool = inspect_cli_tool(values.get("cli_path", ""))
        profile = validate_profile(dict(values, **tool, programmer_serial=target.serial_number))
        image = inspect_firmware(Path(values["firmware_path"]), profile)
        folder = self.window.data_dir / "firmware_images"
        folder.mkdir(parents=True, exist_ok=True)
        stored = folder / (image.sha256 + Path(image.name).suffix.lower())
        if stored.exists():
            if hashlib.sha256(stored.read_bytes()).hexdigest() != image.sha256:
                raise ValueError("The station's stored firmware does not match its approved hash.")
        else:
            stored.write_bytes(image.data)
        saved = dict(profile, firmware_path=str(stored.resolve()), firmware_name=image.name,
            firmware_sha256=image.sha256, variant=recipe.name, recipe_revision=revision,
            recipe_fingerprint=recipe_fingerprint(recipe), target_identity=target.identity,
            approval_scope="firmware_for_variant",
            programmer_serial=target.serial_number, approved_by=administrator["id"],
            approved_at=datetime.now(timezone.utc).isoformat(), target_mcu_verified=False)
        updated = copy.deepcopy(self._profiles)
        updated[recipe.name] = saved
        atomic_json(self.profile_path, dict(schema=1, profiles=updated))
        self._profiles = updated
        self._context_key = None
        self._profile_error = ""
        self.page.set_profile(saved)
        self.page.set_status("Firmware approved for " + recipe.name + ". Each run checks the connected chip and verifies the full flash readback.")

    def start(self, payload):
        if self.busy or self._closing:
            return
        try:
            recipe, record, operator = self._context()
            reason = self.window._workspace_busy_reason("firmware")
            if reason:
                raise ValueError(reason)
            if self.window.legacy_page.live_mode == "simulation":
                raise ValueError("Firmware programming is disabled in simulation mode.")
            if self.window.load_setup.busy:
                raise ValueError("Wait for the load connection operation to finish.")
            if self.window.legacy_page.device_open or self.window.legacy_page._live_future is not None:
                raise ValueError("Close the manual CAN device before firmware programming.")
            if payload.get("ac_off_confirmed") is not True or payload.get("fixture_power_confirmed") is not True:
                raise ValueError("Confirm charger AC is OFF and the approved programming fixture powers the MCU.")
            error = validate_serial(payload.get("serial", ""), allow_custom=payload.get("serial_override", False))
            if error:
                raise ValueError(error)
            profile = copy.deepcopy(self._profiles.get(recipe.name))
            if not profile or profile.get("backend") != "gdlink_cli" or profile.get("variant") != recipe.name:
                raise ValueError("Configure and approve firmware for this variant first.")
            target = payload.get("target")
            if target is None or target.identity != profile.get("target_identity"):
                raise ValueError("Select the GD-Link programmer approved for this variant.")
            self._cancel.clear()
            self._submitted.clear()
            self._run_started = time.monotonic()
            self.snapshot = dict(state="Checking firmware", detail="Checking the approved image and GD-Link identity.", submitted=False)
            self.page.update_snapshot(self.snapshot)
            self._set_future("program", self._executor.submit(self._program_worker,
                target, profile, copy.deepcopy(payload), operator, record["revision"]))
        except Exception as exc:
            self.page.set_status("Programming not started: " + str(exc))

    def _program_worker(self, target, profile, payload, operator, revision):
        folder = self.window.data_dir / "firmware_runs" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
        folder.mkdir(parents=True, exist_ok=False)
        started = datetime.now(timezone.utc).isoformat()
        events = []
        log_errors = []
        result = None
        with (folder / "activity.jsonl").open("x", encoding="utf-8", newline="\n") as log:
            def event_sink(event):
                event = copy.deepcopy(event) if isinstance(event, dict) else {"detail": str(event)}
                event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
                if event.get("submitted"):
                    self._submitted.set()
                events.append(event)
                self._events.put(event)
                try:
                    log.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                    log.flush()
                except OSError as exc:
                    log_errors.append(str(exc))
                    if not self._submitted.is_set():
                        raise
                    # Submitted programming cannot be undone. Keep observing
                    # the outcome even if logging becomes unavailable.
            try:
                validated = validate_profile(profile)
                image = inspect_firmware(Path(profile["firmware_path"]), validated)
                if image.sha256 != profile["firmware_sha256"]:
                    raise ValueError("Firmware image changed after administrator approval.")
                result = ProgrammingJob(target, validated, image, cancel=self._cancel,
                    event_sink=event_sink, evidence_dir=folder,
                    ac_off_confirmed=payload.get("ac_off_confirmed") is True and payload.get("fixture_power_confirmed") is True).run()
            except Exception as exc:
                result = dict(status="UNCONFIRMED" if self._submitted.is_set() else "FAILED",
                              error=str(exc), submitted=self._submitted.is_set())
        result.setdefault("independent_readback_verified", False)
        result.setdefault("target_mcu_verified", False)
        result.setdefault("disconnect_acknowledged", False)
        result = dict(result, application_version=VERSION, serial=payload["serial"],
            serial_override=payload.get("serial_override", False), operator=operator,
            variant=profile["variant"], recipe_revision=revision, approved_profile=profile,
            started_at=started, finished_at=datetime.now(timezone.utc).isoformat(),
            events=events, log_errors=log_errors, calibration_started=False,
            ac_off_confirmed=payload.get("ac_off_confirmed") is True,
            fixture_power_confirmed=payload.get("fixture_power_confirmed") is True)
        path = folder / "result.json"
        try:
            atomic_json(path, result)
            result["report_path"] = str(path)
        except OSError as exc:
            result["export_error"] = str(exc)
            result["evidence_folder"] = str(folder)
        return result

    def stop(self):
        if self._console_wait:
            self.page.set_status("The vendor programmer is still running. Keep it connected; other operations remain locked.")
            return
        if self.busy and self._kind == "program":
            if self._submitted.is_set():
                self.page.set_status("Firmware was submitted. Programming cannot be interrupted; waiting for the programmer's result.")
            else:
                self._cancel.set()
                self.page.set_status("Cancellation requested before firmware submission.")

    def refresh(self):
        if self._closing:
            return
        if self._console_wait and not console_process_running():
            self._console_wait = False
            self.page.set_busy(False)
            self.active_changed.emit(False)
            self.page.set_status("The vendor console has closed. The programming result remains unconfirmed; review the saved report.")
        while not self._events.empty():
            event = self._events.get_nowait()
            self.snapshot.update(event)
            self.snapshot["state"] = event.get("phase", event.get("state", "Programming"))
            self.snapshot["submitted"] = self._submitted.is_set()
            self.page.update_snapshot(self.snapshot)
        if self._future is not None and self._kind == "program" and self._run_started is not None:
            self.page.elapsed.set(f"{max(0.0, time.monotonic() - self._run_started):.1f}")
        if self._future is not None and self._future.done():
            future, kind = self._future, self._kind
            self._future = self._kind = None
            try:
                result = future.result()
                self._console_wait = bool(kind == "program" and result.get("process_running") is True)
                self.page.set_busy(self._console_wait)
                self.active_changed.emit(self._console_wait)
                if kind == "discover":
                    self._targets = result
                    self.page.set_targets(result)
                    self.page.set_status(f"Found {len(result)} GD-Link programmer(s)." if result else
                        "No GD-Link programmer found. Connect GD-Link V3 by USB and check its Windows driver.")
                else:
                    self.snapshot = dict(result, state=result["status"], detail=result.get("error") or result.get("detail") or
                        "Programming result recorded. Calibration has not been started.")
                    self.page.update_snapshot(self.snapshot)
                    if result.get("report_path"):
                        self.page.set_report(result["report_path"])
                    if result.get("export_error"):
                        self.page.set_status("Result file could not be saved: " + result["export_error"])
                    if self._console_wait:
                        self.page.set_status("Result unconfirmed; vendor programmer still running. Keep it connected. Other operations remain locked.")
            except Exception as exc:
                self.page.set_busy(False)
                self.active_changed.emit(False)
                self.page.set_status(str(exc))
        recipe = operator = None
        readiness = self._profile_error
        try:
            recipe, _, operator = self._context()
            if not self.busy:
                readiness = self.window._workspace_busy_reason("firmware") or readiness
            key = recipe.name
            if key != self._context_key:
                self._context_key = key
                profile = self._profiles.get(recipe.name)
                self.page.set_profile(profile if profile and profile.get("backend") == "gdlink_cli"
                                      and profile.get("variant") == recipe.name else None)
            if self.window.legacy_page.live_mode == "simulation":
                readiness = "Firmware programming is disabled in simulation mode."
        except (ValueError, TypeError, KeyError, OSError) as exc:
            readiness = str(exc)
            self.page.set_profile(None)
            self._context_key = None
            try:
                operator = resolve_operator(self.window, self._operator)
            except (ValueError, OSError):
                operator = None
        self.page.refresh_context(recipe, operator,
            busy=bool(self.window._workspace_busy_reason("firmware")), readiness=readiness)

    def dispose(self):
        self._closing = True
        self.timer.stop()
        if not self._submitted.is_set():
            self._cancel.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
