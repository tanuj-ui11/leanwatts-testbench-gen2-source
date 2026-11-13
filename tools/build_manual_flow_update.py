"""Package the independent manual calibration flow recovered from the Chinese UI."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_controls_update as builder

builder.PATCH = builder.ROOT / "dist/CAN-Manual-Calibration-Update-5.zip"
builder.FULL = builder.ROOT / "dist/CAN-Calibration-Live-Manual-CalibrationUpdate5-win64.zip"
builder.FILES += ("leanwatts_can/tests/test_manual_bench_setup.py",
                  "leanwatts_can/tests/test_manual_readback_compatibility.py",
                  "leanwatts_can/tests/test_manual_calibration_flow.py",
                  "README-Portable.txt", "README-CAN-Calibration.md",
                  "reverse_engineering/protocol-report.md")
builder.INSTRUCTIONS = """CAN CALIBRATION - MANUAL CALIBRATION UPDATE 5 (CUMULATIVE)

INSTALL ON THE WORKING LAPTOP
1. Close our calibration GUI and the Chinese charger application.
2. Extract this update and copy its contents INTO your existing CAN-Calibration
   folder containing Start-Original-UI.cmd and runtime. Replace included files.
3. Reopen Start-Original-UI.cmd. Confirm "Manual calibration update 5" appears.

Includes all earlier fixes. Saved variants, calibration_data, drivers, SDK DLLs
and Windows settings are preserved. This replaces Update 4's readback gate.

CALIBRATION WITHOUT UPDATING PARAMETERS
1. Open device and Start CAN in Live manual control.
2. Use Bench limits to enter the approved voltage/current/power ceilings for
   the test setup. This is local setup and sends no charger parameter writes.
3. Set the load bank for your validated current- or voltage-calibration step.
4. Click Shutdown and confirm physical output is off.
5. Review/edit the two displayed Voltage duty and Current duty values. Both
   initialize to 80.0, as in the original app. These are duty values, not V/A
   measurements or retrieved calibration values. Typing sends nothing.
6. Click Cal. start. It sends the recovered start command, then BOTH displayed
   duties three times, with nominal 100 ms pacing. These affect output live.
7. Use +/- or Apply duties while observing the independent load-bank reading.
   When the measurement is correct, click Save full current or Save voltage.
8. For the next calibration stage, change the load-bank setup and explicitly
   press Cal. start again if needed. It uses the displayed duties; no parameter
   update or extra Shutdown is required within the same calibration session.
9. Verify the saved calibration after a power cycle and perform the required
   standalone checks with all CAN commands stopped.

Send data is OPTIONAL and is not part of this calibration-only sequence.
No charger configuration input, prior write, matching 12/16 readbacks, or
18FF56E5 response is required for manual Cal. start, tuning or save. The four
Adj. V/I fields can remain at their original 0 defaults. Calibration does not
write those fields or any of the 16 parameter settings. Missing actual readbacks
remain unconfirmed rather than being manufactured as zero measurements.

PARAMETER UPDATE IS A SEPARATE ACTION
Only use Send data when you intend to change charger parameters. It validates
and sends all 16 explicit entries and reports readback/mismatch results. A
completed write's pending/failed readback check does not block calibration.
An actual write still in progress must finish or be interrupted by Shutdown
before another manual operation is dispatched. Work start uses the charger's
existing settings; edited fields are not sent implicitly by starting it.

LIVE OUTPUT AND SAVING
- Typing a duty value changes the screen only.
- Cal. start sends start plus the three initial duty pairs.
- +/- and Apply duties send live duty adjustments.
- Save voltage/full current/half current sends its save command only on click.
No save command is sent automatically. Firmware storage/persistence is not
proven by API acceptance; verify by power cycling after the save.

The manual path still checks the live connection, operator action sequence,
explicit bench limits, recent received over-limit output and valid duties.
Shutdown remains prioritized; incomplete/failed/cancelled startup cannot mark
duties applied. Connection changes cancel queued operations, with no replay.
No speculative CAN selector or periodic BMS control stream is added.

Closing/resetting CAN stops communication, not charger output. The Maynuo load
is still operated manually; automatic calibration remains unavailable pending
the exact load-control protocol. Software validation uses fake transport only.
"""

if __name__ == "__main__":
    builder.main(note_name="MANUAL-CALIBRATION-UPDATE-5.txt")
