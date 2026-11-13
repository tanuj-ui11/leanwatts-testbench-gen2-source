"""Bench client for the LeanWatts dashboard EoL API. Standard library only; importing performs no I/O."""
from __future__ import annotations

import json
import re
import socket
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 2 * 1024 * 1024
API_PREFIX = "/api/v1/eol"
USER_AGENT = "Leanwatts-Testbench-Gen2-EoL"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
_API_KEY = re.compile(r"[A-Za-z0-9_.\-]{16,256}")
_ERROR_CODE = re.compile(r"[a-z_]{1,64}")


class DashboardError(Exception):
    """kind is one of: auth, not_found, rejected, too_large, network, server."""

    def __init__(self, kind, status, message):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.message = message


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Returning None makes urllib raise HTTPError for the 3xx status; nothing is followed.
        return None


def checked_base_url(value, *, allow_loopback_http=False):
    """Return scheme://host[:port] for the dashboard. HTTPS only, except loopback when explicitly allowed (tests)."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError("Enter the dashboard address, for example https://ops.example.com.")
    value = value.strip()
    if any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("The dashboard address must not contain spaces or control characters.")
    try:
        parsed = urlsplit(value)
        parsed.port  # rejects malformed ports
    except ValueError:
        raise ValueError("The dashboard address is not valid.") from None
    host = parsed.hostname or ""
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Enter only the dashboard address, without a path, query, or sign-in details.")
    scheme = parsed.scheme.lower()
    if scheme == "https":
        pass
    elif scheme == "http" and allow_loopback_http and host in LOOPBACK_HOSTS:
        pass
    else:
        raise ValueError("The dashboard address must use HTTPS.")
    authority = f"[{host}]" if ":" in host else host
    return scheme + "://" + authority + (f":{parsed.port}" if parsed.port else "")


def _error_info(exc, api_key=""):
    """Return (code, text) from a dashboard error body. The code must match a strict pattern; the text is the
    server's own short message, stripped of control characters, with the API key masked and the length capped."""
    try:
        body = exc.read(4096)
    except OSError:
        return "", ""
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    error = data.get("error")
    error = error if isinstance(error, dict) else {}
    code = error.get("code", "")
    code = code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else ""
    text = ""
    for candidate in (error.get("message"), error.get("detail"), data.get("detail"), data.get("message")):
        if isinstance(candidate, str) and candidate.strip():
            text = candidate
            break
    if api_key:
        text = text.replace(api_key, "***")
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    if len(text) > 200:
        text = text[:199].rstrip() + "\u2026"
    return code, text


def _status_error(status, code, text=""):
    suffix = f" ({code})" if code else ""
    text = text.rstrip(".")
    if text and text != code:
        suffix += ": " + text
    if status == 401:
        return DashboardError("auth", status, "The dashboard did not accept this key (check the key in Settings > Dashboard).")
    if status == 403:
        return DashboardError("auth", status, "This key is not allowed to use the EoL API (it needs a Testing-role user with write access).")
    if status == 404:
        return DashboardError("not_found", status, "The dashboard does not have this item" + suffix + ".")
    if status == 413:
        return DashboardError("too_large", status, "The dashboard refused the report because it is too large" + suffix + ".")
    if 300 <= status < 400:
        return DashboardError("network", status, "The dashboard address redirected the request. Redirects are refused; check the address.")
    if 400 <= status < 500:
        return DashboardError("rejected", status, "The dashboard refused the request" + suffix + ".")
    return DashboardError("server", status, "The dashboard had a server error" + suffix + ".")


class DashboardClient:
    def __init__(self, base_url, api_key, *, timeout=20, opener=None, allow_loopback_http=False):
        self.base_url = checked_base_url(base_url, allow_loopback_http=allow_loopback_http)
        if not isinstance(api_key, str) or not _API_KEY.fullmatch(api_key.strip()):
            raise ValueError("Enter the dashboard API key exactly as the dashboard issued it.")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 60:
            raise ValueError("The dashboard timeout must be between 0 and 60 seconds.")
        self._api_key = api_key.strip()
        self._timeout = float(timeout)
        self._opener = opener or build_opener(_NoRedirects)

    def __repr__(self):
        return "DashboardClient(" + self.base_url + ", key=<hidden>)"

    def _request(self, method, path, body=None, *, idempotency_key=None):
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT, "X-API-Key": self._api_key}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(data) > MAX_REQUEST_BYTES:
                raise DashboardError("too_large", None, "The report is too large to send.")
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(self.base_url + API_PREFIX + path, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            # Never copy headers or the key into messages; only a vetted error code and the server's short text.
            code, text = _error_info(exc, self._api_key)
            raise _status_error(exc.code, code, text) from None
        except (URLError, socket.timeout, OSError):
            raise DashboardError("network", None,
                                 "Could not reach the dashboard over HTTPS. Check this laptop's network connection.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DashboardError("server", status, "The dashboard response is too large.")
        if status not in (200, 201):
            raise DashboardError("server", status, "The dashboard sent an unexpected response.")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise DashboardError("server", status, "The dashboard response was not valid JSON.") from None
        if not isinstance(value, dict):
            raise DashboardError("server", status, "The dashboard response was not a JSON object.")
        return value

    @staticmethod
    def _ref(value, label):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
            raise ValueError(f"Enter a {label}.")
        return quote(value.strip(), safe="")

    def ping(self):
        return self._request("GET", "/ping")

    def work_order_serials(self, ref):
        return self._request("GET", "/work-orders/" + self._ref(ref, "work order number") + "/serials")

    def serial(self, serial):
        return self._request("GET", "/serials/" + self._ref(serial, "serial number"))

    def post_report(self, body, client_report_id):
        if not isinstance(body, dict):
            raise ValueError("The report body must be an object.")
        if not isinstance(client_report_id, str) or not 1 <= len(client_report_id) <= 80:
            raise ValueError("The report needs a client report id of up to 80 characters.")
        payload = dict(body, client_report_id=client_report_id)
        return self._request("POST", "/reports", payload, idempotency_key=client_report_id)

    def get_report(self, report_id):
        return self._request("GET", "/reports/" + self._ref(report_id, "report id"))

    @staticmethod
    def clock_skew_seconds(ping_result, local_now):
        """Server clock minus local clock, in seconds (positive when this laptop runs slow)."""
        if not isinstance(ping_result, dict) or not isinstance(ping_result.get("server_time"), str):
            raise ValueError("The dashboard did not report its time.")
        if not isinstance(local_now, datetime) or local_now.tzinfo is None:
            raise ValueError("The local time must include a timezone.")
        try:
            server = datetime.fromisoformat(ping_result["server_time"])
        except ValueError:
            raise ValueError("The dashboard time is not valid.") from None
        if server.tzinfo is None:
            raise ValueError("The dashboard time must include a timezone.")
        return round((server - local_now).total_seconds(), 1)
