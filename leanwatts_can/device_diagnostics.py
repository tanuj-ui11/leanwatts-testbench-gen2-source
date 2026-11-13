"""Read-only Windows Plug-and-Play diagnostics for the known ZLG USB adapter.

No CAN channel is opened, no vendor library is loaded, and no Windows/device
settings are changed. A ready PnP driver does not establish CAN communication.

API references:
https://learn.microsoft.com/en-us/windows/win32/api/setupapi/nf-setupapi-setupdigetclassdevsw
https://learn.microsoft.com/en-us/windows/win32/api/cfgmgr32/nf-cfgmgr32-cm_get_devnode_status
https://learn.microsoft.com/en-us/windows-hardware/drivers/install/cm-prob-driver-failed-load
https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/event-id-explanations
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DN_STARTED = 0x00000008
DN_HAS_PROBLEM = 0x00000400
DN_PRIVATE_PROBLEM = 0x00008000
_ADAPTER_ID = re.compile(r"^USB\\VID_0471&PID_1200(?:[&\\]|$)", re.IGNORECASE)
_USBCAN_DRIVER = re.compile(r"(?:^|[\\/])usbcan\.sys$", re.IGNORECASE)


def _matches_adapter(instance_id: Any) -> bool:
    return isinstance(instance_id, str) and bool(_ADAPTER_ID.match(instance_id))


def _enumerate_adapter_nodes() -> tuple[list[dict[str, Any]], list[str]]:
    # Import and load only Windows inventory libraries, and only on explicit check.
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    class SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", GUID),
                    ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_size_t)]

    setup = ctypes.WinDLL("setupapi.dll", use_last_error=True)
    cfg = ctypes.WinDLL("cfgmgr32.dll", use_last_error=True)
    ptr_info = ctypes.POINTER(SP_DEVINFO_DATA)
    ptr_dword = ctypes.POINTER(wintypes.DWORD)
    setup.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setup.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setup.SetupDiEnumDeviceInfo.argtypes = [wintypes.HANDLE, wintypes.DWORD, ptr_info]
    setup.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
    setup.SetupDiGetDeviceInstanceIdW.argtypes = [wintypes.HANDLE, ptr_info, wintypes.LPWSTR, wintypes.DWORD, ptr_dword]
    setup.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
    setup.SetupDiGetDeviceRegistryPropertyW.argtypes = [wintypes.HANDLE, ptr_info, wintypes.DWORD,
                                                       ptr_dword, ctypes.c_void_p, wintypes.DWORD, ptr_dword]
    setup.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
    setup.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    setup.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
    cfg.CM_Get_DevNode_Status.argtypes = [ptr_dword, ptr_dword, wintypes.DWORD, wintypes.ULONG]
    cfg.CM_Get_DevNode_Status.restype = wintypes.ULONG

    devices: list[dict[str, Any]] = []
    errors: list[str] = []
    # DIGCF_PRESENT | DIGCF_ALLCLASSES; inventory only, with USB enumerator.
    handle = setup.SetupDiGetClassDevsW(None, "USB", None, 0x02 | 0x04)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "Windows device inventory could not be opened")

    def property_text(info: SP_DEVINFO_DATA, prop: int) -> str:
        kind, required = wintypes.DWORD(), wintypes.DWORD()
        buffer = ctypes.create_unicode_buffer(1024)
        if setup.SetupDiGetDeviceRegistryPropertyW(handle, ctypes.byref(info), prop, ctypes.byref(kind),
                                                    buffer, ctypes.sizeof(buffer), ctypes.byref(required)):
            if kind.value == 1:  # REG_SZ; do not interpret an unexpected binary type.
                return buffer.value
        return ""

    try:
        for index in range(10000):
            info = SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(info)
            if not setup.SetupDiEnumDeviceInfo(handle, index, ctypes.byref(info)):
                error = ctypes.get_last_error()
                if error != 259:  # ERROR_NO_MORE_ITEMS is the only clean end.
                    errors.append(f"Device enumeration ended unexpectedly (Windows error {error}).")
                break
            required = wintypes.DWORD()
            instance = ctypes.create_unicode_buffer(1024)
            if not setup.SetupDiGetDeviceInstanceIdW(handle, ctypes.byref(info), instance, len(instance), ctypes.byref(required)):
                errors.append(f"A USB device identity could not be read (Windows error {ctypes.get_last_error()}).")
                continue
            if not _matches_adapter(instance.value):
                continue
            name = property_text(info, 12) or property_text(info, 0) or "USB CAN adapter (0471:1200)"
            status, problem = wintypes.DWORD(), wintypes.DWORD()
            result = cfg.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), info.DevInst, 0)
            device: dict[str, Any] = {"instance_id": instance.value, "name": name,
                                      "problem_code": None, "status_flags": None, "started": False,
                                      "has_problem": None, "status_error": ""}
            if result != 0:
                device["status_error"] = f"Windows status query failed (CONFIGRET 0x{result:08X})."
            else:
                has_problem = bool(status.value & DN_HAS_PROBLEM)
                device.update(status_flags=status.value, problem_code=problem.value if has_problem else 0,
                              has_problem=has_problem, started=bool(status.value & DN_STARTED))
            devices.append(device)
        else:
            errors.append("Device enumeration exceeded its bound; inventory may be incomplete.")
    finally:
        if not setup.SetupDiDestroyDeviceInfoList(handle):
            errors.append("Windows device inventory handle could not be released cleanly.")
    return devices, errors


def _parse_event_xml(text: str) -> list[dict[str, Any]]:
    if len(text) > 512000 or "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ValueError("Unexpected or oversized event XML.")
    root = ET.fromstring("<Events>" + text.lstrip("\ufeff") + "</Events>")
    ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
    events = []
    for event in root.findall("e:Event", ns):
        event_id = event.findtext("e:System/e:EventID", namespaces=ns)
        stamp = event.find("e:System/e:TimeCreated", ns)
        if event_id != "3111" or stamp is None:
            continue
        events.append({"event_id": 3111, "timestamp": stamp.get("SystemTime", ""),
                       "record_id": event.findtext("e:System/e:EventRecordID", namespaces=ns),
                       "data_values": [node.text or "" for node in event.findall("e:EventData/e:Data", ns)]})
    return events


def _query_code_integrity_events() -> tuple[list[dict[str, Any]], str]:
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/wevtutil.exe"
    try:
        completed = subprocess.run(
            [str(executable), "qe", "Microsoft-Windows-CodeIntegrity/Operational",
             "/q:*[System[(EventID=3111)]]", "/c:20", "/rd:true", "/f:xml"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
        if completed.returncode:
            return [], f"Optional Code Integrity lookup failed (exit {completed.returncode})."
        if len(completed.stdout) > 512000:
            return [], "Optional Code Integrity response exceeded its bound."
        return _parse_event_xml(completed.stdout), ""
    except subprocess.TimeoutExpired:
        return [], "Optional Code Integrity lookup timed out; the driver problem code remains authoritative."
    except (OSError, ValueError, ET.ParseError) as error:
        return [], "Optional Code Integrity lookup unavailable: " + str(error)


def _matching_recent_events(events: list[Any], now: datetime) -> list[dict[str, Any]]:
    matches = []
    for event in events:
        if not isinstance(event, dict) or event.get("event_id") != 3111:
            continue
        values = event.get("data_values")
        if not isinstance(values, list) or not any(isinstance(value, str) and _USBCAN_DRIVER.search(value) for value in values):
            continue
        try:
            timestamp = datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00"))
            if timestamp.tzinfo is None or not now - timedelta(days=7) <= timestamp <= now:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        matches.append({"event_id": 3111, "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
                        "record_id": event.get("record_id"), "driver": "usbcan.sys"})
    return matches


def _classify(devices: list[dict[str, Any]], errors: list[str]) -> dict[str, Any]:
    devices = [dict(device) for device in devices if _matches_adapter(device.get("instance_id"))]
    result: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "state": "error", "summary": "Windows adapter status could not be established.",
        "devices": devices, "details": list(errors), "communication_tested": False,
    }
    if errors:
        return result
    if not devices:
        result.update(state="not_found", summary="No connected USB adapter with ID 0471:1200 was found.")
        result["details"].append("Only currently present matching USB devices were checked; no CAN connection was attempted.")
        return result
    for device in devices:
        flags, problem = device.get("status_flags"), device.get("problem_code")
        if (device.get("status_error") or not isinstance(flags, int) or isinstance(flags, bool)
                or not isinstance(problem, int) or isinstance(problem, bool)):
            result["details"].append(device.get("status_error") or "A matching adapter has an unknown Windows status.")
            return result
    blocked = [device for device in devices if device["problem_code"] != 0
               or device["status_flags"] & (DN_HAS_PROBLEM | DN_PRIVATE_PROBLEM)
               or not device["status_flags"] & DN_STARTED]
    if blocked:
        result.update(state="driver_blocked", summary="The USB adapter is detected, but its Windows driver is not ready.")
        for device in blocked:
            code = device["problem_code"]
            if code == 39:
                result["details"].append("Windows Code 39: the adapter driver could not be loaded.")
            elif code:
                result["details"].append(f"Windows reports problem Code {code} for {device['name']}.")
            else:
                result["details"].append("A matching adapter is not started or has an unresolved Windows driver problem.")
    else:
        result.update(state="driver_ready", summary="Windows reports the matching USB adapter driver as started without a problem.")
    if len(devices) > 1:
        result["details"].append(f"{len(devices)} matching adapter instances were found; the intended adapter must be selected before future live use.")
    result["details"].append("This checks Windows device status only. CAN traffic and charger communication have not been tested.")
    return result


def check_adapter() -> dict[str, Any]:
    """Read OS inventory/status only; always return a JSON-ready diagnostic result."""
    if sys.platform != "win32":
        return _classify([], ["Windows Plug-and-Play diagnostics are available only on Windows."])
    try:
        devices, errors = _enumerate_adapter_nodes()
        result = _classify(devices, errors)
    except Exception as error:
        return _classify([], ["Windows device inventory failed: " + str(error)])
    if result["state"] == "driver_blocked" and any(device.get("problem_code") == 39 for device in result["devices"]):
        try:
            events, event_error = _query_code_integrity_events()
            matches = _matching_recent_events(events, datetime.now(timezone.utc))
            if matches:
                result["code_integrity_events"] = matches
                result["details"].append(
                    "Recent Code Integrity event 3111 names usbcan.sys: that driver did not meet HVCI/Memory Integrity policy. "
                    "Together with current Code 39, this indicates a driver compatibility issue; the event is historical evidence, not a new communication test."
                )
            if event_error:
                result["details"].append(event_error)
        except Exception as error:
            result["details"].append("Optional Code Integrity evidence unavailable: " + str(error))
    return result
