"""Build Leanwatts Testbench Gen2 1.0 and its single-file installer.

Only packaging/compiler operations run here. Application imports and instrument
access are deliberately absent. Run after all release source edits are complete.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_gen2_delta import render_installer


ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.0"
PREFIX = "CAN-Calibration/"
BASE = ROOT / "dist/CAN-Calibration-Auto-7.5-win64.zip"
BASES = [ROOT / f"dist/CAN-Calibration-Bench-Automation-{v}-win64.zip" for v in ("7", "7.1", "7.2", "7.3")]
BASES += [ROOT / f"dist/CAN-Calibration-Auto-{v}-win64.zip" for v in ("7.4", "7.5")]
FULL = ROOT / "dist/Leanwatts-Testbench-Gen2-1.0-win64.zip"
PATCH = ROOT / "dist/CAN-Upgrade-to-Leanwatts-Testbench-Gen2-1.0.zip"
SETUP = ROOT / "dist/Leanwatts-Testbench-Gen2-Setup-1.0.exe"
REPORT = ROOT / "artifacts/gen2-1.0-packaging.json"
WORK = ROOT / "artifacts/installer-gen2-1.0-build"
CSC = Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe")
NOTE_NAME = "INSTALL-AND-UPDATE-GEN2-1.0.txt"
NOTE = """Leanwatts Testbench Gen2 1.0 - INSTALLATION AND UPDATES

RECOMMENDED: SINGLE-FILE INSTALLER
1. Finish/stop the charger process and close the operator session. Close both
   charger applications before installing.
2. Open Leanwatts-Testbench-Gen2-Setup-1.0.exe on the production laptop.
3. For a first installation, select the old calibration_data folder if it is
   not detected automatically. For a later update, existing data is retained.
4. Click Install, then Finish. Use the Leanwatts Testbench Gen2 desktop shortcut.

The per-user installation is in LocalAppData\\Programs\\Leanwatts-Testbench-Gen2.
It requires no separate Python installation. The existing CAN/serial drivers
remain in use. Saved recipes, station settings, operators and reports are
copied forward; an existing installed program is retained as a backup.
The installer does not switch Windows protections off or install drivers.
This locally built installer is not code-signed.

PORTABLE / EXISTING ZIP INSTALLATION
Extract Leanwatts-Testbench-Gen2-1.0-win64.zip into a new folder. Its internal
application folder remains CAN-Calibration for compatibility. Copy the previous
calibration_data folder into that application folder, then open
Leanwatts-Testbench-Gen2.exe or Start-CAN-Calibration.cmd. Alternatively extract
CAN-Upgrade-to-Leanwatts-Testbench-Gen2-1.0.zip
directly into an existing version 7 through 7.5 CAN-Calibration folder and
replace the included files. The cumulative update contains no station data.

ONLINE UPDATES
Settings provides private GitHub updates and Install local test update.
For private updates an administrator enters the repository owner/name and a
fine-grained token with Contents: read-only for that repository. The token is
saved with Windows account encryption, not bundled in this package. It must
be entered separately on each production laptop/Windows account. Never send
it in chat or include it in a release. GitHub browser login does not configure
the desktop application's private download credential automatically.

For an offline/local test update, keep the setup EXE and its matching
.update.json file together. Select the EXE in Settings. The app validates the
product, version, size and SHA-256, then offers Install and restart. A same-
version test reinstall requires explicit confirmation; downgrades are blocked.

Future normal releases can use small delta installers containing only changed
application files. They require the matching installed base. The first 1.0
installation is full. Small updates retain recipes, credentials and reports.
No update service is published by building this package. Publish the setup
and its manifest as private GitHub release assets before checking for updates.
The manifest asset is named update-manifest.json by default. Checksums protect
integrity; the trusted repository and its credentials control publication.
"""


def digest(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError("Unsafe archive path: " + name)
    if path.parts[0].casefold() == "calibration_data":
        raise ValueError("Release cannot contain station data.")


def compile_cs(source, output, *extra):
    command = [str(CSC), "/nologo", "/target:winexe", "/platform:anycpu", "/optimize+",
               "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll", "/out:" + str(output)]
    command.extend(extra)
    command.append(str(source))
    subprocess.run(command, cwd=ROOT, check=True)


def artifact(path):
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(checksum + "  " + path.name + "\n", encoding="ascii")
    return {"artifact": str(path), "bytes": path.stat().st_size, "sha256": checksum}


def main():
    if any(path.exists() for path in (FULL, PATCH, SETUP, REPORT)):
        raise SystemExit("Published Gen2 1.0 outputs already exist; preserving them.")
    WORK.mkdir(parents=True, exist_ok=True)
    launcher = WORK / "Leanwatts-Testbench-Gen2.exe"
    compile_cs(ROOT / "tools/CanCalibrationLauncher.cs", launcher)
    replacements = {}
    for source in sorted((ROOT / "leanwatts_can").rglob("*.py")):
        if "tests" not in source.parts and "__pycache__" not in source.parts:
            replacements[source.relative_to(ROOT).as_posix()] = source.read_bytes()
    for name in ("launch_calibration.py", "Start-CAN-Calibration.cmd", "Start-Original-UI.cmd",
                 "README-Portable.txt", "README-CAN-Calibration.md", "tools/README-UPDATE-PUBLISHING.txt"):
        replacements[name] = (ROOT / name).read_bytes()
    replacements["Leanwatts-Testbench-Gen2.exe"] = launcher.read_bytes()
    replacements[NOTE_NAME] = NOTE.encode("utf-8")
    replacements["VERSION.json"] = (json.dumps({"application": "leanwatts-testbench-gen2", "version": VERSION}, indent=2) + "\n").encode()
    for name in replacements:
        safe_name(name)
    with zipfile.ZipFile(BASE) as source:
        manifest = json.loads(source.read(PREFIX + "MANIFEST.json"))
        for name in manifest:
            safe_name(name)
        if any(info.filename[len(PREFIX):].casefold().startswith("calibration_data/") for info in source.infolist()):
            raise ValueError("Base archive unexpectedly contains saved station data.")
        for name, data in replacements.items():
            manifest[name] = digest(data)
        changed = set()
        for previous in BASES:
            with zipfile.ZipFile(previous) as archive:
                older = json.loads(archive.read(PREFIX + "MANIFEST.json"))
            if set(older) - set(manifest):
                raise ValueError("Release removes files; explicit migration is required.")
            changed.update(name for name, entry in manifest.items() if older.get(name) != entry)
        patch_files = {}
        for name in sorted(changed):
            safe_name(name)
            data = replacements[name] if name in replacements else source.read(PREFIX + name)
            if digest(data) != manifest[name]:
                raise ValueError("Base archive differs from its manifest: " + name)
            patch_files[name] = data
        manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
        replacements["MANIFEST.json"] = patch_files["MANIFEST.json"] = manifest_bytes
        with zipfile.ZipFile(FULL, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
            for info in source.infolist():
                if not info.filename.startswith(PREFIX):
                    raise ValueError("Unexpected base package root.")
                if info.filename[len(PREFIX):] not in replacements:
                    output.writestr(info, source.read(info.filename))
            for name, data in replacements.items():
                output.writestr(PREFIX + name, data)
        with zipfile.ZipFile(PATCH, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
            for name, data in patch_files.items():
                output.writestr(name, data)
    with zipfile.ZipFile(FULL) as archive:
        for name, expected in manifest.items():
            if digest(archive.read(PREFIX + name)) != expected:
                raise ValueError("Full release integrity mismatch: " + name)
    with zipfile.ZipFile(PATCH) as archive:
        for name in changed:
            if digest(archive.read(name)) != manifest[name]:
                raise ValueError("Cumulative release integrity mismatch: " + name)
    full_info = artifact(FULL)
    patch_info = artifact(PATCH)
    source = (ROOT / "tools/CanCalibrationSetup.cs").read_text(encoding="utf-8")
    generated = WORK / "CanCalibrationSetup.cs"
    generated.write_text(render_installer(source, VERSION, full_info["sha256"], incremental=False), encoding="utf-8")
    compile_cs(generated, SETUP, "/r:System.IO.Compression.dll", "/r:System.IO.Compression.FileSystem.dll",
               "/r:System.Web.Extensions.dll", "/resource:" + str(FULL) + ",payload.zip")
    setup_info = artifact(SETUP)
    template = {"schema": 1, "application": "leanwatts-testbench-gen2", "version": VERSION,
                "package_type": "full",
                "installer": {"filename": SETUP.name, "asset_name": SETUP.name,
                              "sha256": setup_info["sha256"], "size": setup_info["bytes"]},
                "notes": "Operator sessions, combined calibration and post-calibration tests, session reports, and a desktop installer."}
    SETUP.with_suffix(".update.json").write_text(json.dumps(template, indent=2), encoding="utf-8")
    (ROOT / "dist/update-manifest-gen2-1.0.template.json").write_text(json.dumps(template, indent=2), encoding="utf-8")
    report = {"version": VERSION, "artifacts": [setup_info, patch_info, full_info],
              "manifest_files": len(manifest), "cumulative_changed_files": sorted(changed),
              "station_data_included": False, "archive_integrity_verified": True,
              "installer_compiled": True, "installer_executed": False,
              "code_signed": False, "online_feed_published": False, "hardware_access": False}
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
