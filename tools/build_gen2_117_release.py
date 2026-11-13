"""Package Gen2 1.1.7 firmware tools plus 1.1.5 and 1.1.6 exact deltas.

This preserves all previously built releases. It performs no installation,
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

VERSION = "1.1.7"
PREFIX = "CAN-Calibration/"
BASE = ROOT / "dist/Leanwatts-Testbench-Gen2-1.1.6-win64.zip"
DELTA_BASE = ROOT / "dist/Leanwatts-Testbench-Gen2-1.1.5-win64.zip"
FULL = ROOT / f"dist/Leanwatts-Testbench-Gen2-{VERSION}-win64.zip"
SETUP = ROOT / f"dist/Leanwatts-Testbench-Gen2-Setup-{VERSION}.exe"
DELTA = ROOT / f"dist/Leanwatts-Testbench-Gen2-Update-1.1.5-to-{VERSION}.exe"
DELTA_116 = ROOT / f"dist/Leanwatts-Testbench-Gen2-Update-1.1.6-to-{VERSION}.exe"
REPORT = ROOT / f"artifacts/gen2-{VERSION}-packaging.json"
SOURCE = ROOT / f"dist/Leanwatts-Testbench-Gen2-{VERSION}-source.zip"
WORK = ROOT / f"artifacts/installer-gen2-{VERSION}-build"
LOCAL = ROOT / f"dist/Leanwatts-Testbench-Gen2-Update-1.1.5-to-{VERSION}-local.zip"
ICON = ROOT / "leanwatts_can/assets/leanwatts.ico"
LOGO = ROOT / "leanwatts_can/assets/leanwatts-logo-light.png"
APP_CHANGES = (
    "leanwatts_can/gui.py", "leanwatts_can/firmware_page.py",
    "leanwatts_can/firmware_controller.py", "leanwatts_can/gdlink.py",
    "leanwatts_can/test_program_controller.py",
    "leanwatts_can/version.py", "leanwatts_can/about_page.py",
)
SOURCE_TESTS = (
    "leanwatts_can/tests/test_gdlink.py",
    "leanwatts_can/tests/test_firmware_page.py",
    "leanwatts_can/tests/test_firmware_integration.py",
    "leanwatts_can/tests/test_program_integration.py",
    "leanwatts_can/tests/test_session_ui_v8.py",
)
ALLOWED_CHANGES = set(APP_CHANGES) | {
    "Leanwatts-Testbench-Gen2.exe", "VERSION.json",
    "INSTALL-AND-UPDATE-GEN2-1.1.6.txt", "INSTALL-AND-UPDATE-GEN2-1.1.7.txt",
}
NOTES = "Adds a separate Firmware Programming tab for the GD-Link workflow. Includes the 1.1.6 Test tab, editable data-only programs, revised startup baseline, experimental calibration-mode checks and CAN result bundles. A direct 1.1.5 update includes all changes; an optional 1.1.6 update is also provided. Station data is preserved."

NOTE = """Leanwatts Testbench Gen2 1.1.7 - FIRMWARE PROGRAMMING AND TEST PROGRAMS

Close the application and run Leanwatts-Testbench-Gen2-Update-1.1.5-to-1.1.7.exe
on a station with the exact installed 1.1.5 release. The EXE works by itself.
This includes 1.1.6's features; do not install an intermediate version first.
Stations already on 1.1.6 can use the smaller exact-base
Leanwatts-Testbench-Gen2-Update-1.1.6-to-1.1.7.exe instead.
For another version, a portable copy or modified installation, use the full
Leanwatts-Testbench-Gen2-Setup-1.1.7.exe. Station records are preserved.

FIRMWARE PROGRAMMING
Firmware programming has its own tab and does not run automatically with a
calibration/test cycle. Use the GD-Link workflow with the approved firmware
file and matching target setup. Device connection and programming require
explicit operator actions; opening the tab does not start device discovery.
Firmware images are not supplied in this installer. Read the tab's status and
recorded result; a software launch alone is not successful programming.

TEST TAB
Start an operator session and select the correct variant as usual. On Test,
choose 01-full-cycle-calibration-mode.json and scan the charger serial number.
Start the program, then switch charger AC ON promptly while the program waits
for startup voltage (15 seconds). The station cannot switch AC automatically.
The full cycle measures baseline, checks/updates parameters, calibrates current
and voltage, and performs experimental output/cutoff checks.

CHANGED BASELINE
The load is prepared in CV while input remains OFF. The first fresh voltage
above the engagement threshold (variant test voltage, normally 49 V) is captured
as the initial open-voltage snapshot, and the load input is enabled immediately.
This is a startup snapshot, not a stabilized no-load regulation test. Current
gets a bounded ramp/settling period before its baseline is recorded. Missing or
invalid baseline stops the unit before programming. Existing calibration tuning,
limits, session-reference trims and save behaviour are unchanged.

EXPERIMENTAL TEST METHOD
The supplied program sends calibration entry alone, then measures with CAN in
listen-only mode. It does not send 80/80 values, trim adjustments, parameter writes
or calibration saves during its testing phase. The full cycle's earlier parameter
and calibration phases still perform the intended programming and save requests.
Cutoff checks establish end voltage and hold above the cutoff for 30 seconds
before tapering load current. A cutoff not observed in calibration mode is
inconclusive; it is not proof of defective normal-operation cutoff. Mode entry's
firmware effects and retained saved trims need bench verification. These runs
are diagnostic, not normal production acceptance or persistence certification.

PROGRAM FILES
Open Program folder from Test. Add/duplicate a .json file, then click Refresh;
the dropdown uses filenames. Supported method/timing settings can change without
an app update. Programs contain validated data, not Python code or arbitrary CAN
frames. New unsupported actions/protocols still need driver/application support.
Existing edited station programs are preserved, not overwritten by examples.

RESULT BUNDLE
Each run saves its exact program, selected variant, measurements, activity and
CAN evidence. Copy result file copies the run ZIP for sending back for analysis.
Capture covers this application's open CAN handles; reconnect/closed-handle gaps
are identified and traffic before capture is not claimed recorded. An API-accepted
transmit does not prove charger acknowledgement or calibration persistence.

EXISTING PRODUCTION WORKFLOWS
The three production buttons and summary/detailed session reports remain. Their
baseline uses the revised early-load method; normal production tests do not
silently switch to the experimental calibration-mode startup.

No password recovery, GitHub publication or driver/protection changes are included.
"""

LOCAL_NOTE = """Leanwatts Testbench Gen2 - direct local update 1.1.5 to 1.1.7

1. Copy this ZIP to production and extract it.
2. Close Leanwatts Testbench Gen2.
3. Run Leanwatts-Testbench-Gen2-Update-1.1.5-to-1.1.7.exe.
4. Open Test, choose 01-full-cycle-calibration-mode.json and scan the charger.
5. Start program, then switch charger AC ON while waiting for voltage (15 s).
6. After completion or stopping, use Copy result file to share the run ZIP.

Firmware Programming is a separate tab for the GD-Link workflow. It does not
run automatically during calibration or testing. Use the approved firmware
and target setup; no firmware image is supplied with this update.

The Test tab runs diagnostic programs loaded by filename from its Program folder.
The initial program includes baseline, parameters, calibration and experimental
calibration-mode tests. Its test phase blocks trim/parameter/save writes. Normal
charging/cutoff equivalence is not certified by these diagnostic results.

No JSON is needed when running the EXE directly. Keep the matching .update.json
beside it only when using Settings > Software updates > Install local test update.
Other starting versions or modified installations need the full Setup 1.1.7 EXE.
Saved accounts, recipes, settings and reports are preserved.
"""


def source_snapshot(released_files):
    # Application bytes come from the actual package, never unfinished workspace files.
    source_files = {name: data for name, data in released_files.items()
                    if (name.startswith("leanwatts_can/") and
                        (name.endswith(".py") or name.startswith(("leanwatts_can/assets/", "leanwatts_can/fonts/", "leanwatts_can/test_programs/"))))
                    or name in ("launch_calibration.py", "Start-CAN-Calibration.cmd", "Start-Original-UI.cmd",
                                "README-CAN-Calibration.md", "README-Portable.txt", "THIRD-PARTY-NOTICES.txt",
                                "leanwatts_can/release_channel.json", "INSTALL-AND-UPDATE-GEN2-1.1.7.txt")}
    # Supporting build files come from the frozen 1.1.6 source release.
    with zipfile.ZipFile(ROOT / "dist/Leanwatts-Testbench-Gen2-1.1.6-source.zip") as prior:
        old_prefix = "Leanwatts-Testbench-Gen2-1.1.6-source/"
        for entry in prior.namelist():
            name = entry.removeprefix(old_prefix)
            if entry.startswith(old_prefix) and (name.startswith("tools/") or name.startswith("leanwatts_can/tests/") or name in (".gitignore", "SOURCE-README.txt")):
                source_files[name] = prior.read(entry)
    for name in SOURCE_TESTS:
        source_files[name] = (ROOT / name).read_bytes()
    source_files["tools/build_gen2_117_release.py"] = Path(__file__).read_bytes()
    source_files["tools/verify_gen2_package.py"] = (ROOT / "tools/verify_gen2_package.py").read_bytes()
    source_files["SOURCE-README.txt"] = ("Leanwatts Testbench Gen2 1.1.7 - firmware programming and test programs\n\n"
        "Application files exactly match the shipped 1.1.7 package. Development support\n"
        "comes from the frozen 1.1.6 source release. No station data, credentials, unfinished\n"
        "features or vendor runtime binaries are included. This release builder needs the\n"
        "exact 1.1.5 and 1.1.6 full ZIPs plus 1.1.6 source ZIP in dist to reproduce both deltas.\n"
        "Use the build_gen2_117_release.py script after preparing the approved source files.\n").encode("utf-8")
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
    if any(path.exists() for path in (FULL, SETUP, DELTA, DELTA_116, SOURCE, REPORT, LOCAL)):
        raise SystemExit("Gen2 1.1.7 outputs already exist; preserving them.")
    if not ICON.is_file() or not LOGO.is_file():
        raise SystemExit("Add the approved Leanwatts ICO and light-background PNG before packaging.")
    files, baseline, _, base_version = read_release(BASE)
    if base_version != "1.1.6":
        raise ValueError("The overlay base must be frozen Gen2 1.1.6.")
    with zipfile.ZipFile(DELTA_BASE) as prior:
        if json.loads(prior.read(PREFIX + "VERSION.json"))["version"] != "1.1.5":
            raise ValueError("The primary delta base must be frozen Gen2 1.1.5.")
    WORK.mkdir(parents=True, exist_ok=True)
    launcher_source = WORK / "CanCalibrationLauncher.cs"
    launcher_source.write_text(render_launcher((ROOT / "tools/CanCalibrationLauncher.cs").read_text(encoding="utf-8"), VERSION), encoding="utf-8")
    launcher = WORK / "Leanwatts-Testbench-Gen2.exe"
    compile_cs(launcher_source, launcher, "/win32icon:" + str(ICON))
    for name in APP_CHANGES:
        files[name] = (ROOT / name).read_bytes()
    files["Leanwatts-Testbench-Gen2.exe"] = launcher.read_bytes()
    files.pop("INSTALL-AND-UPDATE-GEN2-1.1.6.txt", None)
    files["INSTALL-AND-UPDATE-GEN2-1.1.7.txt"] = NOTE.encode("utf-8")
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
    delta_info = build_delta(DELTA_BASE, FULL, VERSION, DELTA, icon=ICON, logo=LOGO)
    delta_116_info = build_delta(BASE, FULL, VERSION, DELTA_116, icon=ICON, logo=LOGO)
    delta_manifest = json.loads(DELTA.with_suffix(".update.json").read_text(encoding="utf-8"))
    delta_116_manifest = json.loads(DELTA_116.with_suffix(".update.json").read_text(encoding="utf-8"))
    combined = {"schema": 1, "application": "leanwatts-testbench-gen2", "version": VERSION,
                "notes": NOTES, "packages": [
                    {key: delta_manifest[key] for key in ("package_type", "base_version", "installer")},
                    {key: delta_116_manifest[key] for key in ("package_type", "base_version", "installer")},
                    {key: full_manifest[key] for key in ("package_type", "installer")}]}
    (ROOT / "dist/update-manifest-gen2-1.1.7.json").write_text(json.dumps(combined, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(LOCAL, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(DELTA, DELTA.name)
        archive.write(DELTA.with_suffix(".update.json"), DELTA.with_suffix(".update.json").name)
        archive.writestr("READ-ME.txt", LOCAL_NOTE)
    local_info = artifact(LOCAL)
    report = {"version": VERSION, "artifacts": [setup_info, full_info, delta_info, delta_116_info, local_info, source_info],
              "manifest_files": len(manifest), "approved_changed_paths": sorted(changed_paths), "windows_icons_embedded": True,
              "station_data_included": False, "archive_integrity_verified": True,
              "installer_compiled": True, "installer_executed": False,
              "code_signed": False, "online_feed_published": False, "hardware_access": False,
              "original_1_0_preserved": True, "original_1_1_preserved": True, "original_1_1_1_preserved": True,
              "original_1_1_5_preserved": True,
              "original_1_1_6_preserved": True,
              "test_program_format": "Validated data-only JSON; no arbitrary script execution"}
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
