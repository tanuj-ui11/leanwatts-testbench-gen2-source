"""Build the cumulative update for direct Original UI manual setup."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_controls_update as builder

builder.PATCH = builder.ROOT / "dist/CAN-Manual-Setup-Update-3.zip"
builder.FULL = builder.ROOT / "dist/CAN-Calibration-Live-Manual-SetupUpdate3-win64.zip"
builder.FILES += ("leanwatts_can/tests/test_manual_bench_setup.py", "README-Portable.txt", "README-CAN-Calibration.md")
builder.INSTRUCTIONS = """CAN CALIBRATION - MANUAL SETUP UPDATE 3 (CUMULATIVE)

INSTALL ON THE WORKING LAPTOP
1. Close our calibration GUI and the Chinese charger application.
2. Extract this small update directly INTO your existing CAN-Calibration folder
   containing Start-Original-UI.cmd and runtime. Replace the included files.
3. Reopen Start-Original-UI.cmd. Confirm the banner says "Manual setup update 3".
   If absent, the old copy is still running or you patched a different folder.

This includes both previous updates. It preserves calibration_data, saved
variants, drivers, SDK DLLs and Windows settings. No separate install is needed.

DIRECT ENTRY IN ORIGINAL UI - ENGLISH
You can now use this page without first creating/applying a variant recipe.
1. Select Live manual control. Open device and Start CAN.
2. Click Bench limits in the top toolbar. Enter the maximum permitted voltage,
   current and power for this bench and charger variant, then apply them.
   These are explicit operating ceilings, not CAN parameter values. Applying
   them sends nothing and preserves all directly entered charger parameters.
3. Enter all 16 required charger parameters in the original positions.
4. Click Shutdown once and confirm physical output is off at the bench.
5. Click Send data. Wait for matching actual readbacks, then use Cal. start.
6. Enter both validated initial duty values, Apply duties, adjust while
   observing the independent load-bank readings, and use the appropriate Save.

The current manual-session limits are editable through Bench limits. For named,
reusable variants, the existing Variant recipes page remains available.

WHY THE PREVIOUS BUTTONS WERE GREY
Directly entering charger parameters did not apply bench limits. Previously the
only route was Variant recipes > Use variant in manual CAN, which also required
recipe fields unrelated to this initial manual check. The new local setup
removes that dependency while retaining numerical checks and readback checks.
The status now displays the specific reason if Send data is unavailable.

CALIBRATION READBACK
Cal. start requires a successful Send data and all 16 matching fresh readbacks.
Earlier supplied traces lacked frame 18FF56E5, containing the four adjustable
limits. If those remain missing, the banner identifies the failure and the trace
lists the affected fields. Export a fresh trace for further analysis. No absent
readback is assumed correct and no command protocol has been changed here.

This build includes stable controls during live polling, queued operator actions,
Shutdown priority, paced read requests, and failed-frame logging from Update 2.
Original canvas controls retain their positions; Bench limits is in the toolbar.

Stop/Reset CAN and Close adapter stop communication, not charger output.
API acceptance does not confirm physical output state or saved calibration.
The Maynuo load is still operated manually. Software tests use fake transport;
the actual remotely connected bench still requires operator verification.
"""

if __name__ == "__main__":
    builder.main(note_name="MANUAL-SETUP-UPDATE-3.txt")
