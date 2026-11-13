from pathlib import Path
import json
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
vendor = root / 'research/gdutility-official/GD-LinkUtilityProgrammer'
folder = Path(tempfile.mkdtemp(prefix='lwgdlink-probe-'))
script = folder / 'read.gdlink'
script.write_text('c 36390B7A0257\nsi 1\nsavebin path-check.bin 8000000 10\nq\n', encoding='ascii')
result = subprocess.run([str(vendor / 'GDLink_CLI.exe'), '-speed', '1000', '-commandfile', '-e', str(script)],
    cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
print(result.stdout.decode('utf-8', errors='replace'))
data = dict(returncode=result.returncode, cwd=str(folder),
    cwd_file_exists=(folder / 'path-check.bin').is_file(), vendor_file_exists=(vendor / 'path-check.bin').exists())
if data['cwd_file_exists']:
    data['bytes'] = len((folder / 'path-check.bin').read_bytes())
    data['hex'] = (folder / 'path-check.bin').read_bytes().hex()
(root / 'artifacts/gdlink-connection-20261112/path-check.json').write_text(json.dumps(data, indent=2))
print(json.dumps(data, indent=2))
