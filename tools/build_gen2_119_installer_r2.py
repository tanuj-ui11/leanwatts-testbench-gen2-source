"""Rebuild only the 1.1.8 -> 1.1.9 installer; application archives stay immutable."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from build_gen2_delta import build, read_release


def main():
    output = ROOT / "dist/Leanwatts-Testbench-Gen2-Update-1.1.8-to-1.1.9-r2.exe"
    local = output.with_name(output.stem + "-local.zip")
    source = output.with_name(output.stem + "-installer-source.zip")
    if any(path.exists() for path in (output, local, source)):
        raise SystemExit("Installer revision already exists; preserving released artifacts.")
    report = build(
        ROOT / "dist/Leanwatts-Testbench-Gen2-1.1.8-win64.zip",
        ROOT / "dist/Leanwatts-Testbench-Gen2-1.1.9-win64.zip",
        "1.1.9", output,
        icon=ROOT / "leanwatts_can/assets/leanwatts.ico",
        logo=ROOT / "leanwatts_can/assets/leanwatts-logo-light.png",
    )
    note = (
        "Leanwatts Testbench Gen2 1.1.9 - corrected installer (r2)\n\n"
        "Close Testbench and both charger applications. Run the r2 EXE from Downloads.\n"
        "Use the same Windows account as the existing 1.1.8 installation.\n"
        "This EXE works alone; no separate JSON is needed for direct installation.\n"
        "It updates only changed application files without replacing the installation\n"
        "folder. Existing recipes, accounts, settings and reports stay in place.\n"
        "A recovery backup is retained beside the installation. The application\n"
        "contents are the exact original 1.1.9 release; only the installer changed.\n"
        "Windows security settings and folder permissions are not altered.\n\n"
        "For Settings > local update, keep the included .update.json beside the EXE.\n"
    )
    with zipfile.ZipFile(local, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in (output, output.with_suffix(".update.json"), output.with_suffix(".exe.sha256")):
            archive.write(path, path.name)
        archive.writestr("READ-ME.txt", note)
    with zipfile.ZipFile(source, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in ("tools/CanCalibrationSetup.cs", "tools/build_gen2_delta.py",
                     "tools/build_gen2_119_installer_r2.py", "tools/test_gen2_installer_permissions.py"):
            archive.write(ROOT / name, name)
        archive.writestr("READ-ME.txt", note)
    for path in (local, source):
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        path.with_suffix(path.suffix + ".sha256").write_text(checksum + "  " + path.name + "\n", encoding="ascii")
    # Independently compare every resulting byte against the frozen full release.
    base, _, _, _ = read_release(ROOT / "dist/Leanwatts-Testbench-Gen2-1.1.8-win64.zip")
    target, target_manifest, _, _ = read_release(ROOT / "dist/Leanwatts-Testbench-Gen2-1.1.9-win64.zip")
    with zipfile.ZipFile(output.with_suffix(".payload.zip")) as archive:
        package = json.loads(archive.read("PACKAGE.json"))
        rebuilt = {name: value for name, value in base.items() if name in target_manifest}
        rebuilt.update({name: archive.read("CAN-Calibration/" + name) for name in package["changed_files"]})
        assert rebuilt == target
    report.update(installer_revision="r2", application_bytes_unchanged=True,
                  exact_original_119_reconstructed=True, local_zip=str(local),
                  installer_sources=str(source))
    (ROOT / "artifacts/gen2-1.1.9-installer-r2.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
