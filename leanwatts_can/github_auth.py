"""Company GitHub App device sign-in. No network activity occurs on import."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import update_credentials

CHANNEL_FILE = Path(__file__).with_name("release_channel.json")
DEVICE_ENDPOINT = "https://github.com/login/device/code"
TOKEN_ENDPOINT = "https://github.com/login/oauth/access_token"
VERIFY_URL = "https://github.com/login/device"
PROFILE_URL = "https://api.github.com/user"
_AUTH_LOCK = threading.RLock()
_GENERATIONS = {}


def channel_config():
    """Read public publisher identity shipped with the signed/reviewed package."""
    try:
        if CHANNEL_FILE.stat().st_size > 8192:
            raise ValueError()
        value = json.loads(CHANNEL_FILE.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1
                or not isinstance(value.get("owner"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", value["owner"])
                or not isinstance(value.get("repository"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value["repository"])
                or value["repository"] in (".", "..")):
            raise ValueError()
        visibility = value.get("visibility", "private")
        if visibility not in ("public", "private"):
            raise ValueError()
        if visibility == "private" and (
                type(value.get("repository_id")) is not int or not 0 < value["repository_id"] < 2**63
                or not isinstance(value.get("client_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{10,128}", value["client_id"])):
            raise ValueError()
        asset = value.get("manifest_asset", "update-manifest.json")
        if not isinstance(asset, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}", asset):
            raise ValueError()
        keys = ("schema", "owner", "repository")
        if visibility == "private":
            keys += ("repository_id", "client_id")
        return {key: value[key] for key in keys} | {"manifest_asset": asset, "visibility": visibility}
    except (OSError, ValueError, TypeError, KeyError):
        raise RuntimeError("Online updates are not set up in this build. Local update files are available.") from None


def configured():
    try:
        channel_config()
        return True
    except RuntimeError:
        return False


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("GitHub sign-in returned an unexpected redirect. Try again.")


def _request_json(url, fields=None, *, token=None):
    if fields is not None:
        if url not in (DEVICE_ENDPOINT, TOKEN_ENDPOINT) or token is not None:
            raise ValueError("Invalid GitHub sign-in destination.")
    elif url != PROFILE_URL or not token:
        raise ValueError("Invalid GitHub account lookup.")
    headers = {"Accept": "application/json", "User-Agent": "Leanwatts-Testbench-Gen2"}
    data = None
    if fields is not None:
        data = urlencode(fields).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + token
        headers["X-GitHub-Api-Version"] = "2026-03-10"
    request = Request(url, data=data, headers=headers)
    try:
        with build_opener(_NoRedirects()).open(request, timeout=10) as response:
            if response.geturl() != url:
                raise RuntimeError("GitHub sign-in returned an unexpected destination.")
            body = response.read(65537)
        if len(body) > 65536:
            raise ValueError()
        result = json.loads(body.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except HTTPError as exc:
        raise RuntimeError("GitHub sign-in request failed (HTTP " + str(exc.code) + "). Try signing in again.") from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError("Could not reach GitHub. Check this laptop's internet connection and try again.") from None
    except (ValueError, UnicodeError):
        raise RuntimeError("GitHub returned an invalid sign-in response. Try again.") from None


def _channel_matches(record, config):
    return all(record.get(key) == config[key] for key in ("client_id", "owner", "repository", "repository_id"))


def _timestamp(value):
    return type(value) in (float, int) and math.isfinite(value) and value > 0


def _validated_record(record, config):
    if (not isinstance(record, dict) or record.get("schema") != 1 or not _channel_matches(record, config)
            or not isinstance(record.get("login"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", record["login"])
            or not _timestamp(record.get("expires_at"))
            or not _timestamp(record.get("refresh_expires_at"))):
        raise ValueError("The saved GitHub sign-in belongs to a different release channel. Sign in again.")
    for key in ("access_token", "refresh_token"):
        try:
            update_credentials._validate_token(record.get(key))
        except ValueError:
            raise ValueError("The saved GitHub sign-in is invalid. Sign in again.") from None
    return record


def connection_status(data_dir):
    """Local-only status; never returns credentials or refreshes them."""
    try:
        config = channel_config()
    except RuntimeError as exc:
        return {"status": "not_configured", "login": "", "message": str(exc)}
    if config.get("visibility") == "public":
        return {"status": "public", "login": "", "message": "Leanwatts release channel · Public"}
    if not update_credentials.has_auth(data_dir):
        return {"status": "signed_out", "login": "", "message": "Sign in to receive private Leanwatts updates."}
    try:
        record = _validated_record(update_credentials.load_auth(data_dir), config)
        if record["expires_at"] <= time.time() and record["refresh_expires_at"] <= time.time():
            return {"status": "expired", "login": record["login"], "message": "Your GitHub sign-in expired. Sign in again."}
        return {"status": "connected", "login": record["login"], "message": "Signed in as " + record["login"] + "."}
    except (OSError, ValueError, RuntimeError):
        return {"status": "error", "login": "", "message": "This laptop could not unlock the saved GitHub sign-in. Sign in again."}


def _error_message(code):
    if not isinstance(code, str):
        return "GitHub sign-in could not finish. Try again."
    return {
        "access_denied": "GitHub sign-in was declined. You can try again.",
        "expired_token": "The sign-in code expired. Start sign-in again.",
        "token_expired": "The sign-in code expired. Start sign-in again.",
        "incorrect_device_code": "The sign-in code is no longer valid. Start sign-in again.",
        "bad_verification_code": "The sign-in code is no longer valid. Start sign-in again.",
        "device_flow_disabled": "GitHub sign-in is not enabled for this release. Contact your administrator.",
        "incorrect_client_credentials": "This release's GitHub sign-in setup is not valid. Contact your administrator.",
        "unverified_user_email": "Verify your GitHub email address, then sign in again.",
        "bad_refresh_token": "Your GitHub sign-in has expired or was revoked. Sign in again.",
    }.get(code, "GitHub sign-in could not finish. Try again.")


def begin_sign_in():
    config = channel_config()
    if config.get("visibility") == "public":
        raise RuntimeError("Public Leanwatts updates do not require GitHub sign-in.")
    response = _request_json(DEVICE_ENDPOINT, {"client_id": config["client_id"], "scope": "offline_access"})
    if response.get("error"):
        raise RuntimeError(_error_message(response.get("error")))
    if (response.get("verification_uri") != VERIFY_URL
            or not isinstance(response.get("device_code"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", response["device_code"])
            or not isinstance(response.get("user_code"), str)
            or not re.fullmatch(r"[A-Za-z0-9]{4}-[A-Za-z0-9]{4}", response["user_code"])
            or type(response.get("expires_in")) is not int or not 1 <= response["expires_in"] <= 1800
            or type(response.get("interval")) is not int or not 1 <= response["interval"] <= 60):
        raise RuntimeError("GitHub returned an invalid sign-in code. Try again.")
    flow = {key: response[key] for key in ("device_code", "user_code", "verification_uri", "expires_in", "interval")}
    flow.update(_config=config, _deadline=time.monotonic() + response["expires_in"],
                _next_poll=time.monotonic() + response["interval"], _finished=False)
    return flow


def _tokens(response, config, login):
    if response.get("error"):
        raise RuntimeError(_error_message(response["error"]))
    try:
        access = update_credentials._validate_token(response.get("access_token"))
        refresh = update_credentials._validate_token(response.get("refresh_token"))
        if (response.get("token_type", "").lower() != "bearer"
                or type(response.get("expires_in")) is not int or not 60 <= response["expires_in"] <= 86400
                or type(response.get("refresh_token_expires_in")) is not int
                or not 60 <= response["refresh_token_expires_in"] <= 366 * 86400):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError("GitHub returned an invalid credential response. Sign in again.") from None
    now = time.time()
    return dict(schema=1, access_token=access, refresh_token=refresh, login=login,
        expires_at=now + response["expires_in"], refresh_expires_at=now + response["refresh_token_expires_in"],
        **{key: config[key] for key in ("client_id", "owner", "repository", "repository_id")})


def _finish_flow(flow, status, message, **extra):
    flow["_finished"] = True
    flow.pop("device_code", None)
    return dict(status=status, message=message, **extra)


def poll_sign_in(data_dir, flow, cancel_event=None):
    """At most one token poll; callers schedule subsequent calls on a worker."""
    cancelled = lambda: cancel_event is not None and cancel_event.is_set()
    if cancelled() or flow.get("_finished"):
        return _finish_flow(flow, "cancelled", "Sign-in cancelled.")
    if time.monotonic() >= flow["_deadline"]:
        return _finish_flow(flow, "expired", "The sign-in code expired. Start sign-in again.")
    now = time.monotonic()
    if now < flow["_next_poll"]:
        return {"status": "pending", "interval": max(1, math.ceil(flow["_next_poll"] - now))}
    key = str(Path(data_dir).resolve())
    try:
        config = channel_config()
        if config != flow["_config"]:
            return _finish_flow(flow, "error", "The release channel changed. Start sign-in again.")
        with _AUTH_LOCK:
            flow.setdefault("_station_key", key)
            flow.setdefault("_generation", _GENERATIONS.get(key, 0))
            if flow["_station_key"] != key or flow["_generation"] != _GENERATIONS.get(key, 0):
                return _finish_flow(flow, "cancelled", "Sign-in cancelled.")
        flow["_next_poll"] = now + flow["interval"]
        response = _request_json(TOKEN_ENDPOINT, {"client_id": config["client_id"],
            "device_code": flow["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "repository_id": str(config["repository_id"])})
        if cancelled():
            return _finish_flow(flow, "cancelled", "Sign-in cancelled.")
        code = response.get("error")
        if code == "authorization_pending":
            return {"status": "pending", "interval": flow["interval"]}
        if code == "slow_down":
            supplied = response.get("interval")
            flow["interval"] = max(flow["interval"] + 5, supplied if type(supplied) is int and 1 <= supplied <= 300 else 0)
            flow["_next_poll"] = time.monotonic() + flow["interval"]
            return {"status": "slow_down", "interval": flow["interval"]}
        if code:
            status = "expired" if code in ("expired_token", "token_expired") else "error"
            return _finish_flow(flow, status, _error_message(code))
        record = _tokens(response, config, "")
        profile = _request_json(PROFILE_URL, token=record["access_token"])
        login = profile.get("login")
        if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", login):
            raise RuntimeError("GitHub did not return a valid account name. Sign in again.")
        record["login"] = login
        with _AUTH_LOCK:
            if cancelled() or flow["_generation"] != _GENERATIONS.get(key, 0):
                return _finish_flow(flow, "cancelled", "Sign-in cancelled.")
            update_credentials.save_auth(data_dir, record)
            if cancelled():
                update_credentials.remove_auth(data_dir)
                return _finish_flow(flow, "cancelled", "Sign-in cancelled.")
        return _finish_flow(flow, "complete", "Signed in as " + login + ".", login=login)
    except (OSError, ValueError, RuntimeError) as exc:
        # Exceptions from our request/validation/storage routines contain no
        # response bodies, tokens or device-code URLs.
        return _finish_flow(flow, "error", str(exc))


def access_token(data_dir):
    """Return a usable token, refreshing one time under a process-wide lock."""
    with _AUTH_LOCK:
        config = channel_config()
        if config.get("visibility") == "public":
            raise RuntimeError("Public Leanwatts updates do not use saved credentials.")
        record = _validated_record(update_credentials.load_auth(data_dir), config)
        now = time.time()
        if record["expires_at"] > now + 60:
            return record["access_token"]
        if record["refresh_expires_at"] <= now + 30:
            raise RuntimeError("Your GitHub sign-in expired. Sign in again.")
        response = _request_json(TOKEN_ENDPOINT, {"client_id": config["client_id"],
            "grant_type": "refresh_token", "refresh_token": record["refresh_token"]})
        refreshed = _tokens(response, config, record["login"])
        update_credentials.save_auth(data_dir, refreshed)
        return refreshed["access_token"]


def sign_out(data_dir):
    """Forget this Windows account's local credentials, not GitHub authorization."""
    key = str(Path(data_dir).resolve())
    with _AUTH_LOCK:
        _GENERATIONS[key] = _GENERATIONS.get(key, 0) + 1
        update_credentials.remove_auth(data_dir)
        update_credentials.remove_token(data_dir)
