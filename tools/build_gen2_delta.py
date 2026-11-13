"""Build a changed-files Gen2 installer from two verified complete releases.

Example (only after both real releases exist):
  python tools/build_gen2_delta.py --base BASE.zip --target TARGET.zip \
      --version TARGET_VERSION --output UPDATE.exe

No release is published and the installer is never executed by this tool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parent.parent
PREFIX = "CAN-Calibration/"
APPLICATION = "leanwatts-testbench-gen2"
CSC = Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe")


def digest(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def version_parts(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+){1,3}", value):
        raise ValueError("Use a numeric version with two to four parts, such as 1.0 or 1.0.1.")
    parts = tuple(int(part) for part in value.split("."))
    if any(part > 65535 for part in parts):
        raise ValueError("Version components must fit Windows assembly version fields.")
    return parts + (0,) * (4-len(parts))


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts or "." in name.split("/")
            or "\\" in name or ":" in name or name.endswith("/")):
        raise ValueError("Unsafe application filename: " + repr(name))
    for part in name.split("/"):
        stem = part.split(".")[0].upper()
        if (not part or part != part.rstrip(" .") or any(character in part for character in '<>"|?*')
                or any(ord(character) < 32 for character in part)
                or stem in ("CON", "PRN", "AUX", "NUL") or re.fullmatch(r"(?:COM|LPT)[0-9]", stem)):
            raise ValueError("Unsafe Windows application filename: " + repr(name))
    first = path.parts[0].casefold()
    if first in ("calibration_data", "updates", ".app-running.lock", ".station-running.lock"):
        raise ValueError("A release cannot contain station data, locks or update cache.")
    if any(part.casefold() in (".app-running.lock", ".station-running.lock") for part in path.parts):
        raise ValueError("A release cannot contain running application locks.")


def read_release(path):
    """Return verified complete file contents and the unchanged manifest bytes."""
    with zipfile.ZipFile(path) as archive:
        entries = [entry.filename for entry in archive.infolist() if not entry.is_dir()]
        if len(entries) != len({name.casefold() for name in entries}):
            raise ValueError("Release contains duplicate filenames: " + str(path))
        if any(not name.startswith(PREFIX) for name in entries):
            raise ValueError("Release must use the CAN-Calibration archive root.")
        manifest_bytes = archive.read(PREFIX + "MANIFEST.json")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest, dict) or not manifest:
            raise ValueError("Release manifest must list all application files.")
        if len(manifest) != len({name.casefold() for name in manifest}):
            raise ValueError("Release manifest contains case-conflicting paths.")
        files = {}
        for name, expected in manifest.items():
            safe_name(name)
            if name.casefold() == "manifest.json":
                raise ValueError("A manifest cannot hash itself.")
            data = archive.read(PREFIX + name)
            if digest(data) != expected:
                raise ValueError("Release content does not match its manifest: " + name)
            files[name] = data
        if set(entries) != {PREFIX + name for name in manifest} | {PREFIX + "MANIFEST.json"}:
            raise ValueError("Release contains unlisted or missing application files.")
        identity = json.loads(files["VERSION.json"].decode("utf-8"))
        if identity.get("application") != APPLICATION:
            raise ValueError("This is not a Leanwatts Testbench Gen2 release.")
        version_parts(identity.get("version"))
        return files, manifest, manifest_bytes, identity["version"]


def render_installer(source, version, payload_hash, *, incremental=True):
    if source.count("__PAYLOAD_SHA256__") != 1:
        raise ValueError("Installer source must contain one payload digest placeholder.")
    source = source.replace("__PAYLOAD_SHA256__", payload_hash)
    windows_version = ".".join(str(part) for part in version_parts(version))
    description = "incremental update" if incremental else "workstation installer"
    patterns = (
        (r'internal const string Version = "[^"]+";', f'internal const string Version = "{version}";'),
        (r'AssemblyVersion\("[^"]+"\)', f'AssemblyVersion("{windows_version}")'),
        (r'AssemblyFileVersion\("[^"]+"\)', f'AssemblyFileVersion("{windows_version}")'),
        (r'AssemblyDescription\("[^"]+"\)', f'AssemblyDescription("Leanwatts Testbench Gen2 {version} {description}")'),
    )
    for pattern, replacement in patterns:
        source, count = re.subn(pattern, lambda _: replacement, source)
        if count != 1:
            raise ValueError("Installer version marker is missing or ambiguous.")
    return source


def build(base_path, target_path, version, output, *, compiler=CSC, icon=None, logo=None):
    output = Path(output).resolve()
    if output.suffix.lower() != ".exe":
        raise ValueError("The output must have an .exe extension.")
    base_files, baseline, base_manifest_bytes, base_version = read_release(Path(base_path))
    target_files, target_manifest, target_manifest_bytes, target_version = read_release(Path(target_path))
    if version != target_version:
        raise ValueError("Requested version does not match the target full release's VERSION.json.")
    if version_parts(version) <= version_parts(base_version):
        raise ValueError("An incremental release must be newer than its base.")
    changed = sorted(name for name, expected in target_manifest.items() if baseline.get(name) != expected)
    removed = sorted(set(baseline) - set(target_manifest))
    payload_path = output.with_suffix(".payload.zip")
    update_path = output.with_suffix(".update.json")
    report_path = output.with_suffix(".build.json")
    work = output.with_suffix(".build")
    checksum_path = output.with_suffix(".exe.sha256")
    if any(path.exists() for path in (output, payload_path, update_path, report_path, work, checksum_path)):
        raise ValueError("An output already exists; published installers are never overwritten.")
    output.parent.mkdir(parents=True, exist_ok=True)
    package = dict(schema=1, application=APPLICATION, package_type="delta",
        base_version=base_version, version=version,
        base_manifest_sha256=hashlib.sha256(base_manifest_bytes).hexdigest(),
        target_manifest_sha256=hashlib.sha256(target_manifest_bytes).hexdigest(),
        changed_files=changed, removed_files=removed,
        station_data_included=False, installed_application_only=True)
    with zipfile.ZipFile(payload_path, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("PACKAGE.json", json.dumps(package, indent=2) + "\n")
        archive.writestr("BASE-MANIFEST.json", base_manifest_bytes)
        archive.writestr(PREFIX + "MANIFEST.json", target_manifest_bytes)
        for name in changed:
            archive.writestr(PREFIX + name, target_files[name])
    with zipfile.ZipFile(payload_path) as archive:
        if set(archive.namelist()) != ({"PACKAGE.json", "BASE-MANIFEST.json", PREFIX + "MANIFEST.json"}
                                     | {PREFIX + name for name in changed}):
            raise ValueError("Delta archive contains unexpected files.")
        reconstructed = {name: data for name, data in base_files.items() if name in target_manifest}
        reconstructed.update({name: archive.read(PREFIX + name) for name in changed})
        if {name: digest(data) for name, data in reconstructed.items()} != target_manifest:
            raise ValueError("The changed files cannot reconstruct the complete target release.")
    with payload_path.open("rb") as stream:
        payload_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    work.mkdir()
    source_path = work / "CanCalibrationSetup.cs"
    source_path.write_text(render_installer((ROOT / "tools/CanCalibrationSetup.cs").read_text(encoding="utf-8"),
                                          version, payload_hash), encoding="utf-8")
    command = [str(compiler), "/nologo", "/target:winexe", "/platform:anycpu", "/optimize+",
        "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll", "/r:System.IO.Compression.dll",
        "/r:System.IO.Compression.FileSystem.dll", "/r:System.Web.Extensions.dll",
        "/out:" + str(output), "/resource:" + str(payload_path) + ",payload.zip"]
    if icon is not None:
        icon = Path(icon).resolve()
        if not icon.is_file():
            raise ValueError("The application icon is missing.")
        command.append("/win32icon:" + str(icon))
    if logo is not None:
        logo = Path(logo).resolve()
        if not logo.is_file():
            raise ValueError("The installer logo is missing.")
        command.append("/resource:" + str(logo) + ",branding.png")
    command.append(str(source_path))
    subprocess.run(command, cwd=ROOT, check=True)
    with output.open("rb") as stream:
        installer_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    update = dict(schema=1, application=APPLICATION, version=version, package_type="delta",
        base_version=base_version, installer=dict(filename=output.name, asset_name=output.name,
            sha256=installer_hash, size=output.stat().st_size),
        notes=f"Incremental update from {base_version} to {version}. Saved station data is preserved.")
    update_path.write_text(json.dumps(update, indent=2) + "\n", encoding="utf-8")
    checksum_path.write_text(installer_hash + "  " + output.name + "\n", encoding="ascii")
    report = dict(application=APPLICATION, base_version=base_version, version=version,
        artifact=str(output), bytes=output.stat().st_size, sha256=installer_hash,
        payload=str(payload_path), payload_bytes=payload_path.stat().st_size,
        changed_files=changed, removed_files=removed, full_target_file_count=len(target_manifest),
        local_update_manifest=str(update_path), station_data_included=False,
        archive_reconstruction_verified=True, installer_compiled=True,
        installer_executed=False, installed_application_modified=False,
        published=False, code_signed=False, hardware_access=False)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True, help="Exact complete ZIP used for the installed base version")
    parser.add_argument("--target", type=Path, required=True, help="Verified complete ZIP for the new release")
    parser.add_argument("--version", required=True, help="New release version; must match target VERSION.json")
    parser.add_argument("--output", type=Path, required=True, help="New incremental .exe output path")
    parser.add_argument("--icon", type=Path, help="Optional application ICO to embed in the installer")
    parser.add_argument("--logo", type=Path, help="Optional PNG logo to embed in the installer header")
    args = parser.parse_args(argv)
    try:
        report = build(args.base, args.target, args.version, args.output, icon=args.icon, logo=args.logo)
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        parser.exit(1, "Delta build failed: " + str(exc) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
