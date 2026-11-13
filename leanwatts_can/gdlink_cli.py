"""Documented GD-Link console integration, with frozen inputs and full readback.

Construction and Windows USB discovery never attach to the target MCU. Connecting
the vendor console can halt/reset a target, so jobs require AC-off confirmation.
Vendor binaries remain separately installed; none are shipped with Testbench.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .gdlink import FirmwareImage, GDLinkError, _uint


CLI_VERSION = "2.2.3.41771"
OFFICIAL_DOWNLOAD_URL = "https://www.gd32mcu.com/data/documents/toolSoftware/GD-LinkUtilityProgrammer_win_v2.2.3.41771.7z"
# SHA256 of files read from the official archive, not user-supplied trust values.
TOOL_FILES = {
    "GDLink_CLI.exe": "596f21d4eb730e4103dfbdb838173ab7c8f0ea716c57e779a4c4f1aa19cdb556",
    "GDLink.dll": "57b6e90530e003c9bf6a9490e3c9c9d8a608a6a817af119bbbba7c8c38487a7a",
    "GDDap.dll": "d570f4d19df25021fcf57ecc91bc86c8ec7515554f79d13f1a55e885bd3358a7",
    "GD_MCU_DLL.dll": "4a7446567340939462e7f50d25af3372e8548dcb7f125a943fd3cbd1d81ca751",
    "libusb-1.0.dll": "790413e0335732e2d8b38412f5fad778f9d321461c9b7cf3b4a4fedea610d39b",
    "Qt5Core.dll": "b967ade09a9338320e0db4e5da11a2ac396950f0eed689b28bd31686b7baf018",
    "msvcp140.dll": "51398691feef7ae0a876b523aec47c4a06d9a1ee62f1a0aee27de6d6191c68ad",
    "vcruntime140.dll": "9faeaa45e8cc986af56f28350b38238b03c01c355e9564b849604b8d690919c5",
}


@dataclass(frozen=True)
class CLITarget:
    identity: str
    serial_number: str
    description: str = "GD-Link V3"

    def to_dict(self):
        return dict(identity=self.identity, serial_number=self.serial_number, description=self.description)


@dataclass(frozen=True)
class CLIExecution:
    returncode: int | None
    stdout: str
    timed_out: bool = False
    process_running: bool = False


_USB_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$rows = @(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match '^USB\\VID_28E9&PID_0698\\[^\\]+$' } | ForEach-Object {
  [PSCustomObject]@{ identity=$_.InstanceId; description=$_.FriendlyName; status=$_.Status }
})
ConvertTo-Json -InputObject $rows -Compress
"""


def _windows_usb():
    if sys.platform != "win32":
        raise GDLinkError("GD-Link USB discovery is available on Windows.")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _USB_SCRIPT],
        capture_output=True, text=True, encoding="utf-8-sig", errors="replace", timeout=12,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise GDLinkError("Windows could not identify the GD-Link USB device: " + result.stderr.strip()[:300])
    try:
        rows = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise GDLinkError("Windows returned an invalid USB device list.") from exc
    if not isinstance(rows, list):
        raise GDLinkError("Windows did not return a USB device list.")
    return rows


def discover_programmers():
    """Inspect only present USB parent identities; no vendor process or MCU I/O."""
    targets = []
    for row in _windows_usb():
        identity = str(row.get("identity", "")).upper()
        match = re.fullmatch(r"USB\\VID_28E9&PID_0698\\([A-Z0-9]{6,64})", identity)
        if not match:
            continue
        if str(row.get("status", "")).casefold() != "ok":
            continue
        targets.append(CLITarget(identity, match[1], str(row.get("description") or "GD-Link V3")))
    if len({target.identity for target in targets}) != len(targets):
        raise GDLinkError("Windows reported duplicate GD-Link identities.")
    return targets


def inspect_cli_tool(path):
    """Check a separately installed official tool without executing it."""
    path = Path(path).resolve()
    if path.name.casefold() != "gdlink_cli.exe":
        raise GDLinkError("Select GDLink_CLI.exe from the official GigaDevice 2.2.3.41771 folder.")
    hashes = {}
    for name, expected in TOOL_FILES.items():
        candidate = path.parent / name
        if not candidate.is_file() or candidate.stat().st_size > 100 * 1024 * 1024:
            raise GDLinkError("The official programmer folder is incomplete: " + name)
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != expected:
            raise GDLinkError("The programmer file does not match the supported official version: " + name)
        hashes[name] = actual
    return dict(cli_path=str(path), cli_sha256=hashes["GDLink_CLI.exe"], cli_version=CLI_VERSION,
                tool_files=hashes)


def validate_profile(profile):
    if not isinstance(profile, dict) or profile.get("approved") is not True:
        raise GDLinkError("An administrator must approve the firmware setup.")
    mcu = str(profile.get("declared_mcu", "")).strip().upper()
    if not re.fullmatch(r"GD32[A-Z0-9]{5,24}", mcu):
        raise GDLinkError("Enter the complete expected GD32 part number.")
    start = _uint(profile.get("flash_start"), "Flash start")
    if start != 0x08000000:
        raise GDLinkError("This full internal-flash programming route requires start address 0x08000000.")
    if start != 0x08000000:
        raise GDLinkError("This verified route programs internal flash from 0x08000000 only.")
    size = profile.get("flash_size_kib")
    if type(size) is not int or not 1 <= size <= 4096 or start + size * 1024 > 0x100000000:
        raise GDLinkError("Flash size must be an explicit 1–4096 KiB span.")
    speed = profile.get("speed_khz", 1000)
    if type(speed) is not int or not 100 <= speed <= 10000:
        raise GDLinkError("SWD speed must be 100–10000 kHz.")
    serial = str(profile.get("programmer_serial", "")).upper()
    if not re.fullmatch(r"[A-Z0-9]{6,64}", serial):
        raise GDLinkError("Select an identified GD-Link USB serial number.")
    if (profile.get("cli_version") != CLI_VERSION or profile.get("cli_sha256") != TOOL_FILES["GDLink_CLI.exe"]
            or profile.get("tool_files") != TOOL_FILES or not isinstance(profile.get("cli_path"), str)):
        raise GDLinkError("Select and validate the supported official GD-Link console first.")
    return dict(backend="gdlink_cli", approved=True, declared_mcu=mcu, flash_start=start,
                flash_size_kib=size, speed_khz=speed, programmer_serial=serial,
                cli_path=profile["cli_path"], cli_sha256=profile["cli_sha256"],
                cli_version=CLI_VERSION, tool_files=dict(TOOL_FILES))


def _image_from_bytes(name, data, profile):
    profile = validate_profile(profile)
    if Path(name).suffix.lower() != ".bin":
        raise GDLinkError("This verified programming route currently requires a complete .bin image.")
    if not isinstance(data, bytes) or len(data) != profile["flash_size_kib"] * 1024:
        raise GDLinkError("The BIN must cover the complete approved flash span for full readback verification.")
    start = profile["flash_start"]
    return FirmwareImage(Path(name).name, data, hashlib.sha256(data).hexdigest(), "BIN",
                         ((start, start + len(data)),), len(data))


def inspect_firmware(path, profile):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise GDLinkError("Select a complete firmware BIN of at most 4 MiB.")
    return _image_from_bytes(path.name, path.read_bytes(), profile)


def command_file(profile, phase):
    """Only fixed documented commands; never interpolate filenames or free text."""
    p = validate_profile(profile)
    address, count = f'{p["flash_start"]:X}', f'{p["flash_size_kib"] * 1024:X}'
    # 2.2.3 auto-connects the selected adapter. An additional Connect returned
    # exit 33 ('already connected') on the supervised production-target probe.
    lines = ["c " + p["programmer_serial"], "si 1"]
    # The shipped PDF's filename comma is literal in 2.2.3 (it created
    # backup.bin,). Whitespace syntax was subsequently verified on the target.
    if phase == "backup":
        lines.append(f"savebin backup.bin {address} {count}")
    elif phase == "program":
        lines.extend([f"load input.bin {address}", f"savebin readback.bin {address} {count}"])
    else:
        raise GDLinkError("Unknown programming phase.")
    return "\n".join(lines + ["q", ""])


def parse_identity(output):
    """Require an explicit detected MCU field, not merely echoed commands."""
    patterns = [r"(?:MCU\s+Part\s+No\.?|Device\s+name|Target\s+MCU|Target\s+Device|Chip\s+name)\s*[:=]\s*(GD32[A-Z0-9]+)\b"]
    values = {m.upper() for pattern in patterns for m in re.findall(pattern, output, re.IGNORECASE)}
    if len(values) != 1:
        raise GDLinkError("The console did not report one unambiguous detected MCU part number.")
    return values.pop()


def validate_connected_identity(output, profile):
    detected = parse_identity(output)
    if detected != profile["declared_mcu"]:
        raise GDLinkError("Detected MCU " + detected + " does not match approved " + profile["declared_mcu"] + ".")
    serials = {value.upper() for value in re.findall(r"Current\s+SN\s*[:=]?\s+([A-Z0-9]{6,64})\b", output, re.IGNORECASE)}
    if serials != {profile["programmer_serial"]}:
        raise GDLinkError("The console did not confirm the approved programmer serial number.")
    sizes = {int(value) for value in re.findall(r"Flash\s+Size\s*:\s*(\d+)\s*K(?:i)?B\b", output, re.IGNORECASE)}
    if sizes != {profile["flash_size_kib"]}:
        raise GDLinkError("The detected MCU flash size does not match the complete approved BIN span.")
    if not re.search(r"(?m)^Connect(?:ed)?\s+successfully\.\s*$", output, re.IGNORECASE):
        raise GDLinkError("The console did not confirm connection to the target MCU.")
    return detected


def load_succeeded(output):
    # Exact positive evidence from the supervised 2.2.3.41771 programming run.
    return bool(re.search(r"(?m)^Memory write successfully\.\s+Total time needed:", output))


def exit_description(code):
    if code == 0:
        return "Success"
    names = {1: "Failure", 2: "Target not connected", 4: "Invalid command file", 8: "Invalid BIN",
             16: "Invalid command", 32: "Command failed"}
    if not isinstance(code, int) or code < 0:
        return "Console did not finish normally"
    return "; ".join(name for bit, name in names.items() if code & bit) or f"Unknown console exit code {code}"


_process_lock = threading.Lock()
_pending_processes = []


def console_process_running():
    """Keep the station interlock active after a timed-out console is retained."""
    with _process_lock:
        _pending_processes[:] = [process for process in _pending_processes if process.poll() is None]
        return bool(_pending_processes)


def _check_pending_processes():
    if console_process_running():
        raise GDLinkError("A previous GD-Link console is still running. Wait for it to finish before another attempt.")


def _run_console(args, *, cwd, timeout, output_path):
    """A deadline does not kill a possibly programming process or claim rollback."""
    _check_pending_processes()
    output_path = Path(output_path)
    with output_path.open("xb") as stream:
        process = subprocess.Popen(args, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=stream,
            stderr=subprocess.STDOUT, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with _process_lock:
                _pending_processes.append(process)
            stream.flush()
            return CLIExecution(None, output_path.read_bytes().decode("utf-8", errors="replace"), True, True)
    return CLIExecution(code, output_path.read_bytes().decode("utf-8", errors="replace"))


def _workspace():
    path = Path(tempfile.mkdtemp(prefix="lwgdlink-"))
    # Commands use fixed relative names; the command-file argument is one argv
    # value. Keep even that path free from spaces because vendor quote parsing
    # is not documented for command files.
    if re.search(r"\s", str(path)):
        if os.name == "nt":
            import ctypes
            buf = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf)):
                path = Path(buf.value)
        if re.search(r"\s", str(path)):
            path.rmdir()
            raise GDLinkError("The temporary folder needs a path without spaces for the vendor console.")
    return path


class ProgrammingJob:
    """One backed-up, explicitly requested program attempt; never automatic retry.

    Identity is checked during preflight, the same USB serial is checked again
    immediately before programming, and returned identity is checked afterward.
    The operator must keep the target lead attached throughout: a static vendor
    command file cannot conditionally reject an MCU swapped between sessions.
    """
    def __init__(self, target, profile, image, *, cancel=None, event_sink=None, evidence_dir=None,
                 ac_off_confirmed=False, discovery=None, runner=None, tool_inspector=None,
                 workspace_factory=None, timeout=120, clock=None):
        self.profile = validate_profile(profile)
        if not isinstance(target, CLITarget) or target.serial_number != self.profile["programmer_serial"]:
            raise GDLinkError("Select the GD-Link USB device approved for this variant.")
        if not isinstance(image, FirmwareImage) or _image_from_bytes(image.name, image.data, profile) != image:
            raise GDLinkError("The approved frozen firmware image changed.")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 5 <= timeout <= 600:
            raise GDLinkError("Console timeout must be 5–600 seconds.")
        self.target, self.image, self.timeout = target, image, timeout
        self.cancel = cancel if cancel is not None else threading.Event()
        self.event_sink, self.evidence_dir = event_sink, Path(evidence_dir) if evidence_dir else None
        self.ac_off_confirmed = ac_off_confirmed is True
        self.discovery, self.runner = discovery or discover_programmers, runner or _run_console
        self.tool_inspector, self.workspace_factory = tool_inspector or inspect_cli_tool, workspace_factory or _workspace
        self.clock = clock or time.monotonic
        self.events, self.logging_errors, self.executions = [], [], []
        self.submitted = self.verified = self.target_verified = self.process_running = False
        self._ran = False
        self.started_at = None
        self._started = 0.0
        self.work = None
        self.evidence_copied = False
        self.backup_sha256 = self.readback_sha256 = None

    def _event(self, phase, detail):
        event = dict(timestamp=datetime.now(timezone.utc).isoformat(), phase=phase, detail=detail,
                     submitted=self.submitted, elapsed_seconds=round(max(0, self.clock() - self._started), 3))
        self.events.append(event)
        if self.event_sink:
            try:
                self.event_sink(copy.deepcopy(event))
            except Exception as exc:
                self.logging_errors.append(str(exc))
                if not self.submitted:
                    raise GDLinkError("Could not record programming evidence: " + str(exc)) from exc

    def _connected(self):
        targets = self.discovery()
        matches = [t for t in targets if isinstance(t, CLITarget) and t.identity == self.target.identity
                   and t.serial_number == self.target.serial_number]
        if len(matches) != 1:
            raise GDLinkError("The approved USB programmer is absent or ambiguous.")

    def _tool(self):
        actual = self.tool_inspector(self.profile["cli_path"])
        if any(actual.get(key) != self.profile[key] for key in ("cli_sha256", "cli_version", "tool_files")):
            raise GDLinkError("The vendor tool changed after firmware setup was approved.")

    def _phase(self, phase):
        script = self.work / (phase + ".gdlink")
        script.write_text(command_file(self.profile, phase), encoding="ascii", newline="\n")
        args = [self.profile["cli_path"], "-speed", str(self.profile["speed_khz"]),
                "-commandfile", "-e", str(script)]
        result = self.runner(args, cwd=self.work, timeout=self.timeout,
                             output_path=self.work / (phase + ".log"))
        self.process_running = bool(result.process_running)
        self.executions.append(dict(phase=phase, returncode=result.returncode, stdout=result.stdout,
                                    timed_out=result.timed_out, process_running=result.process_running))
        if result.timed_out or result.process_running:
            raise GDLinkError("The console timed out; it was not terminated. Its target state is unconfirmed.")
        if result.returncode != 0:
            raise GDLinkError(exit_description(result.returncode) + ". No automatic retry was made.")
        validate_connected_identity(result.stdout, self.profile)
        self.target_verified = True
        return result

    def _read_exact(self, name):
        path = self.work / name
        if not path.is_file() or path.stat().st_size != len(self.image.data):
            raise GDLinkError("The console did not produce a fresh complete " + name + ".")
        return path.read_bytes()

    def _result(self, status, detail):
        return dict(status=status, outcome=status, detail=detail, error=detail if status in ("FAILED", "UNCONFIRMED") else "",
                    backend="gdlink_cli", started_at=self.started_at,
                    finished_at=datetime.now(timezone.utc).isoformat(), submitted=self.submitted,
                    elapsed_seconds=round(max(0, self.clock() - self._started), 3),
                    programmer=self.target.to_dict(), firmware=self.image.to_dict(),
                    profile=copy.deepcopy(self.profile), target_mcu_verified=self.target_verified,
                    independent_readback_verified=self.verified, independent_flash_verification=self.verified,
                    backup_sha256=self.backup_sha256, readback_sha256=self.readback_sha256,
                    process_running=self.process_running, console_closed=not self.process_running,
                    disconnect_acknowledged=False, calibration_preservation_verified=False,
                    ac_off_confirmed=self.ac_off_confirmed, executions=copy.deepcopy(self.executions),
                    events=copy.deepcopy(self.events), logging_errors=list(self.logging_errors),
                    evidence_folder=str(self.evidence_dir if self.evidence_copied else self.work) if self.work else None,
                    working_directory=str(self.work) if self.work else None,
                    target_connection_must_remain_unchanged=True)

    def run(self):
        if self._ran:
            raise GDLinkError("This programming job was already attempted; automatic retry is prohibited.")
        self._ran = True
        self._started = self.clock()
        self.started_at = datetime.now(timezone.utc).isoformat()
        status, detail = "FAILED", "Programming did not start."
        try:
            if not self.ac_off_confirmed:
                raise GDLinkError("Confirm charger AC is OFF before attaching the programmer.")
            _check_pending_processes()
            if self.cancel.is_set():
                return self._result("CANCELLED", "Cancelled before connecting the target.")
            self._tool()
            self._connected()
            self.work = Path(self.workspace_factory())
            self.work.mkdir(parents=True, exist_ok=True)
            if any(self.work.iterdir()):
                raise GDLinkError("Programming needs a fresh empty working folder.")
            (self.work / "input.bin").write_bytes(self.image.data)
            self._event("backup", "Connecting to the expected chip and saving its complete current firmware before programming.")
            self._phase("backup")
            self.backup_sha256 = hashlib.sha256(self._read_exact("backup.bin")).hexdigest()
            if self.cancel.is_set():
                status, detail = "CANCELLED", "Cancelled after backup, before firmware submission."
            else:
                self._connected()
                self._tool()
                if self.cancel.is_set():
                    status, detail = "CANCELLED", "Cancelled before firmware submission."
                else:
                    self._event("ready_to_submit", "Backup saved. Programming once, then reading back the complete flash for comparison.")
                    if self.cancel.is_set():
                        status, detail = "CANCELLED", "Cancelled before firmware submission."
                    else:
                        self.submitted = True
                        self._event("programming", "Programming has started. Stop cannot interrupt this attempt; waiting for verification.")
                        result = self._phase("program")
                        readback = self._read_exact("readback.bin")
                        self.readback_sha256 = hashlib.sha256(readback).hexdigest()
                        if not load_succeeded(result.stdout):
                            raise GDLinkError("The vendor did not explicitly confirm programming success.")
                        if readback != self.image.data or self.readback_sha256 != self.image.sha256:
                            raise GDLinkError("Flash readback differs from the approved firmware image.")
                        self.verified = True
                        status, detail = "VERIFIED", "Programming finished and the complete flash readback matches the approved firmware. Console closed."
        except Exception as exc:
            status = "UNCONFIRMED" if self.submitted or self.process_running else "FAILED"
            detail = str(exc)
        finally:
            if self.work and self.evidence_dir:
                try:
                    self.evidence_dir.mkdir(parents=True, exist_ok=True)
                    for path in self.work.iterdir():
                        if path.is_file():
                            shutil.copy2(path, self.evidence_dir / path.name)
                    self.evidence_copied = True
                except Exception as exc:
                    self.logging_errors.append("Evidence copy: " + str(exc))
        return self._result(status, detail)
