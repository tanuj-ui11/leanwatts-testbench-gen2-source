from pathlib import Path
import hashlib
import json
import struct

root = Path(__file__).resolve().parents[1]
folder = root / 'research/gdutility-official/GD-LinkUtilityProgrammer'
out = root / 'artifacts/gdlink-connection-20261112'
backup = (folder / 'backup-original-20261112.bin,').read_bytes()
result = json.loads((out / 'probe-backup-result.json').read_text())
log = (out / 'probe-backup-output.txt').read_text()
assert result['returncode'] == 0
assert 'Current SN  36390B7A0257' in log
assert 'Target Device: GD32F103C8T6, Flash Size: 64KB, SRAM Size: 20KB' in log
assert 'Memory read successfully' in log and len(backup) == 65536
source = (Path.home() / 'Downloads/57.3V20A-261102.bin').read_bytes()
assert len(source) == 65536
expected = 'fa1ffade39c8725eb1d4a19600b058395c00b69c503baf3d226fa7a52c4a66a4'
assert hashlib.sha256(source).hexdigest() == expected
assert not (folder / 'readback-20261112.bin').exists()
(out / 'original-flash-backup.bin').write_bytes(backup)
(folder / 'input-20261112.bin').write_bytes(source)
(folder / 'program-verify.gdlink').write_text('c 36390B7A0257\nsi 1\nload input-20261112.bin 8000000\nsavebin readback-20261112.bin 8000000 10000\nq\n', encoding='ascii')
summary = dict(backup_bytes=len(backup), backup_sha256=hashlib.sha256(backup).hexdigest(),
               source_sha256=expected, already_matches=backup == source,
               backup_vectors=[hex(n) for n in struct.unpack('<II', backup[:8])],
               source_vectors=[hex(n) for n in struct.unpack('<II', source[:8])])
(out / 'pre-program-check.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
print(json.dumps(summary, indent=2))
