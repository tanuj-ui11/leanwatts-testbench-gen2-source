"""Package Gen2 1.1.2 bounded charger startup wait and an exact 1.1.1 delta.

This preserves the previously delivered 1.0 and 1.1.1 files. It performs no installation,
publication or device access. Run only after application sources are frozen.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from build_gen2_delta import build as build_delta, digest, read_release, render_installer, safe_name, version_parts
from build_gen2_release import artifact, compile_cs
from leanwatts_can.version import VERSION as APPLICATION_VERSION

VERSION = "1.1.2"
PREFIX = "CAN-Calibration/"
BASE = ROOT / "dist/Leanwatts-Testbench-Gen2-1.1.1-win64.zip"
FULL = ROOT / f"dist/Leanwatts-Testbench-Gen2-{VERSION}-win64.zip"
SETUP = ROOT / f"dist/Leanwatts-Testbench-Gen2-Setup-{VERSION}.exe"
DELTA = ROOT / f"dist/Leanwatts-Testbench-Gen2-Update-1.1.1-to-{VERSION}.exe"
REPORT = ROOT / f"artifacts/gen2-{VERSION}-packaging.json"
SOURCE = ROOT / f"dist/Leanwatts-Testbench-Gen2-{VERSION}-source.zip"
WORK = ROOT / f"artifacts/installer-gen2-{VERSION}-build"
LOCAL = ROOT / f"dist/Leanwatts-Testbench-Gen2-Update-1.1.1-to-{VERSION}-local.zip"
ICON = ROOT / "leanwatts_can/assets/leanwatts.ico"
LOGO = ROOT / "leanwatts_can/assets/leanwatts-logo-light.png"
APP_CHANGES = (
    "leanwatts_can/automation.py", "leanwatts_can/version.py",
    "leanwatts_can/about_page.py", "leanwatts_can/bench_session.py",
)
SOURCE_TESTS = (
    "leanwatts_can/tests/test_charger_fault_workflow.py",
)
ALLOWED_CHANGES = set(APP_CHANGES) | {
    "Leanwatts-Testbench-Gen2.exe", "VERSION.json",
    "INSTALL-AND-UPDATE-GEN2-1.1.1.txt", "INSTALL-AND-UPDATE-GEN2-1.1.2.txt",
}
NOTES = "Diagnostic charger startup can wait up to 15 seconds for status 0x0020 to clear in fresh telemetry. No calibration adjustment or save proceeds before fresh fault-clear confirmation. Independent limits and other fault checks remain active; deadline expiry stops the process. This software change does not establish the physical cause of the status. Saved station data is preserved."

NOTE = """Leanwatts Testbench Gen2 1.1.2 - CHARGER STARTUP WAIT FIX

UPDATE AN INSTALLED GEN2 1.1.1 STATION
Close Leanwatts Testbench Gen2 and run
Leanwatts-Testbench-Gen2-Update-1.1.1-to-1.1.2.exe.
This small changed-files update requires the exact installed Gen2 1.1.1 release.
Extract the local update ZIP first. Keep the matching .update.json beside the
EXE if selecting it through Settings > Software updates > Install local test
update. Running the update EXE directly does not require an app password.

NEW INSTALLATION OR DIFFERENT STARTING VERSION
Use Leanwatts-Testbench-Gen2-Setup-1.1.2.exe. It includes the complete runtime
and creates the desktop shortcut. Close charger applications before updating.
The full installer is also the fallback for a modified starting installation.

CHARGER STARTUP
Diagnostic startup can wait up to 15 seconds for status 0x0020 to clear.
Fresh charger telemetry must confirm fault-clear before any calibration
adjustment or calibration save can proceed. Other faults and independent
limits remain active; expiry of the startup deadline stops the process.
This software change does not establish the physical cause of status 0x0020.

SAVED DATA AND UPDATES
Updating preserves station accounts, recipes, settings and session reports.
The installer retains the prior installation as a backup. The public GitHub
channel remains configured for
future releases. This release is distributed locally. No online release is
published by this build. The installer is unsigned and does not change drivers
or Windows protections.
"""

LOCAL_NOTE = """Leanwatts Testbench Gen2 - local update 1.1.1 to 1.1.2

1. Copy this ZIP to the production laptop and extract it.
2. Close Leanwatts Testbench Gen2.
3. Run Leanwatts-Testbench-Gen2-Update-1.1.1-to-1.1.2.exe.
4. Open the desktop shortcut and retry the calibration session.
   Diagnostic startup can wait up to 15 seconds for status 0x0020 to clear.
   No calibration adjustment or save proceeds before fresh fault-clear data.
   Other faults, independent limits and the startup deadline remain enforced.

An app administrator password is not required to run this update installer.
The matching .update.json is included for Settings > Software updates > Install
local test update. Keep it beside the EXE when using that route.

Only an exact installed Gen2 1.1.1 application can use this small update. For new,
old 7.x, portable or modified installations, use the full Setup 1.1.2 EXE.
Saved accounts, recipes, settings and reports are preserved.
"""


def source_snapshot(released_files):
    # Application bytes come from the actual package, never unfinished workspace files.
    source_files = {name: data for name, data in released_files.items()
                    if (name.startswith("leanwatts_can/") and
                        (name.endswith(".py") or name.startswith(("leanwatts_can/assets/", "leanwatts_can/fonts/"))))
                    or name in ("launch_calibration.py", "Start-CAN-Calibration.cmd", "Start-Original-UI.cmd",
                                "README-CAN-Calibration.md", "README-Portable.txt", "THIRD-PARTY-NOTICES.txt",
                                "leanwatts_can/release_channel.json", "INSTALL-AND-UPDATE-GEN2-1.1.2.txt")}
    # Supporting build files come from the frozen 1.1.1 source release.
    with zipfile.ZipFile(ROOT / "dist/Leanwatts-Testbench-Gen2-1.1.1-source.zip") as prior:
        old_prefix = "Leanwatts-Testbench-Gen2-1.1.1-source/"
        for entry in prior.namelist():
            name = entry.removeprefix(old_prefix)
            if entry.startswith(old_prefix) and (name.startswith("tools/") or name.startswith("leanwatts_can/tests/") or name in (".gitignore", "SOURCE-README.txt")):
                source_files[name] = prior.read(entry)
    for name in SOURCE_TESTS:
        source_files[name] = (ROOT / name).read_bytes()
    source_files["tools/build_gen2_112_release.py"] = Path(__file__).read_bytes()
    source_files["SOURCE-README.txt"] = ("Leanwatts Testbench Gen2 1.1.2 - bounded charger startup wait\n\n"
        "Application files exactly match the shipped 1.1.2 package. Development support\n"
        "comes from the frozen 1.1.1 source release. No station data, credentials, unfinished\n"
        "features or vendor runtime binaries are included. This release builder needs the\n"
        "exact 1.1.1 full ZIP and 1.1.1 source ZIP in dist to reproduce this changed-files update.\n"
        "Use the build_gen2_112_release.py script after preparing the approved source files.\n").encode("utf-8")
    prefix = f"Leanwatts-Testbench-Gen2-{VERSION}-source/"
    with zipfile.ZipFile(SOURCE, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(source_files.items()):
            safe_name(name)
            archive.writestr(prefix + name, data)
    with zipfile.ZipFile(SOURCE) as archive:
        if set(archive.namelist()) != {prefix + name for name in source_files}:
            raise ValueError("Unexpected source snapshot entries.")
        if any(archive.read(prefix + name) != data for name, data in source_files.items()):
            raise ValueError("Source snapshot bytes differ from the released sources.")
    info = artifact(SOURCE)
    info.update(source_files=len(source_files), station_data_included=False,
                credentials_included=False, unfinished_features_included=False)
    return info


def render_launcher(source, version):
    numeric = ".".join(str(part) for part in version_parts(version))
    for field in ("AssemblyVersion", "AssemblyFileVersion"):
        source, count = re.subn(rf'{field}\("[^"]+"\)', f'{field}("{numeric}")', source)
        if count != 1:
            raise ValueError("Launcher version marker is missing: " + field)
    return source


def main():
    if APPLICATION_VERSION != VERSION:
        raise SystemExit("Application version does not match this release builder.")
    if any(path.exists() for path in (FULL, SETUP, DELTA, SOURCE, REPORT, LOCAL)):
        raise SystemExit("Gen2 1.1.2 outputs already exist; preserving them.")
    if not ICON.is_file() or not LOGO.is_file():
        raise SystemExit("Add the approved Leanwatts ICO and light-background PNG before packaging.")
    files, baseline, _, base_version = read_release(BASE)
    if base_version != "1.1.1":
        raise ValueError("The previous released package must be Gen2 1.1.1.")
    WORK.mkdir(parents=True, exist_ok=True)
    launcher_source = WORK / "CanCalibrationLauncher.cs"
    launcher_source.write_text(render_launcher((ROOT / "tools/CanCalibrationLauncher.cs").read_text(encoding="utf-8"), VERSION), encoding="utf-8")
    launcher = WORK / "Leanwatts-Testbench-Gen2.exe"
    compile_cs(launcher_source, launcher, "/win32icon:" + str(ICON))
    for name in APP_CHANGES:
        files[name] = (ROOT / name).read_bytes()
    files["Leanwatts-Testbench-Gen2.exe"] = launcher.read_bytes()
    files.pop("INSTALL-AND-UPDATE-GEN2-1.1.1.txt", None)
    files["INSTALL-AND-UPDATE-GEN2-1.1.2.txt"] = NOTE.encode("utf-8")
    files["VERSION.json"] = (json.dumps({"application": "leanwatts-testbench-gen2", "version": VERSION}, indent=2) + "\n").encode()
    for name in files:
        safe_name(name)
    manifest = {name: digest(data) for name, data in sorted(files.items())}
    changed_paths = {name for name in set(baseline) | set(manifest) if baseline.get(name) != manifest.get(name)}
    if not changed_paths <= ALLOWED_CHANGES:
        raise ValueError("Unexpected release changes: " + repr(sorted(changed_paths - ALLOWED_CHANGES)))
    with zipfile.ZipFile(FULL, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(PREFIX + name, data)
        archive.writestr(PREFIX + "MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    verified, _, _, version = read_release(FULL)
    if version != VERSION or verified != files:
        raise ValueError("The assembled full archive differs from its sources.")
    full_info = artifact(FULL)
    source = (ROOT / "tools/CanCalibrationSetup.cs").read_text(encoding="utf-8")
    generated = WORK / "CanCalibrationSetup.cs"
    generated.write_text(render_installer(source, VERSION, full_info["sha256"], incremental=False), encoding="utf-8")
    compile_cs(generated, SETUP, "/win32icon:" + str(ICON), "/r:System.IO.Compression.dll",
               "/r:System.IO.Compression.FileSystem.dll", "/r:System.Web.Extensions.dll",
               "/resource:" + str(FULL) + ",payload.zip", "/resource:" + str(LOGO) + ",branding.png")
    setup_info = artifact(SETUP)
    full_manifest = {"schema": 1, "application": "leanwatts-testbench-gen2", "version": VERSION,
                     "package_type": "full", "notes": NOTES,
                     "installer": {"filename": SETUP.name, "asset_name": SETUP.name,
                                   "sha256": setup_info["sha256"], "size": setup_info["bytes"]}}
    SETUP.with_suffix(".update.json").write_text(json.dumps(full_manifest, indent=2) + "\n", encoding="utf-8")
    # The delta builder verifies both archives independently; release these
    # complete runtime buffers before it reads the packages again.
    source_info = source_snapshot(files)
    del verified, files
    delta_info = build_delta(BASE, FULL, VERSION, DELTA, icon=ICON, logo=LOGO)
    delta_manifest = json.loads(DELTA.with_suffix(".update.json").read_text(encoding="utf-8"))
    combined = {"schema": 1, "application": "leanwatts-testbench-gen2", "version": VERSION,
                "notes": NOTES, "packages": [
                    {key: delta_manifest[key] for key in ("package_type", "base_version", "installer")},
                    {key: full_manifest[key] for key in ("package_type", "installer")}]}
    (ROOT / "dist/update-manifest-gen2-1.1.2.json").write_text(json.dumps(combined, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(LOCAL, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(DELTA, DELTA.name)
        archive.write(DELTA.with_suffix(".update.json"), DELTA.with_suffix(".update.json").name)
        archive.writestr("READ-ME.txt", LOCAL_NOTE)
    local_info = artifact(LOCAL)
    report = {"version": VERSION, "artifacts": [setup_info, full_info, delta_info, local_info, source_info],
              "manifest_files": len(manifest), "approved_changed_paths": sorted(changed_paths), "windows_icons_embedded": True,
              "station_data_included": False, "archive_integrity_verified": True,
              "installer_compiled": True, "installer_executed": False,
              "code_signed": False, "online_feed_published": False, "hardware_access": False,
              "original_1_0_preserved": True, "original_1_1_preserved": True, "original_1_1_1_preserved": True}
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
