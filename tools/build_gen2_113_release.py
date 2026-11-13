"""Package Gen2 1.1.3 calibration workflow and tuning update and an exact 1.1.2 delta.

This preserves the previously delivered 1.0 and 1.1.2 files. It performs no installation,
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

VERSION = "1.1.3"
PREFIX = "CAN-Calibration/"
BASE = ROOT / "dist/Leanwatts-Testbench-Gen2-1.1.2-win64.zip"
FULL = ROOT / f"dist/Leanwatts-Testbench-Gen2-{VERSION}-win64.zip"
SETUP = ROOT / f"dist/Leanwatts-Testbench-Gen2-Setup-{VERSION}.exe"
DELTA = ROOT / f"dist/Leanwatts-Testbench-Gen2-Update-1.1.2-to-{VERSION}.exe"
REPORT = ROOT / f"artifacts/gen2-{VERSION}-packaging.json"
SOURCE = ROOT / f"dist/Leanwatts-Testbench-Gen2-{VERSION}-source.zip"
WORK = ROOT / f"artifacts/installer-gen2-{VERSION}-build"
LOCAL = ROOT / f"dist/Leanwatts-Testbench-Gen2-Update-1.1.2-to-{VERSION}-local.zip"
ICON = ROOT / "leanwatts_can/assets/leanwatts.ico"
LOGO = ROOT / "leanwatts_can/assets/leanwatts-logo-light.png"
APP_CHANGES = (
    "leanwatts_can/automation.py", "leanwatts_can/core.py", "leanwatts_can/gui.py",
    "leanwatts_can/session_page.py", "leanwatts_can/session_store.py",
    "leanwatts_can/session_history.py", "leanwatts_can/production_flow.py",
    "leanwatts_can/version.py", "leanwatts_can/about_page.py",
)
SOURCE_TESTS = (
    "leanwatts_can/tests/test_release_113.py",
    "leanwatts_can/tests/test_session_workflow_options.py",
)
ALLOWED_CHANGES = set(APP_CHANGES) | {
    "Leanwatts-Testbench-Gen2.exe", "VERSION.json",
    "INSTALL-AND-UPDATE-GEN2-1.1.2.txt", "INSTALL-AND-UPDATE-GEN2-1.1.3.txt",
}
NOTES = "Two workflow buttons: parameters plus calibration, with optional post-tests. Faster bounded current adjustments after learning the response. Up to two voltage refinements toward the exact target. No-load post-test response timeout is 15 seconds. Full final stability and limits remain enforced. Existing station data is preserved."

NOTE = """Leanwatts Testbench Gen2 1.1.3

Close the application and run Leanwatts-Testbench-Gen2-Update-1.1.2-to-1.1.3.exe
on a station with the exact installed 1.1.2 release. The EXE works by itself.
For another version, a portable copy or a modified installation, use the full
Leanwatts-Testbench-Gen2-Setup-1.1.3.exe instead. Saved station data is preserved.

Parameters + Calibration checks/updates charger parameters and calibrates both
axes. It records CALIBRATED with post-tests NOT RUN, not a full production PASS.
Parameters + Calibration + Test adds the existing full-current, end-voltage and
cutoff tests. Scanner Enter and F5 select calibration only.

Current calibration can use up to 3 duty points per coarse adjustment only after
two separated responses establish sensitivity. Near target it uses smaller
steps. Voltage calibration can make up to two minimum-duty corrections while
inside tolerance, within an 8-second adjustment window, if predicted to improve
accuracy. Final stable-reading confirmation, physical limits and duty bounds
remain enforced. Exact output depends on charger resolution and measurement noise.

Post-tests still use explicit CAN Start then listen-only measurement. If output
is not established within 15 seconds, stop with the last load readings. This
release does not establish why the production charger did not produce normal-mode
output. No recurring demand traffic or automatic CAN retries were added.

Calibration saves remain persistence-unverified until separately checked after
restart. Neither installer nor software changes Windows drivers/protections.
This is a local release; no GitHub publication or password recovery change.
"""

LOCAL_NOTE = """Leanwatts Testbench Gen2 - local update 1.1.2 to 1.1.3

1. Copy this ZIP to production and extract it.
2. Close Leanwatts Testbench Gen2.
3. Run Leanwatts-Testbench-Gen2-Update-1.1.2-to-1.1.3.exe.
4. Open the desktop shortcut. Use Parameters + Calibration for calibration only.

The EXE alone works without an application password or a JSON file.
Keep the matching .update.json beside it only when selecting the update through
Settings > Software updates > Install local test update.
For other versions or portable/modified copies use the full Setup 1.1.3 EXE.
Saved recipes, accounts, settings and reports are preserved.
"""


def source_snapshot(released_files):
    # Application bytes come from the actual package, never unfinished workspace files.
    source_files = {name: data for name, data in released_files.items()
                    if (name.startswith("leanwatts_can/") and
                        (name.endswith(".py") or name.startswith(("leanwatts_can/assets/", "leanwatts_can/fonts/"))))
                    or name in ("launch_calibration.py", "Start-CAN-Calibration.cmd", "Start-Original-UI.cmd",
                                "README-CAN-Calibration.md", "README-Portable.txt", "THIRD-PARTY-NOTICES.txt",
                                "leanwatts_can/release_channel.json", "INSTALL-AND-UPDATE-GEN2-1.1.3.txt")}
    # Supporting build files come from the frozen 1.1.2 source release.
    with zipfile.ZipFile(ROOT / "dist/Leanwatts-Testbench-Gen2-1.1.2-source.zip") as prior:
        old_prefix = "Leanwatts-Testbench-Gen2-1.1.2-source/"
        for entry in prior.namelist():
            name = entry.removeprefix(old_prefix)
            if entry.startswith(old_prefix) and (name.startswith("tools/") or name.startswith("leanwatts_can/tests/") or name in (".gitignore", "SOURCE-README.txt")):
                source_files[name] = prior.read(entry)
    for name in SOURCE_TESTS:
        source_files[name] = (ROOT / name).read_bytes()
    source_files["tools/build_gen2_113_release.py"] = Path(__file__).read_bytes()
    source_files["SOURCE-README.txt"] = ("Leanwatts Testbench Gen2 1.1.3 - calibration workflow and tuning update\n\n"
        "Application files exactly match the shipped 1.1.3 package. Development support\n"
        "comes from the frozen 1.1.2 source release. No station data, credentials, unfinished\n"
        "features or vendor runtime binaries are included. This release builder needs the\n"
        "exact 1.1.2 full ZIP and 1.1.2 source ZIP in dist to reproduce this changed-files update.\n"
        "Use the build_gen2_113_release.py script after preparing the approved source files.\n").encode("utf-8")
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
        raise SystemExit("Gen2 1.1.3 outputs already exist; preserving them.")
    if not ICON.is_file() or not LOGO.is_file():
        raise SystemExit("Add the approved Leanwatts ICO and light-background PNG before packaging.")
    files, baseline, _, base_version = read_release(BASE)
    if base_version != "1.1.2":
        raise ValueError("The previous released package must be Gen2 1.1.2.")
    WORK.mkdir(parents=True, exist_ok=True)
    launcher_source = WORK / "CanCalibrationLauncher.cs"
    launcher_source.write_text(render_launcher((ROOT / "tools/CanCalibrationLauncher.cs").read_text(encoding="utf-8"), VERSION), encoding="utf-8")
    launcher = WORK / "Leanwatts-Testbench-Gen2.exe"
    compile_cs(launcher_source, launcher, "/win32icon:" + str(ICON))
    for name in APP_CHANGES:
        files[name] = (ROOT / name).read_bytes()
    files["Leanwatts-Testbench-Gen2.exe"] = launcher.read_bytes()
    files.pop("INSTALL-AND-UPDATE-GEN2-1.1.2.txt", None)
    files["INSTALL-AND-UPDATE-GEN2-1.1.3.txt"] = NOTE.encode("utf-8")
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
    (ROOT / "dist/update-manifest-gen2-1.1.3.json").write_text(json.dumps(combined, indent=2) + "\n", encoding="utf-8")
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
