"""Run the user's authorized AC-off programming job with fresh evidence."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import sys
import uuid

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from leanwatts_can.gdlink_cli import (
    ProgrammingJob, discover_programmers, inspect_cli_tool, inspect_firmware,
)

serial = "36390B7A0257"
vendor = root / "research/gdutility-official/GD-LinkUtilityProgrammer/GDLink_CLI.exe"
source = Path.home() / "Downloads/57.3V20A-261102.bin"
expected = "fa1ffade39c8725eb1d4a19600b058395c00b69c503baf3d226fa7a52c4a66a4"
profile = dict(inspect_cli_tool(vendor), approved=True, declared_mcu="GD32F103C8T6",
               flash_start=0x08000000, flash_size_kib=64, speed_khz=1000,
               programmer_serial=serial)
image = inspect_firmware(source, profile)
if image.sha256 != expected:
    raise SystemExit("Firmware differs from the supplied, previously verified file.")
targets = [target for target in discover_programmers() if target.serial_number == serial]
if len(targets) != 1:
    raise SystemExit("The approved GD-Link USB adapter is absent or ambiguous.")
folder = root / "artifacts/gdlink-supervised-runs" / (
    datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
folder.mkdir(parents=True)
print("Evidence:", folder, flush=True)

def event_sink(event):
    print(event.get("phase", "") + ": " + event.get("detail", ""), flush=True)

result = ProgrammingJob(targets[0], profile, image, ac_off_confirmed=True,
                        evidence_dir=folder, event_sink=event_sink).run()
(folder / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
summary = {key: result.get(key) for key in (
    "status", "detail", "submitted", "elapsed_seconds", "target_mcu_verified",
    "independent_readback_verified", "backup_sha256", "readback_sha256", "process_running")}
print(json.dumps(summary, indent=2), flush=True)
for execution in result.get("executions", []):
    print(execution["phase"] + " console:\n" + execution["stdout"], flush=True)
raise SystemExit(0 if result["status"] == "VERIFIED" else 1)
