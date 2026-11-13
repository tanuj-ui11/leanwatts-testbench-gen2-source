"""Package the verified load-control update against the full 7.2 release."""
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_controls_update as builder

builder.BASE = builder.ROOT / "dist/CAN-Calibration-Bench-Automation-7.2-win64.zip"
builder.PATCH = builder.ROOT / "dist/CAN-Load-Control-Update-7.3.zip"
builder.FULL = builder.ROOT / "dist/CAN-Calibration-Bench-Automation-7.3-win64.zip"
builder.FILES = (
    "leanwatts_can/maynuo.py", "leanwatts_can/bench_session.py",
    "leanwatts_can/automation.py", "leanwatts_can/load_setup.py",
    "leanwatts_can/operator_page.py",
    "leanwatts_can/tests/test_maynuo.py",
    "leanwatts_can/tests/test_bench_session.py",
    "leanwatts_can/tests/test_automation.py",
    "leanwatts_can/tests/test_load_setup.py",
    "leanwatts_can/tests/test_operator_page.py",
    "research/maynuo/PROTOCOL-SOURCES.md",
    "README-CAN-Calibration.md", "README-Portable.txt",
)
builder.INSTRUCTIONS = """LOAD CONTROL UPDATE 7.3 - FOR BENCH AUTOMATION 7.2

INSTALL ON AN EXISTING 7.2 STATION
Save the station setup and variant edits before closing the GUI. Unsaved CC/CV
mode observations exist only in memory and will need to be captured again.
Close the GUI, then extract this small update INTO its CAN-Calibration folder,
replacing the included files. Reopen Start-CAN-Calibration.cmd. Load setup must
show 'load diagnostics 7.3'. This update contains no calibration_data files:
saved recipes, station setup and reports are preserved.

For a new laptop, extract the full 7.3 ZIP into a new folder. It includes the
runtime and all application files. Copy the existing station's calibration_data
folder while both applications are closed if its recipes/reports are needed.
Select the actual load COM port on the destination laptop and repeat its setup
checks. Do not assume COM3/COM11 carries over between laptops.

CONFIRMED ON THIS LOAD
The CH343 converter and working TTL wiring communicate at 9600 baud, address 1,
parity N. Panel-confirmed codes are CC 0x3B01 and CV 0x3B02 for model code 0x008C,
firmware 0x002A. Keep this wiring; these observations are not universal defaults.
Readback checks verified remote/local control, CC/CV mode changes and setpoint
writes with load input OFF, followed by restoration of the prior settings.
No energized load-control check or full automatic charger run is claimed yet.

ONE-TIME LOAD SETUP
Keep load input OFF. Click Read load and compare V/I with its front panel.
On the load, press I-SET, keep/re-enter the displayed value and press Enter;
then click Read CC mode. Repeat using V-SET, value, Enter, then Read CV mode.
The GUI read buttons capture the selected mode; they do not change the mode.
Save the confirmed station setup. Automatic calibration also needs the current
and voltage duty bounds/directions established during manual calibration.
Unapproved duty bounds are saved as a draft. Directions default to 'Not yet
checked'; a saved draft does not enable automatic calibration.

UNREG is shown as 'Load unregulated (not at setpoint)', separately from actual
protection faults. An idle OFF load may report this status. It is not accepted
as a valid calibrated reading or, by itself, proof of end-current cutoff.
All protection, communication, identity and limit checks remain active.

FIRST PRODUCTION RUN
Keep the Chinese GUI closed. Confirm the recipe targets and fixture settings,
select the correct COM port, and close our manual CAN connection before the
Operator automatic run. Perform the first auto-calibration under supervision
and verify the output and saved calibration after restarting the charger.
End-current ramp qualification remains separate and must not be assumed from
the OFF-only checks. Driver acceptance of a save request is not proof of its
permanent storage. Windows protections and hardware drivers are unchanged.
"""


def main():
    # Both outputs must be deployable without replacing station-owned data.
    # The full 7.2 archive contains no calibration_data; fail closed if changed.
    with zipfile.ZipFile(builder.BASE) as source:
        manifest = json.loads(source.read("CAN-Calibration/MANIFEST.json"))
        names = [name.removeprefix("CAN-Calibration/") for name in source.namelist()]
        if any(name.casefold().startswith("calibration_data/") for name in names + list(manifest)):
            raise SystemExit("Base archive contains station data; no release created.")
    if any(name.casefold().startswith("calibration_data/") for name in builder.FILES):
        raise SystemExit("Release file list must not contain station data.")
    builder.main(note_name="LOAD-CONTROL-7.3.txt")


if __name__ == "__main__":
    main()
