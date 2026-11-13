"""Explicit HTTPS update downloads; importing this module performs no I/O."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import update_credentials, github_auth
from .version import VERSION, APPLICATION_ID


APPLICATION = APPLICATION_ID
CURRENT_VERSION = VERSION
MAX_INSTALLER_BYTES = 512 * 1024 * 1024


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,5}){1,3}", value):
        raise ValueError("The update version must contain two to four numeric parts.")
    parts = tuple(int(part) for part in value.split("."))
    return parts + (0,) * (4 - len(parts))


def https_url(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("Enter the published HTTPS update manifest address.")
    parsed = urlsplit(value)
    if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or any(ord(c) < 33 for c in value)):
        raise ValueError("Updates require an HTTPS address without embedded credentials or a fragment.")
    return value


class HTTPSRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        # GitHub's asset API redirects to signed binary URLs. Never forward the
        # repository token to a storage host or any non-API destination.
        if redirected is not None and not _github_api_url(newurl):
            for mapping in (redirected.headers, redirected.unredirected_hdrs):
                for key in list(mapping):
                    if key.lower() == "authorization":
                        del mapping[key]
        return redirected


def _github_api_url(url):
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == "api.github.com" and parsed.port in (None, 443)


def _open(url, *, token=None, accept="application/json, application/octet-stream"):
    url = https_url(url)
    headers = {"User-Agent": "Leanwatts-Testbench-Gen2-Updater/" + CURRENT_VERSION, "Accept": accept}
    if token is not None:
        if not _github_api_url(url):
            raise ValueError("Private update credentials may only be sent to the GitHub API.")
        headers.update(Authorization="Bearer " + token, **{"X-GitHub-Api-Version": "2026-03-10"})
    request = Request(url, headers=headers)
    try:
        response = build_opener(HTTPSRedirects()).open(request, timeout=10)
    except HTTPError as exc:
        # Do not copy request headers, response bodies or signed URLs into logs.
        if token is not None and exc.code in (401, 403, 404):
            raise RuntimeError("GitHub could not access the published update. Sign in again, or ask your administrator to check release access.") from None
        raise RuntimeError(f"Update server returned HTTP {exc.code}.") from None
    except URLError:
        raise RuntimeError("Could not reach the update server over HTTPS. Check this laptop's network connection.") from None
    https_url(response.geturl())
    return response


def _json_bytes(body):
    try:
        manifest = json.loads(body.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("The update address did not return a JSON manifest. Use a direct download address, not a sharing or sign-in page.") from exc
    return manifest


def _read_json(url, *, token=None, binary=False, maximum=65536):
    with _open(url, token=token, accept="application/octet-stream" if binary else "application/vnd.github+json") as response:
        body = response.read(maximum + 1)
    if len(body) > maximum:
        raise ValueError("The update server response is too large.")
    return _json_bytes(body)


def _asset_name(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,180}", value)
            or value in (".", "..") or value.rstrip(" .") != value):
        raise ValueError("The update asset filename is invalid.")
    return value


def _release(manifest, current_version=CURRENT_VERSION, *, needs_url=True):
    if not isinstance(manifest, dict) or type(manifest.get("schema")) is not int or manifest.get("schema") != 1 or manifest.get("application") != APPLICATION:
        raise ValueError("This update manifest is not for Leanwatts Testbench Gen2.")
    version = manifest.get("version")
    newer = version_key(version) > version_key(current_version)
    packages = manifest.get("packages", [manifest])
    if not isinstance(packages, list) or not 1 <= len(packages) <= 30 or any(not isinstance(p, dict) for p in packages):
        raise ValueError("The update package list is invalid.")
    matching = [p for p in packages if p.get("package_type", "full") == "delta"
                and p.get("base_version") and version_key(p["base_version"]) == version_key(current_version)]
    full = [p for p in packages if p.get("package_type", "full") == "full"]
    package = (matching or full or ([] if newer else packages))
    if not package:
        raise ValueError("This update needs a different installed base version. Obtain the full installer or the update for this laptop's version.")
    package = package[0]
    package_type = package.get("package_type", "full")
    if package_type not in ("full", "delta"):
        raise ValueError("Unsupported update package type.")
    base_version = package.get("base_version")
    if package_type == "delta":
        version_key(base_version)
    installer = package.get("installer")
    if not isinstance(installer, dict):
        raise ValueError("The update manifest has no installer details.")
    url = https_url(installer.get("url")) if needs_url else installer.get("url", "")
    if url:
        https_url(url)
    checksum = installer.get("sha256")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
        raise ValueError("The update manifest has no valid SHA-256 checksum.")
    size = installer.get("size")
    if type(size) is not int or not 0 < size <= MAX_INSTALLER_BYTES:
        raise ValueError("The installer size is missing or exceeds the supported limit.")
    notes = manifest.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 10000:
        raise ValueError("The update release notes are invalid.")
    filename = _asset_name(installer.get("filename") or installer.get("asset_name")
                           or "Leanwatts-Testbench-Gen2-Setup-" + version + ".exe")
    if not filename.lower().endswith(".exe"):
        raise ValueError("The update must be a Windows installer EXE.")
    return {"version": version, "available": newer, "notes": notes,
            "package_type": package_type, "base_version": base_version,
            "installer": {"url": url, "sha256": checksum.lower(), "size": size,
                          "filename": filename, "asset_name": _asset_name(installer.get("asset_name", filename))}}


def check_update(feed_url, current_version=CURRENT_VERSION):
    return _release(_read_json(feed_url), current_version)


def _repository(owner, repository):
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", owner):
        raise ValueError("Enter the GitHub account or organisation name, not its email address.")
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository) or repository in (".", ".."):
        raise ValueError("Enter the GitHub repository name without a URL.")
    return owner + "/" + repository


def _github_asset(assets, name, repository):
    matches = [a for a in assets if isinstance(a, dict) and a.get("name") == name
               and type(a.get("id")) is int and a["id"] > 0 and a.get("state") == "uploaded"]
    if len(matches) != 1:
        raise ValueError("The published release must contain exactly one asset named " + name + ".")
    asset = matches[0]
    # Build the API URL from validated identity; do not trust an asset URL from JSON.
    return "https://api.github.com/repos/" + repository + "/releases/assets/" + str(asset["id"]), asset


def check_github(data_dir, current_version=CURRENT_VERSION, *, channel=None, auth_token=None):
    # Explicit channel/token arguments are used by the fixed publisher channel.
    # The old configuration path remains readable for 1.0 compatibility only.
    config = channel if channel is not None else load_config(data_dir)
    repository = _repository(config.get("owner"), config.get("repository"))
    public = channel is not None and channel.get("visibility") == "public"
    token = None if public else (auth_token if auth_token is not None else update_credentials.load_token(data_dir))
    release = _read_json("https://api.github.com/repos/" + repository + "/releases/latest",
                         token=token, maximum=2 * 1024 * 1024)
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        raise ValueError("No published production release is available.")
    assets = release.get("assets")
    if not isinstance(assets, list) or len(assets) > 1000:
        raise ValueError("GitHub returned an invalid release asset list.")
    manifest_url, _ = _github_asset(assets, config.get("manifest_asset", "update-manifest.json"), repository)
    manifest = _read_json(manifest_url, token=token, binary=True)
    result = _release(manifest, current_version, needs_url=False)
    installer_url, asset = _github_asset(assets, result["installer"]["asset_name"], repository)
    if asset.get("size") != result["installer"]["size"]:
        raise ValueError("The GitHub installer size does not match its update manifest.")
    result["installer"]["url"] = installer_url
    result.update(source="github_public" if public else ("github_app" if channel is not None else "github_private"), repository=repository)
    return result


def check_configured(data_dir, current_version=CURRENT_VERSION):
    config = github_auth.channel_config()
    return check_github(data_dir, current_version, channel=config,
                        auth_token=None if config.get("visibility") == "public" else github_auth.access_token(data_dir))


def download_update(release, destination_dir, *, token=None):
    """Download only; never launch, install, or delete an existing installer."""
    version_key(release["version"])
    target_dir = Path(destination_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    # Different delta bases/test builds can share a release version.
    target = target_dir / (release["installer"]["sha256"][:12] + "-" + _asset_name(release["installer"]["filename"]))
    installer = release["installer"]
    if target.exists():
        verify_installer(target, installer)
        return str(target)
    descriptor, temp_name = tempfile.mkstemp(prefix=target.stem + "-", suffix=".download", dir=target_dir)
    os.close(descriptor)
    temporary = Path(temp_name)
    deadline = time.monotonic() + 180
    total = 0
    digest = hashlib.sha256()
    try:
        with _open(installer["url"], token=token, accept="application/octet-stream") as response, temporary.open("wb") as output:
            header_size = response.headers.get("Content-Length")
            if header_size and int(header_size) != installer["size"]:
                raise ValueError("The download size differs from the published installer.")
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("The installer download timed out. Check the network and try again.")
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > installer["size"] or total > MAX_INSTALLER_BYTES:
                    raise ValueError("The download exceeds the published installer size.")
                digest.update(chunk)
                output.write(chunk)
        if total != installer["size"] or digest.hexdigest() != installer["sha256"]:
            raise ValueError("The downloaded installer failed its size/checksum check; it will not be installed.")
        with temporary.open("rb") as check:
            if check.read(2) != b"MZ":
                raise ValueError("The downloaded file is not a Windows installer.")
        os.replace(temporary, target)
        return str(target)
    except Exception:
        # Only this known partial file is removed, never an installed program.
        if temporary.is_file():
            temporary.unlink()
        raise


def download_configured(data_dir, release):
    token = None
    if release.get("source") in ("github_app", "github_public"):
        config = github_auth.channel_config()
        repository = _repository(config["owner"], config["repository"])
        public = config.get("visibility") == "public"
        if repository != release.get("repository") or public != (release.get("source") == "github_public"):
            raise ValueError("The release channel changed. Check for updates again.")
        # Never attach a new credential to a URL from another publisher/channel.
        asset_url = release["installer"]["url"]
        if not re.fullmatch(re.escape("https://api.github.com/repos/" + repository + "/releases/assets/") + r"[1-9][0-9]*", asset_url):
            raise ValueError("The release asset does not belong to this update channel.")
        token = None if public else github_auth.access_token(data_dir)
    elif release.get("source") == "github_private":
        config = load_config(data_dir)
        if _repository(config.get("owner"), config.get("repository")) != release.get("repository"):
            raise ValueError("The repository changed. Check for updates again before downloading.")
        token = update_credentials.load_token(data_dir)
    return download_update(release, Path(data_dir) / "updates", token=token)


def local_update(installer_path, manifest_path=None, current_version=CURRENT_VERSION):
    """Validate an explicitly selected local test/full/delta installer; no execution."""
    installer_path = Path(installer_path).resolve()
    manifest_path = Path(manifest_path).resolve() if manifest_path else installer_path.with_suffix(".update.json")
    if not manifest_path.is_file():
        raise ValueError("Keep the matching .update.json manifest beside this installer, or select it when prompted.")
    if manifest_path.stat().st_size > 65536:
        raise ValueError("The local update manifest is too large.")
    release = _release(_json_bytes(manifest_path.read_bytes()), current_version, needs_url=False)
    if version_key(release["version"]) < version_key(current_version):
        raise ValueError("This local update is older than the installed version; downgrades are not supported.")
    if release["package_type"] == "delta" and version_key(release["base_version"]) != version_key(current_version):
        raise ValueError("This small update was built for a different installed base. Use the matching update or the full installer.")
    if installer_path.name != release["installer"]["filename"]:
        raise ValueError("The selected installer filename does not match its manifest.")
    verify_installer(installer_path, release["installer"])
    release.update(source="local", local_path=str(installer_path))
    return release


def verify_installer(path, details):
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size != details["size"]:
        raise ValueError("The downloaded installer is missing or its size changed.")
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("The downloaded file is not a Windows installer.")
        stream.seek(0)
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if checksum != details["sha256"]:
        raise ValueError("The installer checksum changed; download it again.")
    return path


def launch_installer(path, release):
    """Called only by the explicit Install action after the run/session ends."""
    if os.name != "nt":
        raise RuntimeError("The installer is for Windows.")
    verified = verify_installer(path, release["installer"])
    # A running EXE must not live under the installation being replaced.
    # Stage a verified copy outside the app before asking the GUI to close.
    staging = Path(tempfile.mkdtemp(prefix="Leanwatts-Testbench-Gen2-Update-"))
    staged = staging / verified.name
    shutil.copy2(verified, staged)
    verify_installer(staged, release["installer"])
    subprocess.Popen([str(staged), "--wait-pid", str(os.getpid())], shell=False,
                     cwd=str(staging), close_fds=True)


def load_config(data_dir):
    path = Path(data_dir) / "update-settings.json"
    if not path.exists():
        return {"schema": 2, "source": "github_private", "owner": "", "repository": "",
                "manifest_asset": "update-manifest.json", "manifest_url": ""}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") not in (1, 2):
        raise ValueError("Unsupported update settings.")
    if value.get("schema") == 1:
        value.update(schema=2, source="https", owner="", repository="", manifest_asset="update-manifest.json")
    if value.get("source") not in ("github_private", "https"):
        raise ValueError("Unsupported update source.")
    url = value.get("manifest_url", "")
    if url:
        https_url(url)
    return value


def save_config(data_dir, *, source, owner="", repository="", manifest_asset="update-manifest.json", manifest_url="", token=""):
    if source not in ("github_private", "https"):
        raise ValueError("Choose a supported update source.")
    owner, repository, manifest_asset = owner.strip(), repository.strip(), manifest_asset.strip()
    if source == "github_private":
        _repository(owner, repository)
        _asset_name(manifest_asset)
    url = https_url(manifest_url.strip()) if manifest_url.strip() else ""
    if source == "https" and not url:
        raise ValueError("Enter the published HTTPS manifest address.")
    if token.strip():
        update_credentials.save_token(data_dir, token)
    path = Path(data_dir) / "update-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"schema": 2, "source": source, "manifest_url": url,
                                    "owner": owner, "repository": repository,
                                    "manifest_asset": manifest_asset}, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return load_config(data_dir)


def load_feed(data_dir):
    return load_config(data_dir).get("manifest_url", "")


def save_feed(data_dir, url):
    save_config(data_dir, source="https", manifest_url=url)
    return url.strip()
