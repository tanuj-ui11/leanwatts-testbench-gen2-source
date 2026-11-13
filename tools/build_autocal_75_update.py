"""Package auto calibration 7.5 without importing or executing application code."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile


ROOT = Path(__file__).resolve().parent.parent
PREFIX = "CAN-Calibration/"
BASE = ROOT / "dist/CAN-Calibration-Auto-7.4-win64.zip"
BASES = [ROOT / f"dist/CAN-Calibration-Bench-Automation-{version}-win64.zip"
         for version in ("7", "7.1", "7.2", "7.3")] + [BASE]
FULL = ROOT / "dist/CAN-Calibration-Auto-7.5-win64.zip"
PATCH = ROOT / "dist/CAN-Upgrade-7-to-7.5.zip"
REPORT = ROOT / "artifacts/autocal-7.5-packaging.json"
FILES = (
    "leanwatts_can/automation.py",
    "leanwatts_can/load_setup.py",
    "leanwatts_can/operator_page.py",
    "leanwatts_can/gui.py",
    "README-Portable.txt",
    "README-CAN-Calibration.md",
)
NOTE_NAME = "UPGRADE-7-TO-7.5.txt"
NOTE = """AUTO CALIBRATION 7.5 - DIRECT CUMULATIVE UPGRADE

Supports installed versions 7, 7.1, 7.2, 7.3 and 7.4.
No intermediate installation or new settings are required.

INSTALL ON THE PRODUCTION LAPTOP
1. Let the current run finish, or use Stop and wait for the process to end.
   Then close our calibration GUI and the Chinese charger application.
2. Open the existing CAN-Calibration folder containing
   Start-CAN-Calibration.cmd, runtime and leanwatts_can.
3. Extract ALL contents of CAN-Upgrade-7-to-7.5.zip directly INTO that folder.
   Replace the included files. Do not create another CAN-Calibration folder
   inside it, and do not run the application from inside the ZIP.
4. Run Start-CAN-Calibration.cmd. Confirm AUTO CALIBRATION 7.5 in the header.
   The screen uses a middle dot before the version.

This update contains no calibration_data. Saved variants, station settings,
reports and logs remain in place. It does not alter drivers or Windows settings.
For a separate full installation, extract CAN-Calibration-Auto-7.5-win64.zip
into a new folder, then copy calibration_data from the previous installation.

FASTER AUTOMATIC CALIBRATION
The controller first learns how a small duty adjustment changes measured
output. Far from the target it then uses calculated coarse jumps, limited to
1.0 duty per adjustment and the configured duty bounds. Near the target it
aims for 0.1 A current changes or 0.05 V voltage changes, reducing the requested
change as it approaches the target. Actual output changes depend on the
charger response and the available 0.1-duty command resolution.

Tuning waits for 3 stable readings between adjustments. Saving still requires
8 stable readings at the target. Direction detection, measurement freshness,
settling, regulation checks, bench limits and shutdown handling remain active.
The Operator page shows tuning versus final confirmation and live measurements.
Current calibration and saving are followed by voltage calibration and saving.

The release was packaged without running software tests, simulations or
hardware checks, as requested. Packaging hashes verify archive contents only.
Save-command acceptance does not prove persistence after a charger restart.
"""


def digest(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts
            or "\\" in name or ":" in name):
        raise ValueError(f"Unsafe archive path: {name}")
    if path.parts and path.parts[0].casefold() == "calibration_data":
        raise ValueError("An application update must not contain station data.")


def read_manifest(archive):
    manifest = json.loads(archive.read(PREFIX + "MANIFEST.json"))
    for name in manifest:
        safe_name(name)
    for info in archive.infolist():
        if not info.filename.startswith(PREFIX):
            raise ValueError("Unexpected archive root: " + info.filename)
        relative = info.filename[len(PREFIX):]
        if relative:
            safe_name(relative)
    return manifest


def main():
    outputs = (FULL, PATCH, REPORT,
               FULL.with_suffix(".zip.sha256"), PATCH.with_suffix(".zip.sha256"))
    if any(path.exists() for path in outputs):
        raise SystemExit("A 7.5 release output already exists; preserving it.")

    replacements = {name: (ROOT / name).read_bytes() for name in FILES}
    replacements[NOTE_NAME] = NOTE.encode("utf-8")
    for name in replacements:
        safe_name(name)

    with zipfile.ZipFile(BASE) as source:
        manifest = read_manifest(source)
        for name, data in replacements.items():
            manifest[name] = digest(data)
        cumulative = set()
        for base_path in BASES:
            with zipfile.ZipFile(base_path) as older:
                old_manifest = read_manifest(older)
                removed = set(old_manifest) - set(manifest)
                if removed:
                    raise ValueError("File removals require migration: " + repr(sorted(removed)))
                cumulative.update(name for name, expected in manifest.items()
                                  if old_manifest.get(name) != expected)

        patch_files = {}
        for name in sorted(cumulative):
            data = replacements[name] if name in replacements else source.read(PREFIX + name)
            if digest(data) != manifest[name]:
                raise ValueError("Source differs from its manifest: " + name)
            patch_files[name] = data
        manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        patch_files["MANIFEST.json"] = manifest_bytes
        replacements["MANIFEST.json"] = manifest_bytes

        with zipfile.ZipFile(FULL, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for info in source.infolist():
                name = info.filename[len(PREFIX):]
                if name not in replacements:
                    archive.writestr(info, source.read(info.filename))
            for name, data in replacements.items():
                archive.writestr(PREFIX + name, data)
        with zipfile.ZipFile(PATCH, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, data in patch_files.items():
                archive.writestr(name, data)

    # Integrity checks inspect bytes only; no application module is imported.
    with zipfile.ZipFile(FULL) as archive:
        for name, expected in manifest.items():
            if digest(archive.read(PREFIX + name)) != expected:
                raise RuntimeError("Full archive integrity mismatch: " + name)
    with zipfile.ZipFile(PATCH) as archive:
        for name in cumulative:
            if digest(archive.read(name)) != manifest[name]:
                raise RuntimeError("Cumulative archive integrity mismatch: " + name)
        if archive.read("MANIFEST.json") != manifest_bytes:
            raise RuntimeError("Cumulative archive manifest mismatch.")

    artifacts = []
    for output in (PATCH, FULL):
        with output.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        output.with_suffix(".zip.sha256").write_text(
            checksum + "  " + output.name + "\n", encoding="ascii")
        artifacts.append({"artifact": str(output), "bytes": output.stat().st_size,
                          "sha256": checksum})
    report = {
        "target_version": "7.5",
        "supported_base_versions": ["7", "7.1", "7.2", "7.3", "7.4"],
        "artifacts": artifacts,
        "workspace_files": list(FILES),
        "cumulative_changed_files": sorted(cumulative),
        "manifest_files": len(manifest),
        "archive_integrity_verified": True,
        "station_data_included": False,
        "application_executed": False,
        "tests_run": False,
        "simulation_run": False,
        "hardware_access": False,
        "validation_status": "Untested application; archive byte integrity only.",
    }
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
