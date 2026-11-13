"""Cumulative real bench automation release, with manual Update 5 preserved."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_operator_update import builder

builder.PATCH = builder.ROOT / "dist/CAN-Bench-Automation-Update-7.zip"
builder.FULL = builder.ROOT / "dist/CAN-Calibration-Bench-Automation-7-win64.zip"
builder.FILES += (
    "leanwatts_can/core.py", "leanwatts_can/maynuo.py", "leanwatts_can/bench_session.py",
    "leanwatts_can/load_setup.py", "leanwatts_can/tests/test_core.py",
    "leanwatts_can/tests/test_maynuo.py", "leanwatts_can/tests/test_load_setup.py",
    "leanwatts_can/tests/test_bench_session.py", "leanwatts_can/tests/test_recipe_automation.py",
    "leanwatts_can/tests/test_live_automation_ui.py",
    "research/maynuo/PROTOCOL-SOURCES.md", "research/maynuo/M97-user-provided-2010011957866497.pdf",
)
builder.INSTRUCTIONS = """CAN CALIBRATION - BENCH AUTOMATION 7 (CUMULATIVE)

INSTALL
Close our GUI. Copy this update's extracted contents INTO the existing
CAN-Calibration folder, replacing included files. Open Start-CAN-Calibration.cmd
and confirm BENCH AUTOMATION 7. Manual calibration remains Update 5.
Saved variants, reports, drivers and Windows settings are preserved.

ONE-TIME ENGINEERING SETUP ON THE WORKING LAPTOP
1. Close the Chinese GUI and close our manual CAN adapter connection.
2. Load setup: select the USB COM port and the load's front-panel serial settings.
3. Read load. Compare V/I with its front panel. With input OFF, select CC and
   click Read CC mode; then select CV and click Read CV mode.
4. Confirm matching readings/protocol compatibility and save station setup.
   This initial check reads only. Every real run rechecks instrument identity.
5. Calibration tuning: enter the starting duties, bounds and directions verified
   in manual calibration. No guessed tuning bounds are enabled automatically.
6. Qualify continuous CC reduction with input staying ON before enabling the
   cutoff ramp. Its behavior cannot be established by a read-only probe alone.

VARIANT RECIPE
Required output and Programmed value are separate columns. Required is the
physical acceptance target; Programmed may include the team's compensation.
For example, programmed 1.5 A and observed 1.1 A remain distinct. Enter the actual
required cutoff explicitly, rather than deriving it from either example value.
Saving a recipe sends no charger parameter update. Manual Send data remains
separate and uses the programmed values if supplied.

DC LOAD TESTS
Set output-voltage test CC and full-current test CV per variant. This variant
needs 49 V for full-current checking because the user reports derating at 48 V.
Optional: multiple CC points for voltage regulation; multiple CV points for
current regulation; sustained-load duration/current; end-current ramp from the
chosen start (the team uses 5 A). Power is measured V times measured I.
No AC-efficiency, ripple or extra-instrument tests are included.

OPERATOR
Select variant, scan serial, enter operator, press Auto calibration OR Auto test
and report. These are separate processes. Calibration uses stored charger
settings; Send data is not required. Live independent V/I, target/error, stage
and progress are shown. Other control pages lock while the run owns the bench.
Before standalone testing, restart charger power without CAN start commands and
tick the startup confirmation. Tests open listen-only CAN and transmit nothing.
Test Stop turns off the load only: charger power remains externally controlled.

REPORTS
JSON, CSV and printable HTML save automatically on finish, stop or error.
Required/programmed/measured values and cutoff evidence remain separate.
Cutoff is an inferred bracket from the last conducting reading and first off
step, never zero after shutdown. An ambiguous bracket is INDETERMINATE.
Untested stages are NOT TESTED. Save acceptance does not prove persistence.

EVIDENCE / LIMITS
Real M97-compatible Modbus RTU commands are implemented from your supplied
manual. M84 equivalence is user-asserted and qualified on the actual bench by
the setup checks; the manual itself does not prove it. USB must expose COM.
No guessed CC/CV readback enumeration is used. CRC, identity, status, input,
mode/setpoint and fresh independent measurements are checked. There are no
automatic write retries or simulation fallbacks after communication errors.
UNREG is treated as inconclusive fault status, not accepted as proof of cutoff.
Software tests use fake instruments. This build has not run on your physical
bench. Read README-CAN-Calibration.md for the full workflow and source notes.
"""

if __name__ == "__main__":
    builder.main(note_name="BENCH-AUTOMATION-7.txt")
