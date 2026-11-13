"""Build the frozen 1.1.11 -> 1.1.12 release; never install or access hardware.

All previous release artifacts are immutable inputs. Only the explicit source
allowlist overlays the verified 1.1.11 full archive.
"""
from __future__ import annotations

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

VERSION = "1.1.12"
BASE_VERSION = "1.1.11"
PREFIX = "CAN-Calibration/"
BASE = ROOT / f"dist/Leanwatts-Testbench-Gen2-{BASE_VERSION}-win64.zip"
BASE_SOURCE = ROOT / f"dist/Leanwatts-Testbench-Gen2-{BASE_VERSION}-source.zip"
FULL = ROOT / f"dist/Leanwatts-Testbench-Gen2-{VERSION}-win64.zip"
SETUP = ROOT / f"dist/Leanwatts-Testbench-Gen2-Setup-{VERSION}.exe"
DELTA = ROOT / f"dist/Leanwatts-Testbench-Gen2-Update-{BASE_VERSION}-to-{VERSION}.exe"
LOCAL = ROOT / f"dist/Leanwatts-Testbench-Gen2-Update-{BASE_VERSION}-to-{VERSION}-local.zip"
SOURCE = ROOT / f"dist/Leanwatts-Testbench-Gen2-{VERSION}-source.zip"
REPORT = ROOT / f"artifacts/gen2-{VERSION}-packaging.json"
WORK = ROOT / f"artifacts/installer-gen2-{VERSION}-build"
ICON = ROOT / "leanwatts_can/assets/leanwatts.ico"
LOGO = ROOT / "leanwatts_can/assets/leanwatts-logo-light.png"

# This is finalized with the feature owners before the release is built.
APP_CHANGES = (
    "leanwatts_can/automation.py", "leanwatts_can/production_flow.py", "leanwatts_can/test_programs.py",
    "leanwatts_can/test_program_page.py",
    "leanwatts_can/test_programs/05-ocv-and-full-current.json", "leanwatts_can/test_programs/README.txt",
    "leanwatts_can/session_page.py", "leanwatts_can/session_store.py", "leanwatts_can/session_history.py",
    "leanwatts_can/version.py", "leanwatts_can/about_page.py",
)
APP_REMOVALS = ("leanwatts_can/test_programs/04-ocv-current-fixed-cutoff.json",)
SOURCE_TESTS = (
    "leanwatts_can/tests/test_baseline.py", "leanwatts_can/tests/test_automation.py",
    "leanwatts_can/tests/test_bench_session.py", "leanwatts_can/tests/test_experimental_automation.py",
    "leanwatts_can/tests/test_bench_trace_program.py", "leanwatts_can/tests/test_program_integration.py",
    "leanwatts_can/tests/test_test_programs.py", "leanwatts_can/tests/test_normal_test_pipeline.py",
    "leanwatts_can/tests/test_test_program_page.py", "leanwatts_can/tests/test_test_restoration_119.py",
    "leanwatts_can/tests/test_session_ui_v8.py",
    "leanwatts_can/tests/test_session_workflow_options.py", "leanwatts_can/tests/test_session_pairing.py",
    "leanwatts_can/tests/test_simple_session_reports.py",
    "leanwatts_can/tests/test_production_sessions.py",
    "leanwatts_can/tests/test_external_restart_pipeline.py",
    "leanwatts_can/tests/test_external_restart_bench.py",
    "leanwatts_can/tests/test_ocv_current_only_pipeline.py",
)
SOURCE_TOOLS = (
    "tools/build_gen2_1112_release.py", "tools/verify_gen2_package.py",
    "tools/build_gen2_delta.py", "tools/build_gen2_release.py",
    "tools/CanCalibrationSetup.cs", "tools/CanCalibrationLauncher.cs",
    "tools/test_gen2_delta.py", "tools/test_gen2_installer_permissions.py",
)
ALLOWED_CHANGES = set(APP_CHANGES) | set(APP_REMOVALS) | {
    "Leanwatts-Testbench-Gen2.exe", "VERSION.json",
    f"INSTALL-AND-UPDATE-GEN2-{BASE_VERSION}.txt", f"INSTALL-AND-UPDATE-GEN2-{VERSION}.txt",
}
NOTES = "TODO: finalize 1.1.12 notes after source freeze."
NOTE = """Leanwatts Testbench Gen2 1.1.12

TODO: finalized OCV/current-only tests and corrected initial current display.
"""
LOCAL_NOTE = """Leanwatts Testbench Gen2 - local update 1.1.11 to 1.1.12

1. Copy this ZIP to the production laptop and extract it.
2. Close Leanwatts Testbench Gen2 and leave it closed during the update.
3. Run Leanwatts-Testbench-Gen2-Update-1.1.11-to-1.1.12.exe.
4. Reopen the desktop shortcut and confirm About shows 1.1.12.

The EXE works by itself. Its matching .update.json is only needed when using
Settings > Software updates > Install local test update. Other versions or
modified installations require the full Setup 1.1.12 EXE.
The small update replaces changed application files in place and retains a
recovery backup. It does not rename the installed application folder.
Station accounts, recipes, programs, settings and reports are preserved.
Vendor programmer software and firmware BIN files remain separately supplied.
"""


def assert_no_station_or_vendor_package(files):
    forbidden_names = {"gdlink_cli.exe", "gdlink.dll", "gddap.dll", "gd_mcu_dll.dll",
                       "firmware_profiles.json", "controlcan.dll"}
    for name in files:
        lowered = name.replace("\\", "/").lower()
        parts = lowered.split("/")
        if (lowered.startswith(("research/", "calibration_data/"))
                or any(part in {"gdutility-official", "gd-linkutilityprogrammer", "firmware_images", "waveshare"} for part in parts)
                or parts[-1] in forbidden_names):
            raise ValueError("Station or newly downloaded vendor content is excluded: " + name)


def render_launcher(source):
    numeric = ".".join(str(part) for part in version_parts(VERSION))
    for field in ("AssemblyVersion", "AssemblyFileVersion"):
        source, count = re.subn(rf'{field}\("[^\"]+"\)', f'{field}("{numeric}")', source)
        if count != 1:
            raise ValueError("Launcher version marker is missing: " + field)
    return source


def source_snapshot(released_files):
    source_files = {name: data for name, data in released_files.items()
                    if (name.startswith("leanwatts_can/") and
                        (name.endswith(".py") or name.startswith(("leanwatts_can/assets/", "leanwatts_can/fonts/", "leanwatts_can/test_programs/"))))
                    or name in ("launch_calibration.py", "Start-CAN-Calibration.cmd", "Start-Original-UI.cmd",
                                "README-CAN-Calibration.md", "README-Portable.txt", "THIRD-PARTY-NOTICES.txt",
                                "leanwatts_can/release_channel.json", f"INSTALL-AND-UPDATE-GEN2-{VERSION}.txt")}
    old_prefix = f"Leanwatts-Testbench-Gen2-{BASE_VERSION}-source/"
    with zipfile.ZipFile(BASE_SOURCE) as prior:
        for entry in prior.namelist():
            name = entry.removeprefix(old_prefix)
            if entry.startswith(old_prefix) and (name.startswith("tools/") or name.startswith("leanwatts_can/tests/")
                    or name in (".gitignore", "SOURCE-README.txt")):
                source_files[name] = prior.read(entry)
    for name in SOURCE_TESTS:
        source_files[name] = (ROOT / name).read_bytes()
    for name in SOURCE_TOOLS:
        source_files[name] = (ROOT / name).read_bytes()
    source_files["SOURCE-README.txt"] = (f"Leanwatts Testbench Gen2 {VERSION}\n\n"
        f"Application bytes match the released {VERSION} package. Development support starts\n"
        f"from the immutable {BASE_VERSION} source release with this version's changed tests\n"
        "and packaging tools, including the repaired installer source. No station data,\n"
        "credentials or vendor runtime binaries\n"
        f"are included. Reproduction requires the exact {BASE_VERSION} full/source ZIPs in dist.\n").encode()
    assert_no_station_or_vendor_package(source_files)
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
    return dict(artifact(SOURCE), source_files=len(source_files), station_data_included=False,
                credentials_included=False, unfinished_features_included=False)


def main():
    if APPLICATION_VERSION != VERSION:
        raise SystemExit("Application version does not match the release builder.")
    if "TODO" in NOTES + NOTE:
        raise SystemExit("Finalize release notes after source freeze before building.")
    if any(path.exists() for path in (FULL, SETUP, DELTA, LOCAL, SOURCE, REPORT)):
        raise SystemExit("1.1.12 artifacts already exist; preserving them.")
    if not ICON.is_file() or not LOGO.is_file():
        raise SystemExit("Approved Leanwatts icons are missing.")
    files, baseline, _, base_version = read_release(BASE)
    if base_version != BASE_VERSION:
        raise ValueError("The overlay base must be the frozen 1.1.11 release.")
    WORK.mkdir(parents=True, exist_ok=True)
    launcher_source = WORK / "CanCalibrationLauncher.cs"
    launcher_source.write_text(render_launcher((ROOT / "tools/CanCalibrationLauncher.cs").read_text(encoding="utf-8")), encoding="utf-8")
    launcher = WORK / "Leanwatts-Testbench-Gen2.exe"
    compile_cs(launcher_source, launcher, "/win32icon:" + str(ICON))
    for name in APP_CHANGES:
        files[name] = (ROOT / name).read_bytes()
    for name in APP_REMOVALS:
        files.pop(name, None)
    files["Leanwatts-Testbench-Gen2.exe"] = launcher.read_bytes()
    files.pop(f"INSTALL-AND-UPDATE-GEN2-{BASE_VERSION}.txt", None)
    files[f"INSTALL-AND-UPDATE-GEN2-{VERSION}.txt"] = NOTE.encode("utf-8")
    files["VERSION.json"] = (json.dumps({"application": "leanwatts-testbench-gen2", "version": VERSION}, indent=2) + "\n").encode()
    assert_no_station_or_vendor_package(files)
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
        raise ValueError("The full package differs from its approved sources.")
    full_info = artifact(FULL)
    # Publish no release here; expose the verified small update as soon as built.
    # The full installer/source assembly can then finish while it is reviewed.
    delta_info = build_delta(BASE, FULL, VERSION, DELTA, icon=ICON, logo=LOGO)
    print(json.dumps({"small_update_built": delta_info["artifact"], "bytes": delta_info["bytes"],
                      "sha256": delta_info["sha256"], "archive_reconstruction_verified": True}), flush=True)
    generated = WORK / "CanCalibrationSetup.cs"
    generated.write_text(render_installer((ROOT / "tools/CanCalibrationSetup.cs").read_text(encoding="utf-8"),
        VERSION, full_info["sha256"], incremental=False), encoding="utf-8")
    compile_cs(generated, SETUP, "/win32icon:" + str(ICON), "/r:System.IO.Compression.dll",
               "/r:System.IO.Compression.FileSystem.dll", "/r:System.Web.Extensions.dll",
               "/resource:" + str(FULL) + ",payload.zip", "/resource:" + str(LOGO) + ",branding.png")
    setup_info = artifact(SETUP)
    full_manifest = {"schema": 1, "application": "leanwatts-testbench-gen2", "version": VERSION,
        "package_type": "full", "notes": NOTES,
        "installer": {"filename": SETUP.name, "asset_name": SETUP.name,
                      "sha256": setup_info["sha256"], "size": setup_info["bytes"]}}
    SETUP.with_suffix(".update.json").write_text(json.dumps(full_manifest, indent=2) + "\n", encoding="utf-8")
    source_info = source_snapshot(files)
    del verified, files
    delta_manifest = json.loads(DELTA.with_suffix(".update.json").read_text(encoding="utf-8"))
    combined = {"schema": 1, "application": "leanwatts-testbench-gen2", "version": VERSION,
        "notes": NOTES, "packages": [
            {key: delta_manifest[key] for key in ("package_type", "base_version", "installer")},
            {key: full_manifest[key] for key in ("package_type", "installer")}]}
    (ROOT / f"dist/update-manifest-gen2-{VERSION}.json").write_text(json.dumps(combined, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(LOCAL, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(DELTA, DELTA.name)
        archive.write(DELTA.with_suffix(".update.json"), DELTA.with_suffix(".update.json").name)
        archive.writestr("READ-ME.txt", LOCAL_NOTE)
    local_info = artifact(LOCAL)
    report = {"version": VERSION, "base_version": BASE_VERSION,
        "artifacts": [setup_info, full_info, delta_info, local_info, source_info],
        "manifest_files": len(manifest), "approved_changed_paths": sorted(changed_paths),
        "windows_icons_embedded": True, "station_data_included": False,
        "archive_integrity_verified": True, "installer_compiled": True,
        "installer_executed": False, "code_signed": False, "online_feed_published": False,
        "hardware_access": False, "previous_release_artifacts_preserved": True,
        "gdlink_vendor_package_included": False, "waveshare_sdk_included": False,
        "firmware_images_or_station_profiles_included": False,
        "test_program_format": "Validated data-only JSON; no arbitrary script execution",
        "installer_source_sha256": digest((ROOT / "tools/CanCalibrationSetup.cs").read_bytes())["sha256"],
        "repaired_installer_source_included": True, "delta_commit": "changed files in place with recovery backup"}
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
