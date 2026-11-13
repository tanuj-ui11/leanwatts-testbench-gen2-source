"""One supervised MCU identification attempt. No flash/write/reset commands."""
from pathlib import Path
import json
import subprocess
import sys
from datetime import datetime, timezone

root = Path(__file__).resolve().parents[1]
folder = root / "research/gdutility-official/GD-LinkUtilityProgrammer"
out = root / "artifacts/gdlink-connection-20261112"
out.mkdir(parents=True, exist_ok=True)
script_name = sys.argv[1] if len(sys.argv) > 1 else "probe-connect.gdlink"
if script_name not in {"probe-connect.gdlink", "probe-backup.gdlink", "program-verify.gdlink"}:
    raise SystemExit("Unknown supervised script")
args = [str(folder / "GDLink_CLI.exe"), "-speed", "1000", "-commandfile", "-e", script_name]
proc = subprocess.Popen(args, cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
try:
    data, _ = proc.communicate(timeout=60)
except subprocess.TimeoutExpired:
    if script_name == "program-verify.gdlink":
        print("Programming process is still running; waiting for its result, do not disconnect.", flush=True)
        data, _ = proc.communicate()
    else:
        proc.terminate()
        data, _ = proc.communicate(timeout=5)
        print("Read-only script timed out and was closed. No programming command was sent.")
stem = script_name.removesuffix(".gdlink")
(out / (stem + "-output.txt")).write_bytes(data)
(out / (stem + "-result.json")).write_text(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(),
    "args": args, "returncode": proc.returncode, "flash_commands": script_name == "program-verify.gdlink",
    "ac_off_confirmed": True}, indent=2), encoding="utf-8")
print(data.decode("utf-8", errors="replace"))
print("EXIT CODE:", proc.returncode)
