"""Create an allowlisted source snapshot without station data or vendor apps."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parent.parent
TOOLS = (
    "CanCalibrationLauncher.cs", "CanCalibrationSetup.cs", "CanReceiveBridge.cs",
    "build_can_bridge.cmd", "build_gen2_release.py", "build_gen2_11_release.py",
    "build_gen2_delta.py", "build_gen2_source.py", "test_gen2_delta.py",
    "README-UPDATE-PUBLISHING.txt",
)
TOP = ("launch_calibration.py", "Start-CAN-Calibration.cmd", "Start-Original-UI.cmd",
       "README-CAN-Calibration.md", "README-Portable.txt", "THIRD-PARTY-NOTICES.txt")

README = """Leanwatts Testbench Gen2 - source snapshot

This source bundle contains the application, brand assets, font licenses and
our C# CAN bridge/launcher/installer sources. It intentionally excludes saved
station data, operator records, credentials, research captures, original vendor
applications, vendor DLLs and the portable Python runtime.

Windows development requires Python 3.12, PySide6 Essentials, pySerial and the
runtime dependencies listed in THIRD-PARTY-NOTICES.txt. The existing portable
runtime may be used to run launch_calibration.py. The CAN bridge can be compiled
with tools/build_can_bridge.cmd; the correct vendor adapter DLLs/drivers remain
separate runtime requirements.

The 1.1 release builder expects the exact published 1.0 full portable ZIP in
dist/Leanwatts-Testbench-Gen2-1.0-win64.zip. Keep each complete release ZIP as the
verified base for subsequent small changed-files updates. See
tools/README-UPDATE-PUBLISHING.txt for publication and integrity details.

The application may operate connected equipment only after explicit controls
are used. Layout previews and fake-instrument tests do not prove production
bench operation. Use the existing calibration_data folder on the intended
station; do not commit or upload that directory.
"""


def build(version, output):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Preserving the existing source snapshot.")
    files = {}
    for source in sorted((ROOT / "leanwatts_can").rglob("*.py")):
        if "__pycache__" not in source.parts:
            files[source.relative_to(ROOT).as_posix()] = source.read_bytes()
    for folder in (ROOT / "leanwatts_can/assets", ROOT / "leanwatts_can/fonts"):
        for source in sorted(folder.rglob("*")):
            if source.is_file() and "__pycache__" not in source.parts:
                files[source.relative_to(ROOT).as_posix()] = source.read_bytes()
    for name in TOP + tuple("tools/" + name for name in TOOLS) + ("leanwatts_can/release_channel.json",):
        files[name] = (ROOT / name).read_bytes()
    files["SOURCE-README.txt"] = README.encode("utf-8")
    files[".gitignore"] = ("calibration_data/\ndist/\nartifacts/\nresearch/\n__pycache__/\n*.pyc\n"
                           "*.dpapi\n*.log\n.app-running.lock\n.station-running.lock\n").encode()
    # Any unexpected path is a packaging error, not an invitation to gather it.
    for name in files:
        if name.startswith(("calibration_data/", "vendor_can/", "research/", "artifacts/", "dist/")):
            raise ValueError("Unexpected non-source path: " + name)
        if name.endswith((".exe", ".dll", ".dpapi", ".pyc")):
            raise ValueError("Unexpected binary or credential path: " + name)
    prefix = f"Leanwatts-Testbench-Gen2-{version}-source/"
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(prefix + name, data)
    with zipfile.ZipFile(output) as archive:
        if set(archive.namelist()) != {prefix + name for name in files}:
            raise ValueError("Source archive contains unexpected paths.")
        for name, data in files.items():
            if archive.read(prefix + name) != data:
                raise ValueError("Source archive content mismatch: " + name)
    with output.open("rb") as stream:
        sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(sha256 + "  " + output.name + "\n", encoding="ascii")
    return dict(artifact=str(output), bytes=output.stat().st_size, sha256=sha256,
                source_files=len(files), station_data_included=False, credentials_included=False,
                vendor_applications_included=False, runtime_binaries_included=False)
