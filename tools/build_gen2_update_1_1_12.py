"""Build the small update Gen2 1.1.11 -> 1.1.12 from the verified 1.1.11 full package.

The immutable 1.1.11 full archive is the base. Only application files under leanwatts_can/
(no tests, no station data), VERSION.json and the install note change; the 1.1.11 launcher EXE
and the bundled runtime are reused byte for byte. The delta installer is then built by the
repository's own tools/build_gen2_delta.py (verified reconstruction of the complete target).

  python tools/build_gen2_update_1_1_12.py --base dist/Leanwatts-Testbench-Gen2-1.1.11-win64.zip \
      --output-dir build/gen2-1.1.12 [--compiler PATH]

--compiler defaults to Windows' csc.exe (as for earlier releases). On macOS/Linux pass a wrapper
around Mono's `mcs` that accepts csc-style options (see docs/claude or the release notes).
Nothing is published, installed or executed by this tool; existing outputs are never overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from build_gen2_delta import CSC, build as build_delta, digest, read_release, safe_name, version_parts  # noqa: E402

VERSION = "1.1.12"
BASE_VERSION = "1.1.11"
PREFIX = "CAN-Calibration/"
APPLICATION = "leanwatts-testbench-gen2"
EXE_NAME = f"Leanwatts-Testbench-Gen2-Update-{BASE_VERSION}-to-{VERSION}.exe"

NOTE = """Leanwatts Testbench Gen2 1.1.12

INSTALL OR UPDATE
Close Testbench and leave it closed during installation. For an exact installed
1.1.11 release, run Leanwatts-Testbench-Gen2-Update-1.1.11-to-1.1.12.exe.
The EXE works by itself. Other versions, portable copies or modified installs
require the full Setup. Station accounts, settings, recipes, edited test programs
and reports are preserved. The small update replaces changed application files in
place and keeps a recovery backup.

WHAT IS NEW
- Per-serial EoL report: press Generate report for a charger in History & Reports.
  One report per work-order serial; nothing is produced or sent automatically.
  Reports are queued locally and sent to the ops dashboard when it is configured
  (Settings > Dashboard: URL, API key, station name). With no network they wait
  and are sent later.
- Work-order serial list: enter a work order, press Download serials, then scan a
  serial barcode. Unknown, other-work-order, rejected, replaced or already-tested
  serials show a warning; nothing is blocked.
- "Other number" stays selected for the same serial after Parameters + Calibration,
  so Test can be started on the same charger without re-entering it.
- End-current cutoff test: each CC step-down now applies to the running load.
- Much faster with large sessions (100+ chargers).
- Operator and station are recorded when a charger starts.

NOT YET VERIFIED ON THE BENCH
The cutoff step-down fix, the Generate report and dashboard sending, and warn-only
handling of rejected/replaced serials need a supervised bench run before production
use. Calibration PASS still means the save request was accepted; persistence after an
AC power cycle is not verified.
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
recovery backup. Station accounts, recipes, programs, settings and reports are preserved.
The EXE is not code-signed; Windows may show a SmartScreen prompt.
"""


def assert_no_station_or_vendor(files):
    forbidden_names = {"gdlink_cli.exe", "gdlink.dll", "gddap.dll", "gd_mcu_dll.dll", "firmware_profiles.json",
                       "controlcan.dll"}
    for name in files:
        lowered = name.replace("\\", "/").lower()
        parts = lowered.split("/")
        if (lowered.startswith(("research/", "calibration_data/", "outbox/", "worklists/"))
                or any(part in {"gdutility-official", "gd-linkutilityprogrammer", "firmware_images", "waveshare"} for part in parts)
                or parts[-1] in forbidden_names):
            raise ValueError("Station or vendor content is excluded: " + name)


def application_files(source_root):
    """Application files under leanwatts_can/ (never tests or caches), keyed by package-relative name."""
    base = Path(source_root) / "leanwatts_can"
    found = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(Path(source_root)).as_posix()
        parts = relative.split("/")
        if "tests" in parts or "__pycache__" in parts or path.suffix in (".pyc", ".pyo"):
            continue
        found[relative] = path.read_bytes()
    return found


def artifact(path):
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(checksum + "  " + path.name + "\n", encoding="ascii")
    return {"artifact": str(path), "bytes": path.stat().st_size, "sha256": checksum}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", type=Path, required=True, help="Exact complete 1.1.11 win64 ZIP")
    parser.add_argument("--output-dir", type=Path, required=True, help="New (empty or absent) output directory")
    parser.add_argument("--source-root", type=Path, default=ROOT, help="Source tree holding leanwatts_can (default: this repo)")
    parser.add_argument("--compiler", type=Path, default=CSC, help="csc.exe, or a csc-compatible wrapper")
    args = parser.parse_args(argv)

    root = args.source_root.resolve()
    version_text = (root / "leanwatts_can/version.py").read_text(encoding="utf-8")
    if f'VERSION = "{VERSION}"' not in version_text:
        raise SystemExit(f"leanwatts_can/version.py must say VERSION = \"{VERSION}\" before building.")
    out = args.output_dir.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit("The output directory is not empty; published outputs are never overwritten.")
    out.mkdir(parents=True, exist_ok=True)
    icon = root / "leanwatts_can/assets/leanwatts.ico"
    logo = root / "leanwatts_can/assets/leanwatts-logo-light.png"
    if not icon.is_file() or not logo.is_file():
        raise SystemExit("Approved Leanwatts icon/logo assets are missing.")

    files, baseline, _, base_version = read_release(args.base)
    if base_version != BASE_VERSION:
        raise SystemExit(f"The base must be the frozen {BASE_VERSION} release (found {base_version}).")
    overlay = application_files(root)
    for name, data in overlay.items():
        files[name] = data
    # Files that were application files in the base but no longer exist in the source tree are removed.
    for name in [n for n in files if n.startswith("leanwatts_can/") and "/tests/" not in n and n not in overlay]:
        files.pop(name)
    files.pop(f"INSTALL-AND-UPDATE-GEN2-{BASE_VERSION}.txt", None)
    files[f"INSTALL-AND-UPDATE-GEN2-{VERSION}.txt"] = NOTE.encode("utf-8")
    files["VERSION.json"] = (json.dumps({"application": APPLICATION, "version": VERSION}, indent=2) + "\n").encode()
    assert_no_station_or_vendor(files)
    for name in files:
        safe_name(name)
    manifest = {name: digest(data) for name, data in sorted(files.items())}
    changed = {name for name in set(baseline) | set(manifest) if baseline.get(name) != manifest.get(name)}
    allowed = ({"VERSION.json", f"INSTALL-AND-UPDATE-GEN2-{BASE_VERSION}.txt", f"INSTALL-AND-UPDATE-GEN2-{VERSION}.txt"}
               | {n for n in changed if n.startswith("leanwatts_can/") and "/tests/" not in n})
    if not changed <= allowed:
        raise SystemExit("Unexpected release changes: " + repr(sorted(changed - allowed)))

    full = out / f"Leanwatts-Testbench-Gen2-{VERSION}-win64.zip"
    with zipfile.ZipFile(full, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(PREFIX + name, data)
        archive.writestr(PREFIX + "MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    verified, _, _, version = read_release(full)
    if version != VERSION or verified != files:
        raise SystemExit("The target package differs from its approved sources.")

    delta = out / EXE_NAME
    report = build_delta(args.base, full, VERSION, delta, compiler=args.compiler, icon=icon, logo=logo)
    update_json = delta.with_suffix(".update.json")
    exe_info = artifact(delta)
    local = out / f"Leanwatts-Testbench-Gen2-Update-{BASE_VERSION}-to-{VERSION}-local.zip"
    with zipfile.ZipFile(local, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(delta, delta.name)
        archive.write(update_json, update_json.name)
        archive.writestr("READ-ME.txt", LOCAL_NOTE)
    local_info = artifact(local)
    summary = dict(exe=exe_info, local_zip=local_info, changed_files=report["changed_files"], removed_files=report["removed_files"],
                   full_target_file_count=report["full_target_file_count"], installer_executed=False, code_signed=False)
    (out / "build-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"exe": exe_info, "local_zip": local_info, "changed": len(report["changed_files"]),
                      "removed": report["removed_files"]}, indent=2))


if __name__ == "__main__":
    main()
