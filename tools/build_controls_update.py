"""Package the cumulative live-controls fix against the first real manual release."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "dist/CAN-Calibration-Live-Manual-win64.zip"
PATCH = ROOT / "dist/CAN-Controls-Update-2.zip"
FULL = ROOT / "dist/CAN-Calibration-Live-Manual-ControlsUpdate2-win64.zip"
FILES = (
    "leanwatts_can/gui.py", "leanwatts_can/legacy_page.py",
    "leanwatts_can/live_page_support.py",
    "leanwatts_can/tests/test_live_integration.py",
    "leanwatts_can/tests/test_live_monitor_ui.py",
    "leanwatts_can/tests/test_shutdown_priority.py",
    "leanwatts_can/tests/test_live_control_queue.py",
)
INSTRUCTIONS = """CAN CALIBRATION - CONTROLS UPDATE 2 (CUMULATIVE)

INSTALL ON THE WORKING LAPTOP
1. Close our calibration GUI and the Chinese charger application.
2. Extract this small update directly INTO your existing CAN-Calibration folder
   containing Start-Original-UI.cmd and runtime. Replace the included files.
3. Reopen Start-Original-UI.cmd. Confirm the banner says "Controls update 2".
   If that text is absent, the old copy is still running or the patch was
   extracted into a different folder. No separate Python install is needed.

This includes the earlier Shutdown priority fix. Install directly on either
the original real-manual build or the Shutdown update. It does not replace
calibration_data, drivers, SDK DLLs, or Windows settings.

WHAT CHANGED
- Background CAN reads no longer disable parameter fields or flash buttons.
- One operator action can wait behind the current background call and runs
  before the next poll. It is checked again before transmission.
- Shutdown has priority over queued actions and cancels unsent batch frames.
- Closing/resetting the connection or a connection failure discards queued
  work. Already in-flight commands cannot be undone. There is no auto retry.
- Read requests keep the original groups 1 through 6, now spaced rather than
  sent in an immediate burst. Charger command bytes are unchanged.
- The status/tooltip explains why calibration or Send data is unavailable.

MANUAL WORKFLOW
1. In Variant recipes, enter/save all charger settings and the bench voltage,
   current and power limits. Click "Use variant in manual CAN".
2. Open device and Start CAN in Live manual control, if not already connected.
3. Click Shutdown once. Wait for "Shutdown frame API accepted" and confirm
   the physical output at the load bank before proceeding.
4. Edit the required settings and click Send data. Wait for actual matching
   readback of all 16 settings. Cal. start then becomes available.
5. Cal. start, enter both validated duty values, Apply duties, then adjust while
   observing the load bank and save using the appropriate calibration button.

Receiving live readings alone does not complete step 4. The supplied older
traces have no settings-write or calibration-start commands, and no response
18FF56E5 for the four adjustable-limit fields. If those remain missing after
this update, the GUI will identify the missing readbacks and keep calibration
unavailable. Export a fresh trace for analysis; no unverified values are treated
as confirmed. Pacing is corrected, but remote bench behavior is not yet tested.

Stop/Reset CAN and Close adapter stop communication, not charger output.
API acceptance confirms the adapter accepted a frame, not physical shutdown,
successful calibration saving, or persistence after a power cycle.
The Maynuo load is still operated manually in this build.
"""


def digest(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def main(note_name="CONTROLS-UPDATE-2.txt"):
    if PATCH.exists() or FULL.exists():
        raise SystemExit("Release artifacts already exist; preserving them.")
    changed = {name: (ROOT / name).read_bytes() for name in FILES}
    changed[note_name] = INSTRUCTIONS.encode("utf-8")
    with zipfile.ZipFile(BASE) as source:
        manifest = json.loads(source.read("CAN-Calibration/MANIFEST.json"))
        for name, data in changed.items():
            manifest[name] = digest(data)
        changed["MANIFEST.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        with zipfile.ZipFile(PATCH, "x", zipfile.ZIP_DEFLATED) as archive:
            for name, data in changed.items():
                archive.writestr(name, data)
        with zipfile.ZipFile(FULL, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for info in source.infolist():
                name = info.filename.removeprefix("CAN-Calibration/")
                if name not in changed:
                    archive.writestr(info, source.read(info.filename))
            for name, data in changed.items():
                archive.writestr("CAN-Calibration/" + name, data)
    with zipfile.ZipFile(FULL) as archive:
        for name, expected in manifest.items():
            if digest(archive.read("CAN-Calibration/" + name)) != expected:
                raise RuntimeError("Archive validation failed: " + name)
    for artifact in (PATCH, FULL):
        with artifact.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        artifact.with_suffix(".zip.sha256").write_text(checksum + "  " + artifact.name + "\n", encoding="ascii")
        print(json.dumps({"artifact": str(artifact), "bytes": artifact.stat().st_size, "sha256": checksum}))


if __name__ == "__main__":
    main()
