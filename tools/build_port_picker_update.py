"""Small port-selector fix against the verified Automation 7 package."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_controls_update as builder

builder.BASE = builder.ROOT / "dist/CAN-Calibration-Bench-Automation-7-win64.zip"
builder.PATCH = builder.ROOT / "dist/CAN-Port-Selector-Fix-7.1.zip"
builder.FULL = builder.ROOT / "dist/CAN-Calibration-Bench-Automation-7.1-win64.zip"
builder.FILES = ("leanwatts_can/load_setup.py", "leanwatts_can/tests/test_load_setup.py")
builder.INSTRUCTIONS = """PORT SELECTOR FIX 7.1 - FOR BENCH AUTOMATION 7

Close the GUI. Copy the extracted contents INTO the existing CAN-Calibration
folder and replace included files. Open Start-CAN-Calibration.cmd. Load setup
now shows 'port selector 7.1'. Saved station setup and variants are preserved.

Click Find ports. A separate selection window now lists each COM port and its
Windows description. Select the Maynuo load's port and click Use selected port.
Then click Read load. Selecting a port does not open or control an instrument.
Cancel keeps the previous selection and saved setup. Changing ports clears old
identity/mode confirmations because they belong to the previous connection.

This COM port is for the Maynuo load, not the USB-CAN adapter. If several ports
appear, identify the load from its Windows device information; do not guess.
No driver, communication setting or Windows protection is changed by this fix.
"""

if __name__ == "__main__":
    builder.main(note_name="PORT-SELECTOR-FIX-7.1.txt")
