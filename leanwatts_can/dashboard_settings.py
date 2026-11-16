"""Dashboard connection settings. The API key is DPAPI-protected per Windows account, or kept in memory only."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import update_credentials
from .dashboard_client import DashboardClient, checked_base_url

SETTINGS_FILE = "dashboard-settings.json"
KEY_FILE = "dashboard-api-key.dpapi"
SETTINGS_SCHEMA = 1
MAX_KEY_FILE_BYTES = 4096
_API_KEY = re.compile(r"[A-Za-z0-9_.\-]{16,256}")


def checked_station_name(value):
    if not isinstance(value, str):
        raise ValueError("Enter the station name.")
    value = value.strip()
    if not 1 <= len(value) <= 64 or any(ord(c) < 32 for c in value):
        raise ValueError("Enter a station name of up to 64 printable characters.")
    return value


def checked_api_key(value):
    if not isinstance(value, str) or not _API_KEY.fullmatch(value.strip()):
        raise ValueError("Enter the dashboard API key exactly as the dashboard issued it.")
    return value.strip()


def _dpapi_protect(data):
    return update_credentials._crypt(data, protect=True)


def _dpapi_unprotect(data):
    return update_credentials._crypt(data, protect=False)


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


class DashboardSettings:
    """Non-secret values live in a JSON file; the key only ever lives in a protected file or in memory."""

    def __init__(self, data_dir, *, protect=_dpapi_protect, unprotect=_dpapi_unprotect, allow_loopback_http=False):
        self.folder = Path(data_dir)
        self._protect = protect
        self._unprotect = unprotect
        self._allow_loopback_http = allow_loopback_http
        self._memory_key = None

    def settings(self):
        values = {"base_url": "", "station_name": ""}
        path = self.folder / SETTINGS_FILE
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return values
            if isinstance(data, dict) and data.get("schema") == SETTINGS_SCHEMA:
                for key in values:
                    if isinstance(data.get(key), str):
                        values[key] = data[key]
        return values

    def save(self, *, base_url, station_name):
        """Save the address and station. The key is never part of this file."""
        base = checked_base_url(base_url, allow_loopback_http=self._allow_loopback_http)
        station = checked_station_name(station_name)
        encoded = json.dumps({"schema": SETTINGS_SCHEMA, "base_url": base, "station_name": station},
                             indent=2, ensure_ascii=False).encode("utf-8")
        _atomic_write(self.folder / SETTINGS_FILE, encoded)

    def save_api_key(self, api_key):
        """Return 'protected' or 'memory'. Memory means Windows protection failed; nothing is written."""
        key = checked_api_key(api_key)
        try:
            encrypted = self._protect(key.encode("ascii"))
        except (RuntimeError, OSError):
            self._memory_key = key
            (self.folder / KEY_FILE).unlink(missing_ok=True)
            return "memory"
        self._memory_key = None
        _atomic_write(self.folder / KEY_FILE, encrypted)
        return "protected"

    def key_storage(self):
        if self._memory_key:
            return "memory"
        if (self.folder / KEY_FILE).is_file():
            return "protected"
        return "none"

    def api_key(self):
        if self._memory_key:
            return self._memory_key
        path = self.folder / KEY_FILE
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_KEY_FILE_BYTES:
            raise ValueError("The saved dashboard key is invalid. Enter it again.")
        try:
            return checked_api_key(self._unprotect(path.read_bytes()).decode("ascii"))
        except (RuntimeError, OSError, UnicodeError, ValueError):
            raise ValueError("The saved dashboard key cannot be opened on this Windows account. Enter it again.") from None

    def forget_key(self):
        self._memory_key = None
        (self.folder / KEY_FILE).unlink(missing_ok=True)

    def is_configured(self):
        values = self.settings()
        return bool(values["base_url"] and values["station_name"] and self.key_storage() != "none")

    def client(self, *, opener=None):
        values = self.settings()
        if not values["base_url"]:
            raise ValueError("Set the dashboard address in Settings > Dashboard first.")
        key = self.api_key()
        if not key:
            raise ValueError("Enter the dashboard API key in Settings > Dashboard first.")
        return DashboardClient(values["base_url"], key, opener=opener,
                               allow_loopback_http=self._allow_loopback_http)
