"""GD-Link V3 virtual-disk programming; construction never accesses hardware.

Uses the manufacturer's documented CONFIG.TXT / BIN-or-HEX / remount result
workflow. This is not a guessed Utility CLI or independent flash verification.
See research/gdutility-automation.md for the primary-source boundary.
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
import subprocess
import sys
import threading
import time


MAX_CONFIG_BYTES = 16 * 1024
MAX_FIRMWARE_BYTES = 64 * 1024 * 1024


class GDLinkError(ValueError):
    pass


@dataclass(frozen=True)
class ProgrammerTarget:
    identity: str
    root: Path
    volume_label: str
    serial_number: str
    config_text: str

    def to_dict(self):
        return dict(identity=self.identity, root=str(self.root), volume_label=self.volume_label,
                    serial_number=self.serial_number, config_text=self.config_text)


@dataclass(frozen=True)
class FirmwareImage:
    name: str
    data: bytes
    sha256: str
    format: str
    ranges: tuple[tuple[int, int], ...]  # Absolute start, exclusive end.
    payload_bytes: int

    def to_dict(self):
        return dict(name=self.name, sha256=self.sha256, format=self.format,
                    file_bytes=len(self.data), payload_bytes=self.payload_bytes,
                    ranges=[dict(start=start, end_exclusive=end) for start, end in self.ranges])


def _uint(value, label):
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", value):
            raise GDLinkError(label + " must be an explicit decimal or hexadecimal address.")
        value = int(value, 16 if value.lower().startswith("0x") else 10)
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        raise GDLinkError(label + " must be an unsigned 32-bit address.")
    return value


def parse_config(raw):
    """Parse documented keys only; return values without rewriting the source text."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise GDLinkError("Supply the programmer's UTF-8 CONFIG.TXT (maximum 16 KB).")
    aliases = {"target cpu": "core", "program flash start address": "flash_start",
               "program start address": "flash_start", "erase method": "erase_method",
               "reset method": "reset_method", "read protection": "read_protection",
               "sram start address": "sram_start", "debug interface": "debug_interface"}
    found = {}
    for line in raw.removeprefix("\ufeff").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise GDLinkError("CONFIG.TXT contains an unsupported line: " + line[:100])
        key, value = line.split(":", 1)
        key = aliases.get(key.strip().casefold())
        if key is None:
            raise GDLinkError("CONFIG.TXT contains an undocumented setting; review its actual format.")
        if key in found:
            raise GDLinkError("CONFIG.TXT repeats a setting: " + key)
        found[key] = value.strip()
    required = {"core", "flash_start", "erase_method", "reset_method", "read_protection", "sram_start"}
    if required - found.keys():
        raise GDLinkError("CONFIG.TXT is missing: " + ", ".join(sorted(required - found.keys())))
    choices = {"core": ("ARM", "RISC-V"), "erase_method": ("Page", "Chip"),
               "reset_method": ("Software", "Hardware"), "read_protection": ("Enable", "Disable"),
               "debug_interface": ("SWD", "JTAG")}
    for key, allowed in choices.items():
        if key not in found:
            found[key] = None
            continue
        match = next((item for item in allowed if item.casefold() == found[key].casefold()), None)
        if match is None:
            raise GDLinkError("Unsupported CONFIG.TXT " + key + ".")
        found[key] = match
    found["flash_start"] = _uint(found["flash_start"], "Program flash start")
    found["sram_start"] = _uint(found["sram_start"], "SRAM start")
    if found["debug_interface"] is not None and (found["core"], found["debug_interface"]) not in (
            ("ARM", "SWD"), ("RISC-V", "JTAG")):
        raise GDLinkError("CONFIG.TXT core and debug interface do not match the documented disk workflow.")
    return found


def validate_profile(profile):
    """Approval is explicit; USB defaults alone do not establish production settings.

    flash_size_kib is the approved writable span FROM flash_start. For an
    application offset this must not be the entire physical chip capacity.
    """
    if not isinstance(profile, dict):
        raise GDLinkError("Supply an approved programming profile.")
    if profile.get("approved") is not True:
        raise GDLinkError("An administrator must confirm the existing production programming settings.")
    mcu = profile.get("declared_mcu")
    if not isinstance(mcu, str) or not re.fullmatch(r"GD32[A-Za-z0-9]{3,32}", mcu.strip(), re.IGNORECASE):
        raise GDLinkError("Enter the full GD32 MCU part number shown by the working programmer.")
    raw = profile.get("config_text")
    config = parse_config(raw)
    start = _uint(profile.get("flash_start"), "Approved flash start")
    size = profile.get("flash_size_kib")
    if type(size) is not int or not 1 <= size <= 4194304 or start + size * 1024 > 0x100000000:
        raise GDLinkError("Enter an explicit approved flash span in KiB within the 32-bit address space.")
    if start != config["flash_start"]:
        raise GDLinkError("Approved flash start must match the imported CONFIG.TXT programming address.")
    if profile.get("expected_core", config["core"]) != config["core"]:
        raise GDLinkError("The expected MCU core does not match the approved CONFIG.TXT.")
    return dict(declared_mcu=mcu.strip().upper(), flash_start=start, flash_size_kib=size,
                config_text=raw, config=config, expected_core=config["core"], approved=True)


def _image_from_bytes(name, data, profile):
    profile = validate_profile(profile)
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_FIRMWARE_BYTES:
        raise GDLinkError("Firmware must contain 1 byte to 64 MB of frozen file data.")
    name = Path(name).name
    if (name in ("", ".", "..") or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)
            or name.endswith((" ", "."))
            or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", name.split(".")[0], re.IGNORECASE)):
        raise GDLinkError("Choose a valid firmware filename.")
    suffix = Path(name).suffix.lower()
    low, high = profile["flash_start"], profile["flash_start"] + profile["flash_size_kib"] * 1024
    ranges = []
    payload_bytes = 0
    if suffix == ".bin":
        ranges = [(low, low + len(data))]
        payload_bytes = len(data)
    elif suffix == ".hex":
        try:
            lines = data.decode("ascii").splitlines()
        except UnicodeDecodeError as exc:
            raise GDLinkError("Intel HEX firmware must use ASCII records.") from exc
        base, eof = 0, False
        entry_points = []
        for number, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue
            if eof:
                raise GDLinkError("Intel HEX contains records after its end-of-file record.")
            if not re.fullmatch(r":[0-9a-fA-F]+", line) or len(line) % 2 != 1:
                raise GDLinkError(f"Intel HEX line {number} is malformed.")
            record = bytes.fromhex(line[1:])
            if len(record) < 5 or len(record) != record[0] + 5 or sum(record) & 0xFF:
                raise GDLinkError(f"Intel HEX line {number} has an invalid length or checksum.")
            count, address, kind = record[0], int.from_bytes(record[1:3], "big"), record[3]
            content = record[4:-1]
            if kind == 0:
                if address + count > 0x10000:
                    raise GDLinkError("Intel HEX data crosses a 64 KB address-record boundary.")
                if count:
                    ranges.append((base + address, base + address + count))
                    payload_bytes += count
            elif kind == 1 and count == 0 and address == 0:
                eof = True
            elif kind in (2, 4) and count == 2 and address == 0:
                base = int.from_bytes(content, "big") << (4 if kind == 2 else 16)
            elif kind in (3, 5) and count == 4 and address == 0:
                entry_points.append(((int.from_bytes(content[:2], "big") << 4)
                                     + int.from_bytes(content[2:], "big")) if kind == 3 else
                                    int.from_bytes(content, "big"))
            else:
                raise GDLinkError(f"Intel HEX line {number} uses an unsupported or malformed record type.")
        if not eof or not ranges:
            raise GDLinkError("Intel HEX must contain data and a final end-of-file record.")
        if any(not low <= address < high for address in entry_points):
            raise GDLinkError("Intel HEX entry point is outside the approved flash span.")
    else:
        raise GDLinkError("Choose a .bin or Intel .hex firmware file.")
    merged = []
    for start, end in sorted(ranges):
        if not low <= start < end <= high:
            raise GDLinkError("Firmware data is outside the approved flash start/span; nothing was programmed.")
        if merged and start < merged[-1][1]:
            raise GDLinkError("Intel HEX contains overlapping data records.")
        if merged and start == merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return FirmwareImage(name, data, hashlib.sha256(data).hexdigest(), suffix[1:].upper(),
                         tuple(merged), payload_bytes)


def inspect_firmware(path, profile):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_FIRMWARE_BYTES:
        raise GDLinkError("Select a firmware file of at most 64 MB.")
    with path.open("rb") as stream:
        data = stream.read(MAX_FIRMWARE_BYTES + 1)
    return _image_from_bytes(path.name, data, profile)


_DISCOVERY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$rows = @()
Get-CimInstance Win32_DiskDrive | Where-Object { $_.InterfaceType -eq 'USB' -or $_.PNPDeviceID -like 'USBSTOR\*' } | ForEach-Object {
    $disk = $_
    Get-CimAssociatedInstance -InputObject $disk -Association Win32_DiskDriveToDiskPartition | ForEach-Object {
        Get-CimAssociatedInstance -InputObject $_ -Association Win32_LogicalDiskToPartition | ForEach-Object {
            $rows += [PSCustomObject]@{ root = ($_.DeviceID + '\'); volume_label = $_.VolumeName; pnp_id = $disk.PNPDeviceID; serial_number = $disk.SerialNumber; interface = $disk.InterfaceType }
        }
    }
}
ConvertTo-Json -InputObject @($rows) -Compress
"""


def _windows_disks():
    if sys.platform != "win32":
        raise GDLinkError("GD-Link USB volume discovery is available on Windows.")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _DISCOVERY_SCRIPT],
        capture_output=True, text=True, encoding="utf-8-sig", errors="replace", timeout=12,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise GDLinkError("Could not identify USB programmer volumes: " + result.stderr.strip()[:300])
    try:
        rows = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise GDLinkError("Windows USB volume discovery returned an invalid result.") from exc
    if not isinstance(rows, list):
        raise GDLinkError("Windows USB volume discovery did not return a device list.")
    return rows


def _read_config(root):
    with (Path(root) / "CONFIG.TXT").open("rb") as stream:
        data = stream.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        raise GDLinkError("Programmer CONFIG.TXT exceeds 16 KB.")
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GDLinkError("Programmer CONFIG.TXT is not UTF-8.") from exc
    parse_config(raw)
    return raw


def discover_programmers():
    """Read-only discovery; a drive letter or label alone never identifies a target."""
    candidates = []
    for row in _windows_disks():
        if not isinstance(row, dict) or str(row.get("volume_label", "")).strip().casefold() != "gigadevice":
            continue
        root, pnp = str(row.get("root", "")), str(row.get("pnp_id", "")).strip()
        if not re.fullmatch(r"[A-Za-z]:\\", root):
            raise GDLinkError("GigaDevice volume has an invalid drive path.")
        if not re.fullmatch(r"(?:USBSTOR|USB)\\[^\\]+\\[^\\]+", pnp, re.IGNORECASE):
            raise GDLinkError("GigaDevice volume lacks a unique USB PNP device identity.")
        try:
            raw = _read_config(root)
        except (OSError, GDLinkError) as exc:
            raise GDLinkError("GigaDevice volume CONFIG.TXT could not be qualified: " + str(exc)) from exc
        candidates.append(ProgrammerTarget(pnp.upper(), Path(root), str(row["volume_label"]),
                                           str(row.get("serial_number") or "").strip(), raw))
    identities = [item.identity for item in candidates]
    if len(identities) != len(set(identities)):
        raise GDLinkError("One USB programmer maps to multiple volumes; select an unambiguous fixture.")
    return candidates


def _write_file(path, data, *, exclusive=False):
    with Path(path).open("xb" if exclusive else "wb") as stream:
        written = stream.write(data)
        if written != len(data):
            raise OSError("The programmer did not accept the complete file.")
        stream.flush()
        os.fsync(stream.fileno())


class _DriveAbsenceObserver:
    """Read the Windows logical-drive bitmask every 20 ms during submission.

    No path is opened or written by this observer. It preserves a brief removal
    that might occur while the firmware copy blocks the programming worker.
    A present drive is not identity evidence; CIM must qualify the return.
    """
    def __init__(self, root, *, mask_reader=None):
        text = str(root)
        self._bit = 1 << (ord(text[0].upper()) - ord("A")) if re.fullmatch(r"[A-Za-z]:\\", text) else None
        self._reader = mask_reader
        self._stop = threading.Event()
        self._submitted = threading.Event()
        self._absent = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self.error = ""
        self.available = False

    def start(self):
        if self._bit is None or (sys.platform != "win32" and self._reader is None):
            self.error = "Fast drive observation is unavailable for this volume."
            return False
        if self._reader is None:
            import ctypes
            function = ctypes.WinDLL("kernel32", use_last_error=True).GetLogicalDrives
            function.restype = ctypes.c_uint32
            self._reader = function
        self._thread = threading.Thread(target=self._watch, name="gdlink-volume-observer", daemon=True)
        self._thread.start()
        self._ready.wait(1)
        self.available = self._ready.is_set() and not self.error
        return self.available

    def _watch(self):
        try:
            while not self._stop.is_set():
                submitted_before_read = self._submitted.is_set()
                mask = self._reader()
                if type(mask) is not int or not 0 < mask <= 0x03FFFFFF:
                    raise OSError("Windows logical-drive observation failed; absence was not inferred.")
                self._ready.set()
                if submitted_before_read and not mask & self._bit:
                    self._absent.set()
                self._stop.wait(.02)
        except Exception as exc:
            self.error = str(exc)
            self.available = False
            self._ready.set()

    def mark_submitted(self):
        self._absent.clear()
        self._submitted.set()

    def saw_absence(self):
        return self._absent.is_set()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)


class ProgrammingJob:
    """One explicit submission, then bounded monitoring; never auto-retries flash."""
    def __init__(self, target, profile, image, *, cancel=None, event_sink=None,
                 discovery=None, clock=None, sleep=None, writer=None, timeout=120, observer_factory=None):
        if not isinstance(target, ProgrammerTarget) or not target.identity:
            raise GDLinkError("Select an identified GD-Link programmer.")
        self.profile = validate_profile(profile)
        if not isinstance(image, FirmwareImage):
            raise GDLinkError("Validate and freeze the firmware before starting.")
        checked = _image_from_bytes(image.name, image.data, self.profile)
        if checked != image:
            raise GDLinkError("Frozen firmware contents or validation metadata changed.")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 5 <= timeout <= 600:
            raise GDLinkError("Programming timeout must be between 5 and 600 seconds.")
        self.target, self.image, self.timeout = target, image, timeout
        self.cancel = cancel if cancel is not None else threading.Event()
        self.event_sink = event_sink
        self.discovery = discovery or discover_programmers
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.writer = writer or _write_file
        self.observer_factory = observer_factory or _DriveAbsenceObserver
        self._observer = None
        self.fast_observer_available = False
        self.fast_observer_error = ""
        self.events, self.logging_errors = [], []
        self.submitted = self.copy_completed = self.observed_unmount = self.observed_remount = False
        self.config_written = self.cancel_requested_after_submission = False
        self._ran = False
        self._started = 0.0
        self.started_at = None

    def _event(self, phase, detail, **extra):
        event = dict(timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                     elapsed_seconds=round(max(0, self.clock() - self._started), 3),
                     phase=phase, detail=detail, submitted=self.submitted, **extra)
        self.events.append(event)
        if self.event_sink is not None:
            try:
                self.event_sink(copy.deepcopy(event))
            except Exception as exc:
                self.logging_errors.append(str(exc))
                if not self.submitted:
                    raise GDLinkError("Programming event log failed before submission: " + str(exc)) from exc

    def _find(self):
        targets = self.discovery()
        if not isinstance(targets, (list, tuple)) or not all(isinstance(item, ProgrammerTarget) for item in targets):
            raise GDLinkError("Programmer discovery returned invalid identities.")
        selected = [item for item in targets if item.identity == self.target.identity]
        if len(selected) > 1:
            raise GDLinkError("The selected programmer identity is ambiguous.")
        return selected[0] if selected else None

    def _require_target(self):
        current = self._find()
        if current is None:
            raise GDLinkError("The approved USB programmer is no longer connected.")
        if current.volume_label.strip().casefold() != "gigadevice":
            raise GDLinkError("The selected USB programmer volume label changed.")
        parse_config(current.config_text)
        return current

    @staticmethod
    def _contents(target):
        paths = list(Path(target.root).iterdir())
        items = {path.name.casefold(): path for path in paths}
        if len(items) != len(paths):
            raise GDLinkError("Programmer volume contains ambiguous filenames.")
        return items

    def _clean_volume(self, target):
        items = self._contents(target)
        if set(items) != {"config.txt"} or not items["config.txt"].is_file():
            raise GDLinkError("Programmer volume must contain only CONFIG.TXT before submission. Review existing files; none were deleted.")

    def _result(self, status, detail, **extra):
        return dict(status=status, outcome=status, detail=detail, started_at=self.started_at,
                    finished_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    elapsed_seconds=round(max(0, self.clock()-self._started), 3),
                    submitted=self.submitted, copy_completed=self.copy_completed,
                    config_written=self.config_written, observed_unmount=self.observed_unmount,
                    observed_remount=self.observed_remount,
                    fast_observer_available=self.fast_observer_available,
                    fast_observer_error=self.fast_observer_error,
                    cancel_requested_after_submission=self.cancel_requested_after_submission,
                    programmer=self.target.to_dict(), profile=copy.deepcopy(self.profile),
                    firmware=self.image.to_dict(), events=copy.deepcopy(self.events),
                    logging_errors=list(self.logging_errors), target_mcu_verified=False,
                    independent_flash_verification=False, calibration_preservation_verified=False, **extra)

    def run(self):
        if self._ran:
            raise GDLinkError("This programming job was already attempted; automatic retry is prohibited.")
        self._ran = True
        self._started = self.clock()
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        try:
            if self.cancel.is_set():
                return self._result("CANCELLED", "Cancelled before any programmer writes.")
            self._event("checking", "Checking the selected USB programmer and frozen firmware.")
            current = self._require_target()
            self._clean_volume(current)
            if self.cancel.is_set():
                return self._result("CANCELLED", "Cancelled before any programmer writes.")
            self._event("configuration", "Applying the administrator-approved programming configuration.")
            # Discovery is repeated immediately before each write; drive letters
            # are never reused as identity after disconnect/remount.
            current = self._require_target()
            self._clean_volume(current)
            self.writer(Path(current.root)/"CONFIG.TXT", self.profile["config_text"].encode("utf-8"), exclusive=False)
            self.config_written = True
            current = self._require_target()
            if parse_config(_read_config(current.root)) != self.profile["config"]:
                raise GDLinkError("Approved CONFIG.TXT did not read back correctly; firmware was not submitted.")
            if self.cancel.is_set():
                return self._result("CANCELLED", "Cancelled before firmware submission; approved configuration was written.")
            self._event("ready_to_submit", "Submitting the approved firmware once. Stop cannot interrupt physical flashing after this point.")
            current = self._require_target()
            self._clean_volume(current)
            if parse_config(_read_config(current.root)) != self.profile["config"]:
                raise GDLinkError("Programmer configuration changed immediately before submission.")
            if self.cancel.is_set():
                return self._result("CANCELLED", "Cancelled before firmware submission; approved configuration was written.")
            # Arm before the copy so even a brief removal during write/flush is
            # retained. Identity is still checked with CIM after the return.
            try:
                self._observer = self.observer_factory(current.root)
                self.fast_observer_available = self._observer.start() is True
                self.fast_observer_error = getattr(self._observer, "error", "")
            except Exception as exc:
                self.fast_observer_error = str(exc)
            final = self._require_target()
            if final.root != current.root:
                raise GDLinkError("Programmer drive changed while arming observation; firmware was not submitted.")
            self._clean_volume(final)
            if parse_config(_read_config(final.root)) != self.profile["config"]:
                raise GDLinkError("Programmer configuration changed before the firmware transfer.")
            if self.cancel.is_set():
                return self._result("CANCELLED", "Cancelled before firmware submission; approved configuration was written.")
            self.submitted = True
            self._event("submitting", "Firmware submission started; the physical programming attempt will not be retried.")
            if self.fast_observer_available:
                try:
                    self._observer.mark_submitted()
                except Exception as exc:
                    self.fast_observer_available = False
                    self.fast_observer_error = str(exc)
            copy_error = ""
            try:
                self.writer(Path(current.root)/self.image.name, self.image.data, exclusive=True)
                self.copy_completed = True
            except Exception as exc:
                copy_error = str(exc)
            self._event("programming", "Firmware submission attempted; waiting for the programmer's disconnect/reconnect and result.",
                        copy_completed=self.copy_completed, copy_error=copy_error)
            return self._monitor(copy_error)
        except Exception as exc:
            if self.submitted:
                return self._result("UNCONFIRMED", "Programming may have started; completion is unconfirmed: " + str(exc))
            return self._result("FAILED", "Firmware was not submitted: " + str(exc))
        finally:
            if self._observer is not None:
                try:
                    self._observer.stop()
                except Exception as exc:
                    self.logging_errors.append("Volume observer close: " + str(exc))

    def _monitor(self, copy_error):
        deadline = self.clock() + self.timeout
        stable_since, stable_count, last_error = None, 0, ""
        while self.clock() < deadline:
            if self.cancel.is_set() and not self.cancel_requested_after_submission:
                self.cancel_requested_after_submission = True
                self._event("finishing", "Stop requested after submission. Monitoring continues because physical flashing cannot be cancelled safely.")
            try:
                if self.fast_observer_available:
                    self.fast_observer_error = getattr(self._observer, "error", "")
                    if self._observer.saw_absence() and not self.observed_unmount:
                        self.observed_unmount = True
                        self._event("reconnecting", "Fast observation captured USB drive removal after submission; checking the same programmer's return.")
                current = self._find()
                if current is None:
                    if not self.observed_unmount:
                        self.observed_unmount = True
                        self._event("reconnecting", "Observed the selected programmer disconnect; waiting for the same USB identity to return.")
                    stable_since, stable_count = None, 0
                elif self.observed_unmount:
                    if not self.observed_remount:
                        self.observed_remount = True
                        self._event("checking_result", "The same USB programmer reconnected; reading its programming result.")
                    items = self._contents(current)
                    if "fail.txt" in items and items["fail.txt"].is_file():
                        with items["fail.txt"].open("rb") as stream:
                            failure = stream.read(65536).decode("utf-8-sig", errors="replace")
                        self._event("failed", "The programmer reported a failure in FAIL.TXT.")
                        return self._result("FAILED", "Programmer reported failure: " + failure[:2000], programmer_failure=failure)
                    only_config = set(items) == {"config.txt"} and items["config.txt"].is_file()
                    unchanged_config = only_config and parse_config(_read_config(current.root)) == self.profile["config"]
                    if unchanged_config:
                        if stable_since is None:
                            stable_since = self.clock()
                        stable_count += 1
                        if self.clock() - stable_since >= 1 and stable_count >= 3:
                            if not self.copy_completed:
                                return self._result("UNCONFIRMED", "Programmer returned without FAIL.TXT, but full firmware transfer was not confirmed: " + copy_error)
                            self._event("complete", "Programmer reported success after the observed reconnect. Independent flash verification was not performed.")
                            return self._result("PROGRAMMER_REPORTED_SUCCESS", "Programmer reported success; target MCU identity and independent flash readback were not verified.")
                    else:
                        stable_since, stable_count = None, 0
                        last_error = "Unexpected result files or a changed programming configuration after reconnect."
                # A pre-existing CONFIG.TXT without an observed disconnect/reconnect
                # is never accepted as evidence that this image was programmed.
            except Exception as exc:
                stable_since, stable_count, last_error = None, 0, str(exc)
            self.sleep(.2)
        detail = ("Timed out without an observed programmer disconnect/reconnect; file copy is not proof of programming."
                  if not self.observed_remount else "Timed out waiting for an unambiguous programmer result after reconnect.")
        if last_error:
            detail += " Last observation: " + last_error
        if copy_error:
            detail += " Transfer error: " + copy_error
        self._event("unconfirmed", detail)
        return self._result("UNCONFIRMED", detail)
