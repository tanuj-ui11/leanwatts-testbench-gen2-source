"""Collect local connection identity without opening CAN, serial, or USB instruments."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys

from .device_diagnostics import check_adapter


def collect_connections():
    result = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "hardware_commands_sent": False, "instrument_ports_opened": False,
              "can_driver": check_adapter(), "serial_ports": [], "errors": []}
    try:
        from serial.tools import list_ports
        for port in list_ports.comports():
            result["serial_ports"].append({
                "port": port.device, "description": port.description,
                "manufacturer": port.manufacturer, "product": port.product,
                "vid": port.vid, "pid": port.pid, "hardware_id": port.hwid,
            })
    except Exception as exc:
        result["errors"].append("Serial inventory unavailable: " + str(exc))
    if sys.platform == "win32":
        result["windows_version"] = list(sys.getwindowsversion())[:3]
        try:
            utility = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/pnputil.exe"
            query = subprocess.run([str(utility), "/enum-devices", "/connected", "/bus", "USB", "/ids"],
                                   capture_output=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW)
            if len(query.stdout) > 1000000:
                raise ValueError("USB inventory exceeded its bound.")
            result["usb_inventory"] = query.stdout.decode("oem", errors="replace")
            if query.returncode:
                result["errors"].append(f"Windows USB inventory returned {query.returncode}.")
        except Exception as exc:
            result["errors"].append("USB inventory unavailable: " + str(exc))
    result["load_protocol"] = "Unverified. USB connection alone does not establish a command protocol."
    return result
