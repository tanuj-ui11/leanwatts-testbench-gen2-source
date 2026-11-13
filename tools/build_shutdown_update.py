"""Build a small in-place update and a full portable archive from the tested release."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "dist/CAN-Calibration-Live-Manual-win64.zip"
PATCH = ROOT / "dist/CAN-Shutdown-Update.zip"
FULL = ROOT / "dist/CAN-Calibration-Live-Manual-ShutdownUpdate-win64.zip"
FILES = ("leanwatts_can/legacy_page.py", "leanwatts_can/live_page_support.py",
         "leanwatts_can/tests/test_shutdown_priority.py")
INSTRUCTIONS = """CAN CALIBRATION - SHUTDOWN PRIORITY UPDATE

For the previously supplied CAN-Calibration-Live-Manual-win64 package.

1. Close the calibration GUI and the Chinese charger application.
2. Extract this update directly INTO your existing CAN-Calibration folder:
   the folder containing Start-Original-UI.cmd and runtime.
3. Allow the listed application files and MANIFEST.json to be replaced.
   No calibration_data files, drivers, SDK DLLs or Windows settings are changed.
4. Reopen Start-Original-UI.cmd and connect in Live manual control mode.

Shutdown stays available during background reads and polling. One operator
request takes priority after the current SDK call, cancels unsent adjustments
and parameter writes, and sends the unchanged original shutdown command once.
Closing the adapter cancels a queued request. Connection errors do not trigger
an automatic retry. An already in-flight command cannot be undone.

The distinct messages are "Shutdown queued" and "Shutdown frame API accepted".
Neither confirms physical shutdown. The packet remains extended ID 1806E550,
DLC 4, bytes 00 14 00 80, matching the supplied Chinese application's code.

This corrects command scheduling; it does NOT establish why a charger might
ignore an accepted shutdown frame. If current continues into the load, do not
proceed with parameter writes/calibration assuming output is off. Export the
Original UI trace and retain the load-bank voltage/current readings. Check that
no other application/controller is sending commands, and compare the original
Chinese application's Shutdown on the same unit with this GUI closed.
"""


def digest(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def main():
    if PATCH.exists() or FULL.exists():
        raise SystemExit("Update artifacts already exist; retaining them unchanged.")
    changed = {name: (ROOT / name).read_bytes() for name in FILES}
    changed["SHUTDOWN-UPDATE.txt"] = INSTRUCTIONS.encode("utf-8")
    with zipfile.ZipFile(BASE) as source:
        manifest = json.loads(source.read("CAN-Calibration/MANIFEST.json"))
        for name, data in changed.items():
            manifest[name] = digest(data)
        changed["MANIFEST.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        with zipfile.ZipFile(PATCH, "x", zipfile.ZIP_DEFLATED) as patch:
            for name, data in changed.items():
                patch.writestr(name, data)
        with zipfile.ZipFile(FULL, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as full:
            for info in source.infolist():
                name = info.filename.removeprefix("CAN-Calibration/")
                if name not in changed:
                    full.writestr(info, source.read(info.filename))
            for name, data in changed.items():
                full.writestr("CAN-Calibration/" + name, data)
    with zipfile.ZipFile(FULL) as full:
        for name, expected in manifest.items():
            if digest(full.read("CAN-Calibration/" + name)) != expected:
                raise RuntimeError("Final archive verification failed: " + name)
    for artifact in (PATCH, FULL):
        with artifact.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        artifact.with_suffix(".zip.sha256").write_text(checksum + "  " + artifact.name + "\n", encoding="ascii")
        print(json.dumps({"artifact": str(artifact), "bytes": artifact.stat().st_size, "sha256": checksum}))


if __name__ == "__main__":
    main()
