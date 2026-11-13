"""Package the narrowly scoped manual calibration readback update."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_controls_update as builder

builder.PATCH = builder.ROOT / "dist/CAN-Manual-Calibration-Update-4.zip"
builder.FULL = builder.ROOT / "dist/CAN-Calibration-Live-Manual-CalibrationUpdate4-win64.zip"
builder.FILES += ("leanwatts_can/tests/test_manual_bench_setup.py",
                  "leanwatts_can/tests/test_manual_readback_compatibility.py",
                  "README-Portable.txt", "README-CAN-Calibration.md",
                  "reverse_engineering/protocol-report.md")
builder.INSTRUCTIONS = """CAN CALIBRATION - MANUAL CALIBRATION UPDATE 4 (CUMULATIVE)

INSTALL ON THE WORKING LAPTOP
1. Close our calibration GUI and the Chinese charger application.
2. Extract this update and copy its contents INTO your existing CAN-Calibration
   folder containing Start-Original-UI.cmd and runtime. Replace included files.
3. Reopen Start-Original-UI.cmd. Confirm "Manual calibration update 4" appears.

Includes all earlier updates. Saved variants, calibration_data, drivers, SDK
DLLs and Windows settings are preserved. No separate Python install is needed.

WHY CAL. START REMAINED GREY
Your reported status was: four missing readbacks, zero mismatches, missing
18FF56E5. The four fields are Adj. V, Adj. V lower, Adj. I, and Adj. I lower.
The Chinese app enables Cal. start after Shutdown; it does not require their
readback. Our previous all-16-field condition was an added application check.
Missing replies do not establish whether those four settings were applied.

WHAT THIS UPDATE ALLOWS
Manual Cal. start, duty adjustment, and calibration saves can proceed with
12 matching readbacks and those exact four adjustable settings unconfirmed.
This is a limited manual path, not complete configuration verification.
All of these conditions must hold:
- The complete settings/write-and-read-request sequence finished successfully.
- All 16 entered settings remain the same as the completed write.
- The normal readback waiting period has ended.
- Only the four fields from 18FF56E5 are absent; the other 12 are fresh and match.
- Output voltage/current telemetry is fresh and within the entered bench limits.

Any other missing field, mismatch, stale required reading, partial/cancelled
write, or changed input blocks that path. Changing limits, resetting/closing
CAN, Shutdown, or connection failure invalidates the prior write evidence.
Previously received but stale adjustable-limit fields are not treated as absent.
If all 16 values arrive fresh and match, full readback verification can succeed.

The page keeps the four values visibly unconfirmed. It does not synthesize
their readbacks or claim that calibration was saved permanently. Work start
retains its requirement for all 16 readbacks. Automatic calibration is not enabled.
No additional CAN command, speculative request group, retry or automatic start
has been introduced. Each action still requires an operator click.

WORKFLOW
1. Enter the variant's known working parameters in Original UI. Use Bench limits
   to apply the approved V/A/W ceilings, or load a complete saved variant.
2. Open device, Start CAN, then Shutdown. Confirm physical output is off.
3. Click Send data. Wait for the readback period to finish. Cal. start becomes
   available when the conditions above are met; four adjustable values remain
   labelled unconfirmed if 18FF56E5 is absent.
4. Click Cal. start. Enter both validated duty values, Apply duties, then adjust
   using the independent load-bank readings and the appropriate Save control.

Keep the known working values for Adj. V/I and their lower limits. Their labels
suggest voltage/current adjustment settings, but the exact firmware role of
the upper fields is not established. They are distinct from calibration duties.

Stop/Reset CAN and Close adapter stop communication, not physical output.
API acceptance is not charger acknowledgement or proof of saved calibration.
The Maynuo load is still operated manually. Bench results must be verified on
the working laptop; development tests use fake transport only.
"""

if __name__ == "__main__":
    builder.main(note_name="MANUAL-CALIBRATION-UPDATE-4.txt")
