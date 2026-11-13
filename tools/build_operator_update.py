"""Package Operator workspace 6; real manual control remains Update 5."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_manual_flow_update import builder

builder.PATCH = builder.ROOT / "dist/CAN-Operator-Workspace-Update-6.zip"
builder.FULL = builder.ROOT / "dist/CAN-Calibration-Operator-Workspace-6-win64.zip"
builder.FILES += (
    "leanwatts_can/operator_page.py", "leanwatts_can/automation.py",
    "leanwatts_can/tests/test_operator_page.py", "leanwatts_can/tests/test_automation.py",
    "tools/smoke_portable.py", "research/load_bank.md",
)
builder.INSTRUCTIONS = """CAN CALIBRATION - OPERATOR WORKSPACE 6 (CUMULATIVE)

INSTALL ON THE WORKING LAPTOP
1. Close our GUI. Extract this update and copy its contents INTO the existing
   CAN-Calibration folder containing runtime and the launchers. Replace files.
2. Run Start-CAN-Calibration.cmd for the Operator page. The header must say
   OPERATOR WORKSPACE 6. Start-Original-UI.cmd still opens the manual page.
3. Your working manual calibration is unchanged (Manual calibration update 5).
   Saved variants, calibration_data, drivers and Windows settings are preserved.

OPERATOR SCREEN
- Separate Auto calibration and Auto test & report actions, on one screen.
- Saved variant, serial and operator identity; stage, target, measured value,
  error, settling progress and results; side panel with current/voltage/faults.
- Actual CAN data is observed from the existing manual CAN connection. Stale
  readings disappear. These charger readings are not load-reference readings.
- Fixed laptop layout; no whole-window scrolling.
- Completed/stopped/failed demo runs automatically save JSON, CSV and printable
  HTML. Open report or History & reports > Open selected report views the HTML.

WHAT IS STILL NEEDED FOR REAL AUTOMATION
Real auto calibration and acceptance tests are not enabled in this release.
The load is USB-connected, but its exact M8437B programming protocol is still
unverified. No guessed load commands are implemented. Connections exports
calibration_data/connection-report.json without opening an instrument port.
Provide that identity report and the M84 programming manual/vendor software.
End-current testing also needs the team's actual termination-test procedure.

To review the complete UI flow without hardware, explicitly select Simulation
on Original UI or launch Start-CAN-Calibration.cmd --simulation. Operator
buttons then say Demo calibration and Demo test & report. These use only the
isolated demo adapter and are not live hardware results. Real mode never falls
back to demo. Incomplete end-current checks cannot become a full test PASS.

The automatic workflow engine uses bounded duty steps, independent fresh
measurements, stability checks and timeouts. Its future hardware adapter needs
verified load control, exclusive CAN ownership and commissioned trim bounds.
Standalone tests prohibit all CAN commands, including background polls.
Save request acceptance never proves saved-trim persistence after power cycling.
"""

if __name__ == "__main__":
    builder.main(note_name="OPERATOR-WORKSPACE-6.txt")
