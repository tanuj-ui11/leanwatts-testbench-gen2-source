"""Package auto-calibration 7.4; does not run the application or hardware checks."""
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_controls_update as builder

builder.BASE = builder.ROOT / "dist/CAN-Calibration-Bench-Automation-7.3-win64.zip"
builder.PATCH = builder.ROOT / "dist/CAN-Auto-Calibration-Update-7.4.zip"
builder.FULL = builder.ROOT / "dist/CAN-Calibration-Auto-7.4-win64.zip"
builder.FILES = (
    "leanwatts_can/core.py",
    "leanwatts_can/gui.py",
    "leanwatts_can/live_page_support.py",
    "leanwatts_can/automation.py",
    "leanwatts_can/operator_page.py",
    "leanwatts_can/load_setup.py",
    "README-CAN-Calibration.md",
    "README-Portable.txt",
)
builder.INSTRUCTIONS = """AUTO CALIBRATION 7.4 - UPDATE FOR BENCH AUTOMATION 7.3

INSTALL
Close the calibration GUI and Chinese charger application. Extract the small
update INTO the existing 7.3 CAN-Calibration folder, replacing included files.
The update contains no calibration_data: saved variants and records remain.
Alternatively extract the full CAN-Calibration-Auto-7.4-win64.zip into a new
folder and copy the old calibration_data folder into its CAN-Calibration
folder. Start with Start-CAN-Calibration.cmd.

CALIBRATE
1. In Load setup choose the Maynuo's COM port, keep 9600 baud / address 1 / N,
   and click Save calibration connection. The known bench profile is used.
2. Select/save the real variant's current and voltage targets, tolerances,
   calibration load settings and bench voltage/current/power limits.
3. On Operator choose the variant, enter serial/operator, then Start auto
   calibration. The software opens the load and CAN connections and runs
   current calibration followed by voltage calibration, sending each save
   command after convergence. It does not send charger parameter updates.
   The main header shows AUTO CALIBRATION 7.4. An open manual CAN connection
   in this app is handed over automatically after its pending action finishes.

Trim direction is detected automatically when an adjustment is needed. The
known bench starts at duty 80.0 and uses the user-specified bounds 60.0-85.0.
The known load profile is identity-bound to model code 140 / firmware 42 and
observed modes CC 0x3B01 / CV 0x3B02; these are not universal instrument defaults.
Live current, voltage, target/error and stage progress remain visible. Stop
cancels further adjustments and begins the existing shutdown sequence.

The separate production test/report workflow is deferred. A calibration
record is retained for each run; no acceptance test is automatically added.
This update was implemented and packaged without running software tests,
simulations or hardware checks, as requested. Earlier 7.3 validation does not
validate the new automatic direction detection or a powered calibration run.
Save API acceptance is recorded separately from permanent-storage proof.
"""


def main():
    # Packaging only: preserve station data and reuse the existing runtime.
    with zipfile.ZipFile(builder.BASE) as source:
        manifest = json.loads(source.read("CAN-Calibration/MANIFEST.json"))
        names = [name.removeprefix("CAN-Calibration/") for name in source.namelist()]
        if any(name.casefold().startswith("calibration_data/") for name in names + list(manifest)):
            raise SystemExit("Base archive contains station data; no release created.")
    if any(name.casefold().startswith("calibration_data/") for name in builder.FILES):
        raise SystemExit("Release file list must not contain station data.")
    builder.main(note_name="AUTO-CALIBRATION-7.4.txt")


if __name__ == "__main__":
    main()
