"""Read-only response diagnostics and small field reads against release 7.1."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_controls_update as builder

builder.BASE = builder.ROOT / "dist/CAN-Calibration-Bench-Automation-7.1-win64.zip"
builder.PATCH = builder.ROOT / "dist/CAN-Load-Diagnostics-7.2.zip"
builder.FULL = builder.ROOT / "dist/CAN-Calibration-Bench-Automation-7.2-win64.zip"
builder.FILES = (
    "leanwatts_can/maynuo.py", "leanwatts_can/load_setup.py",
    "leanwatts_can/tests/test_maynuo.py", "leanwatts_can/tests/test_maynuo_diagnostics.py",
    "leanwatts_can/tests/test_load_setup.py",
)
builder.INSTRUCTIONS = """LOAD DIAGNOSTICS 7.2 - FOR BENCH AUTOMATION 7 / 7.1

Close the GUI. Extract the small update INTO the existing CAN-Calibration
folder and replace the included files. Open Start-CAN-Calibration.cmd.
Load setup now shows 'load diagnostics 7.2'. Station setup, variants and reports
are preserved. The full ZIP includes the Python runtime and the whole GUI.

For the load checked in this chat, its panel shows 9600 baud, address 1 and
NONE parity: choose 9600 / 1 / N in Load setup. Select its actual COM port on
this laptop (COM11 was identified on the other laptop; it may differ here).
Click Read load. If it fails, click Copy diagnostics and paste into the chat.
Each check also saves calibration_data/diagnostics/load-probe-latest.json.

The connection checks remain read-only. Voltage/current and setpoints now
use separate two-register reads; mode/model/firmware use one-register reads.
This avoids depending on a mixed eight-register response, but does not prove
compatibility with the attached load until an actual response is checked.

Diagnostics retain a bounded record of transmitted/received bytes, expected
and actual byte count, address, function, CRC result, timings and errors.
Malformed, short, corrupt or echoed read requests are rejected, not treated as
measurements. No automatic retries, baud scans or protocol write probes.
Windows protections and drivers are unchanged.

Panel settings: Shift, then 0 (Menu), CONFIG, Enter; use the arrow keys to find
BAUDRATE SET, ADDRESS SET and COMM.PARITY. Enter views each value; Esc returns.
NONE = N, EVEN = E, ODD = O. GUI values must match the load's front panel.
"""

if __name__ == "__main__":
    builder.main(note_name="LOAD-DIAGNOSTICS-7.2.txt")
