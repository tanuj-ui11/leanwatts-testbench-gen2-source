"""Validate the transferred Operator release with its own runtime, offline."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
bench7 = "--bench7" in sys.argv
port_fix = "--port-fix" in sys.argv
load_diagnostics = "--load-diagnostics" in sys.argv
load_control = "--load-control" in sys.argv
archive = ROOT / ("dist/CAN-Calibration-Bench-Automation-7.3-win64.zip" if load_control else "dist/CAN-Calibration-Bench-Automation-7.2-win64.zip" if load_diagnostics else "dist/CAN-Calibration-Bench-Automation-7.1-win64.zip" if port_fix else "dist/CAN-Calibration-Bench-Automation-7-win64.zip" if bench7 else "dist/CAN-Calibration-Operator-Workspace-6-win64.zip")
folder = Path(tempfile.mkdtemp(prefix="CAN Operator Transfer "))
with zipfile.ZipFile(archive) as bundle:
    bundle.extractall(folder)
package = folder / "CAN-Calibration"
manifest = json.loads((package / "MANIFEST.json").read_text(encoding="utf-8"))
for name, expected in manifest.items():
    data = (package / name).read_bytes()
    assert len(data) == expected["bytes"] and hashlib.sha256(data).hexdigest() == expected["sha256"], name
python = package / "runtime/python.exe"
env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
commands = {
    "tests": [str(python), "-c", 'import os,sys,unittest;sys.path.insert(0,os.getcwd());r=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover("leanwatts_can/tests"));sys.exit(not r.wasSuccessful())'],
    "smoke": [str(python), "tools/smoke_portable.py", str(package)],
}
results = {}
for name, command in commands.items():
    outcome = subprocess.run(command, cwd=package, env=env, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=180,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    results[name] = {"exit_code": outcome.returncode, "output": outcome.stdout + outcome.stderr}
report = {"status": "passed" if all(item["exit_code"] == 0 for item in results.values()) else "failed",
          "package_root": str(package), "manifest_files_verified": len(manifest),
          "hardware_access": False, "results": results}
destination = ROOT / ("artifacts/load-control-7.3-validation.json" if load_control else "artifacts/load-diagnostics-7.2-validation.json" if load_diagnostics else "artifacts/port-selector-7.1-validation.json" if port_fix else "artifacts/bench-automation-7-validation.json" if bench7 else "artifacts/operator-workspace-6-validation.json")
destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps({"status": report["status"], "package_root": str(package), "manifest_files": len(manifest),
                  "report": str(destination), "checks": {name: item["exit_code"] for name, item in results.items()}}))
if report["status"] != "passed":
    print(json.dumps(results, indent=2))
    raise SystemExit(1)
