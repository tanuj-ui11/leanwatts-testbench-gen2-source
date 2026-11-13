"""Package Gen2 1.1 with branded Windows icons and an exact 1.0 delta.

This preserves the previously delivered 1.0 files. It performs no installation,
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
from build_gen2_source import build as build_source
from leanwatts_can.version import VERSION as APPLICATION_VERSION

VERSION = "1.1"
PREFIX = "CAN-Calibration/"
BASE = ROOT / "dist/Leanwatts-Testbench-Gen2-1.0-win64.zip"
FULL = ROOT / f"dist/Leanwatts-Testbench-Gen2-{VERSION}-win64.zip"
SETUP = ROOT / f"dist/Leanwatts-Testbench-Gen2-Setup-{VERSION}.exe"
DELTA = ROOT / f"dist/Leanwatts-Testbench-Gen2-Update-1.0-to-{VERSION}.exe"
REPORT = ROOT / f"artifacts/gen2-{VERSION}-packaging.json"
SOURCE = ROOT / f"dist/Leanwatts-Testbench-Gen2-{VERSION}-source.zip"
WORK = ROOT / f"artifacts/installer-gen2-{VERSION}-build"
ICON = ROOT / "leanwatts_can/assets/leanwatts.ico"
LOGO = ROOT / "leanwatts_can/assets/leanwatts-logo-light.png"
NOTES = "Leanwatts branding and Windows icons; Light/Dark appearance with three accent colours; About; Production Lead operator-password resets and verified own-password changes; public updates for future releases. Calibration and test algorithms are unchanged."

NOTE = """Leanwatts Testbench Gen2 1.1 - INSTALLATION AND UPDATES

NEW INSTALLATION OR UPGRADE FROM THE OLD 7.x APPLICATION
Copy Leanwatts-Testbench-Gen2-Setup-1.1.exe to the production laptop and run it.
Close the charger applications first. Select the old calibration_data folder
when asked. The installer retains saved recipes, settings, users and reports,
creates a desktop shortcut and requires no separate Python installation.

UPDATE AN INSTALLED GEN2 1.0 STATION
Use Leanwatts-Testbench-Gen2-Update-1.0-to-1.1.exe for the small changed-files
update. It requires the exact installed 1.0 application. Keep its matching
.update.json beside it when selecting it through Settings > Software updates
> Install local test update. The full 1.1 installer is the fallback for a
different or modified starting installation.

The portable ZIP is an alternative: extract to a new folder, copy the previous
calibration_data into CAN-Calibration, then open Leanwatts-Testbench-Gen2.exe.

APPEARANCE
Settings includes Light/Dark appearance, three accent colours, and About.
The application's header, Windows icon, pages and reports use subtle Leanwatts
branding. Theme selection is saved for this station and does not change
charger recipes or instrument behavior.

ACCOUNTS
Administrators create operators and production leads in Settings. Production
leads can reset operator passwords only. Users changing their own password
must enter the current password. No default password is shipped.

UPDATES AND DATA
The program is installed in LocalAppData\\Programs\\Leanwatts-Testbench-Gen2.
Updating keeps station data and retains the prior installation as a backup.
The public GitHub release channel is configured in the application. Production
laptops do not need a GitHub login or token. Local installation also works
without an Internet connection. Never include credentials in release files.
Version 1.1 is distributed locally; the online channel is for future releases.
The installer is unsigned. It does not change drivers or Windows protections.
"""


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
    if any(path.exists() for path in (FULL, SETUP, DELTA, SOURCE, REPORT)):
        raise SystemExit("Gen2 1.1 outputs already exist; preserving them.")
    if not ICON.is_file() or not LOGO.is_file():
        raise SystemExit("Add the approved Leanwatts ICO and light-background PNG before packaging.")
    files, _, _, base_version = read_release(BASE)
    if base_version != "1.0":
        raise ValueError("The previous released package must be Gen2 1.0.")
    WORK.mkdir(parents=True, exist_ok=True)
    launcher_source = WORK / "CanCalibrationLauncher.cs"
    launcher_source.write_text(render_launcher((ROOT / "tools/CanCalibrationLauncher.cs").read_text(encoding="utf-8"), VERSION), encoding="utf-8")
    launcher = WORK / "Leanwatts-Testbench-Gen2.exe"
    compile_cs(launcher_source, launcher, "/win32icon:" + str(ICON))
    for source in sorted((ROOT / "leanwatts_can").rglob("*.py")):
        if "tests" not in source.parts and "__pycache__" not in source.parts:
            files[source.relative_to(ROOT).as_posix()] = source.read_bytes()
    for folder in (ROOT / "leanwatts_can/assets", ROOT / "leanwatts_can/fonts"):
        if folder.is_dir():
            for source in sorted(folder.rglob("*")):
                if source.is_file() and "__pycache__" not in source.parts:
                    files[source.relative_to(ROOT).as_posix()] = source.read_bytes()
    for name in ("launch_calibration.py", "Start-CAN-Calibration.cmd", "Start-Original-UI.cmd",
                 "README-Portable.txt", "README-CAN-Calibration.md", "tools/README-UPDATE-PUBLISHING.txt",
                 "leanwatts_can/release_channel.json"):
        files[name] = (ROOT / name).read_bytes()
    files["Leanwatts-Testbench-Gen2.exe"] = launcher.read_bytes()
    files.pop("INSTALL-AND-UPDATE-GEN2-1.0.txt", None)
    files["INSTALL-AND-UPDATE-GEN2-1.1.txt"] = NOTE.encode("utf-8")
    files["VERSION.json"] = (json.dumps({"application": "leanwatts-testbench-gen2", "version": VERSION}, indent=2) + "\n").encode()
    for name in files:
        safe_name(name)
    manifest = {name: digest(data) for name, data in sorted(files.items())}
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
    del verified, files
    delta_info = build_delta(BASE, FULL, VERSION, DELTA, icon=ICON, logo=LOGO)
    delta_manifest = json.loads(DELTA.with_suffix(".update.json").read_text(encoding="utf-8"))
    combined = {"schema": 1, "application": "leanwatts-testbench-gen2", "version": VERSION,
                "notes": NOTES, "packages": [
                    {key: delta_manifest[key] for key in ("package_type", "base_version", "installer")},
                    {key: full_manifest[key] for key in ("package_type", "installer")}]}
    (ROOT / "dist/update-manifest-gen2-1.1.json").write_text(json.dumps(combined, indent=2) + "\n", encoding="utf-8")
    source_info = build_source(VERSION, SOURCE)
    report = {"version": VERSION, "artifacts": [setup_info, full_info, delta_info, source_info],
              "manifest_files": len(manifest), "windows_icons_embedded": True,
              "station_data_included": False, "archive_integrity_verified": True,
              "installer_compiled": True, "installer_executed": False,
              "code_signed": False, "online_feed_published": False, "hardware_access": False,
              "original_1_0_preserved": True}
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
