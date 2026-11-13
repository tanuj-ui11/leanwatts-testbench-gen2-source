"""Build a self-contained internal Windows GUI preview; never access hardware."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE_RUNTIME = ROOT / "LeanWatts-EoL-0.1.0-win64 (1)/LeanWatts EoL/_internal/python"
PACKAGES = ("PySide6", "shiboken6", "pyqtgraph", "numpy", "numpy.libs", "colorama", "serial")
METADATA = ("pyside6_essentials", "shiboken6", "pyqtgraph", "numpy", "colorama", "pyserial")


def main() -> None:
    destination = ROOT / "dist/CAN-Calibration-Live-Manual-win64.zip"
    if destination.exists():
        raise SystemExit(f"Refusing to replace existing artifact: {destination}")
    if not (SOURCE_RUNTIME / "python.exe").is_file():
        raise SystemExit("Supplied embedded Python runtime is missing.")
    destination.parent.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="portable-build-", dir=destination.parent))
    package = staging / "CAN-Calibration"
    package.mkdir()
    runtime = package / "runtime"
    runtime.mkdir()
    for item in SOURCE_RUNTIME.iterdir():
        if item.is_file():
            shutil.copy2(item, runtime / item.name)
    # This isolated runtime must not import the original EoL application.
    (runtime / "python312._pth").write_text(
        "python312.zip\n.\nLib\\site-packages\nimport site\n", encoding="ascii"
    )
    site = runtime / "Lib/site-packages"
    site.mkdir(parents=True)
    source_site = SOURCE_RUNTIME / "Lib/site-packages"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
    for name in PACKAGES:
        # This QWidget application never loads QML. Its unrelated QML build
        # resources also contain paths too long for normal Windows extraction.
        def dependency_ignore(folder, names):
            excluded = set(ignore(folder, names))
            if Path(folder).name == "PySide6":
                excluded.update({"qml", "include", "typesystems", "glue"} & set(names))
            return excluded
        shutil.copytree(source_site / name, site / name, ignore=dependency_ignore)
    for name in METADATA:
        matches = list(source_site.glob(name + "-*.dist-info"))
        if len(matches) != 1:
            raise RuntimeError(f"Expected one metadata directory for {name}: {matches}")
        shutil.copytree(matches[0], site / matches[0].name, ignore=ignore)
    shutil.copytree(ROOT / "leanwatts_can", package / "leanwatts_can", ignore=ignore)
    for name in (
        "launch_calibration.py", "Start-CAN-Calibration.cmd", "Start-Original-UI.cmd",
        "README-Portable.txt", "README-CAN-Calibration.md", "THIRD-PARTY-NOTICES.txt",
    ):
        shutil.copy2(ROOT / name, package / name)
    # Portable launchers have no dependency on the original application folder.
    for name in ("Start-CAN-Calibration.cmd", "Start-Original-UI.cmd"):
        path = package / name
        lines = path.read_text(encoding="utf-8").splitlines()
        lines = [line for line in lines if not line.startswith('if not exist "%PYTHON%" set')]
        path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    docs = package / "reverse_engineering"
    docs.mkdir()
    for name in ("protocol-report.md", "reference-adaptation.md"):
        shutil.copy2(ROOT / "reverse_engineering" / name, docs / name)
    research = package / "research"
    research.mkdir()
    for name in ("load_bank.md", "leanwatts_hardware_review.md"):
        shutil.copy2(ROOT / "research" / name, research / name)
    (package / "tools").mkdir()
    shutil.copy2(ROOT / "tools/smoke_portable.py", package / "tools/smoke_portable.py")
    for name in ("CanReceiveBridge.cs", "build_can_bridge.cmd"):
        shutil.copy2(ROOT / "tools" / name, package / "tools" / name)
    vendor = package / "vendor_can"
    vendor.mkdir()
    shutil.copy2(ROOT / "vendor_can/CanReceiveBridge.exe", vendor / "CanReceiveBridge.exe")
    shutil.copy2(ROOT / "JKCAN_CHARGE/zlgcan.dll", vendor / "zlgcan.dll")
    shutil.copytree(ROOT / "JKCAN_CHARGE/kerneldlls", vendor / "kerneldlls", ignore=shutil.ignore_patterns("*.INF", "*.inf"))

    manifest = {}
    for path in sorted(package.rglob("*")):
        if path.is_file():
            relative = path.relative_to(package).as_posix()
            if "lw_eol" in relative.lower():
                raise RuntimeError(f"Original application file unexpectedly included: {relative}")
            with path.open("rb") as handle:
                file_digest = hashlib.file_digest(handle, "sha256").hexdigest()
            manifest[relative] = {"bytes": path.stat().st_size, "sha256": file_digest}
    (package / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(staging))
    with destination.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    destination.with_suffix(".zip.sha256").write_text(f"{digest}  {destination.name}\n", encoding="ascii")
    print(json.dumps({"zip": str(destination), "bytes": destination.stat().st_size,
                      "sha256": digest, "files": len(manifest), "staging": str(package)}, indent=2))


if __name__ == "__main__":
    main()
