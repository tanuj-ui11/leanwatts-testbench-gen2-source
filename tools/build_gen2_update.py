"""Build a small Gen2 update (BASE -> the version in leanwatts_can/version.py) from a verified full package.

The immutable base full archive (any earlier Gen2 win64 ZIP) is the base. Only application files under leanwatts_can/
(no tests, no station data), VERSION.json and the install note change; the 1.1.11 launcher EXE
and the bundled runtime are reused byte for byte. The delta installer is then built by the
repository's own tools/build_gen2_delta.py (verified reconstruction of the complete target).

  python tools/build_gen2_update.py --base dist/Leanwatts-Testbench-Gen2-1.1.11-win64.zip \
      --output-dir build/gen2-update [--compiler PATH]

--compiler defaults to Windows' csc.exe (as for earlier releases). On macOS/Linux pass a wrapper
around Mono's `mcs` compiled against .NET Framework 4.5 reference assemblies: tools/mcs-csc-shim-net45.sh.
Nothing is published, installed or executed by this tool; existing outputs are never overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from build_gen2_delta import CSC, build as build_delta, digest, read_release, safe_name, version_parts  # noqa: E402

PREFIX = "CAN-Calibration/"
APPLICATION = "leanwatts-testbench-gen2"

def notes(base, version):
    note = f"""Leanwatts Testbench Gen2 {version}

INSTALL OR UPDATE
Close Testbench and leave it closed during installation. For an exact installed
{base} release, run Leanwatts-Testbench-Gen2-Update-{base}-to-{version}.exe.
The EXE works by itself. Other versions, portable copies or modified installs
require the full Setup. Station accounts, settings, recipes, edited test programs
and reports are preserved. The small update replaces changed application files in
place and keeps a recovery backup.

WHAT IS NEW (since 1.1.11)
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
- End-current cutoff test: each CC step-down is now applied to the running load
  (the load's CC command is re-sent after the new current), and a failure message
  shows the last demand and the measured current.
- Much faster with large sessions (100+ chargers).
- Operator and station are recorded when a charger starts.
{"- This build replaces the first 1.1.12 package, which did not include the Other number and cutoff step-down fixes." + chr(10) if base == "1.1.12" else ""}
NOT YET VERIFIED ON THE BENCH
The cutoff step-down fix, the Generate report and dashboard sending, and warn-only
handling of rejected/replaced serials need a supervised bench run before production
use. Calibration PASS still means the save request was accepted; persistence after an
AC power cycle is not verified.
"""
    local = f"""Leanwatts Testbench Gen2 - local update {base} to {version}

1. Copy this ZIP to the production laptop and extract it.
2. Close Leanwatts Testbench Gen2 and leave it closed during the update.
3. Run Leanwatts-Testbench-Gen2-Update-{base}-to-{version}.exe.
4. Reopen the desktop shortcut and confirm About shows {version}.

The EXE works by itself. Its matching .update.json is only needed when using
Settings > Software updates > Install local test update. Other versions or
modified installations require the full Setup {version} EXE.
The small update replaces changed application files in place and retains a
recovery backup. Station accounts, recipes, programs, settings and reports are preserved.
The EXE is not code-signed; Windows may show a SmartScreen prompt.
"""
    return note, local


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
    match = re.search(r'^VERSION = "([^"]+)"', (root / "leanwatts_can/version.py").read_text(encoding="utf-8"), re.M)
    if not match:
        raise SystemExit("leanwatts_can/version.py has no VERSION.")
    version = match.group(1)
    out = args.output_dir.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit("The output directory is not empty; published outputs are never overwritten.")
    out.mkdir(parents=True, exist_ok=True)
    icon = root / "leanwatts_can/assets/leanwatts.ico"
    logo = root / "leanwatts_can/assets/leanwatts-logo-light.png"
    if not icon.is_file() or not logo.is_file():
        raise SystemExit("Approved Leanwatts icon/logo assets are missing.")

    files, baseline, _, base_version = read_release(args.base)
    if version_parts(version) <= version_parts(base_version):
        raise SystemExit(f"The update ({version}) must be newer than its base ({base_version}).")
    note, local_note = notes(base_version, version)
    exe_name = f"Leanwatts-Testbench-Gen2-Update-{base_version}-to-{version}.exe"
    overlay = application_files(root)
    for name, data in overlay.items():
        files[name] = data
    # Files that were application files in the base but no longer exist in the source tree are removed.
    for name in [n for n in files if n.startswith("leanwatts_can/") and "/tests/" not in n and n not in overlay]:
        files.pop(name)
    for name in [n for n in files if re.fullmatch(r"INSTALL-AND-UPDATE-GEN2-[0-9.]+\.txt", n)]:
        files.pop(name)
    files[f"INSTALL-AND-UPDATE-GEN2-{version}.txt"] = note.encode("utf-8")
    files["VERSION.json"] = (json.dumps({"application": APPLICATION, "version": version}, indent=2) + "\n").encode()
    assert_no_station_or_vendor(files)
    for name in files:
        safe_name(name)
    manifest = {name: digest(data) for name, data in sorted(files.items())}
    changed = {name for name in set(baseline) | set(manifest) if baseline.get(name) != manifest.get(name)}
    allowed = ({"VERSION.json", f"INSTALL-AND-UPDATE-GEN2-{base_version}.txt", f"INSTALL-AND-UPDATE-GEN2-{version}.txt"}
               | {n for n in changed if n.startswith("leanwatts_can/") and "/tests/" not in n})
    if not changed <= allowed:
        raise SystemExit("Unexpected release changes: " + repr(sorted(changed - allowed)))

    full = out / f"Leanwatts-Testbench-Gen2-{version}-win64.zip"
    with zipfile.ZipFile(full, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(PREFIX + name, data)
        archive.writestr(PREFIX + "MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    verified, _, _, built_version = read_release(full)
    if built_version != version or verified != files:
        raise SystemExit("The target package differs from its approved sources.")

    delta = out / exe_name
    report = build_delta(args.base, full, version, delta, compiler=args.compiler, icon=icon, logo=logo)
    update_json = delta.with_suffix(".update.json")
    exe_info = artifact(delta)
    local = out / f"Leanwatts-Testbench-Gen2-Update-{base_version}-to-{version}-local.zip"
    with zipfile.ZipFile(local, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(delta, delta.name)
        archive.write(update_json, update_json.name)
        archive.writestr("READ-ME.txt", local_note)
    local_info = artifact(local)
    summary = dict(exe=exe_info, local_zip=local_info, changed_files=report["changed_files"], removed_files=report["removed_files"],
                   full_target_file_count=report["full_target_file_count"], installer_executed=False, code_signed=False)
    (out / "build-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"exe": exe_info, "local_zip": local_info, "changed": len(report["changed_files"]),
                      "removed": report["removed_files"]}, indent=2))


if __name__ == "__main__":
    main()
