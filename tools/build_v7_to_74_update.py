"""Create a cumulative source/file update; never imports or runs the GUI."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PREFIX = 'CAN-Calibration/'
BASES = [ROOT / f'dist/CAN-Calibration-Bench-Automation-{version}-win64.zip'
         for version in ('7', '7.1', '7.2', '7.3')]
TARGET = ROOT / 'dist/CAN-Calibration-Auto-7.4-win64.zip'
OUTPUT = ROOT / 'dist/CAN-Upgrade-7-to-7.4.zip'
NOTE_NAME = 'UPGRADE-7-TO-7.4.txt'
NOTE = '''DIRECT UPGRADE: BENCH AUTOMATION 7 -> AUTO CALIBRATION 7.4

This cumulative update also supports versions 7.1, 7.2 and 7.3.
No intermediate installation is needed.

1. Close our calibration GUI and the Chinese charger application.
2. Open the existing CAN-Calibration folder containing
   Start-CAN-Calibration.cmd, runtime and leanwatts_can.
3. Extract ALL contents of CAN-Upgrade-7-to-7.4.zip directly INTO that folder.
   Choose Replace the files when prompted. Do not create another nested
   CAN-Calibration folder and do not run the program from inside the ZIP.
4. Open Start-CAN-Calibration.cmd. The header must show
   AUTO CALIBRATION 7.4 (the screen uses a middle dot before the version).

The ZIP contains no calibration_data files. Existing recipes, station
settings, reports and logs are preserved. It does not alter drivers or
Windows settings. All necessary updates since version 7 are included.

CALIBRATE
In Load setup, choose the load's actual COM port, use 9600 / address 1 / N,
and click Save calibration connection. Select/save the variant's current
and voltage targets, tolerances, calibration load settings and bench limits.
On Operator select the variant, enter the unit serial and operator, then
Start auto calibration. It connects CAN/load, detects adjustment direction,
calibrates and saves current, then calibrates and saves voltage.
The known load profile uses model 0x008C / firmware 0x002A, CC 0x3B01 and
CV 0x3B02. Starting duties are 80.0 with the supplied 60-85 bounds.
No Send data, manual direction check or separate production test is required.

This release has not had software tests or hardware checks run, as requested.
Calibration save-command acceptance does not prove persistence after restart.
'''


def digest(data):
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
        raise ValueError(f'Unsafe archive path: {name}')
    if path.parts and path.parts[0].casefold() == 'calibration_data':
        raise ValueError('An application update must not contain station data.')


def main():
    if OUTPUT.exists():
        raise SystemExit('Cumulative release already exists; preserving it.')
    with zipfile.ZipFile(TARGET) as target:
        final_manifest = json.loads(target.read(PREFIX + 'MANIFEST.json'))
        changed = set()
        for base_path in BASES:
            with zipfile.ZipFile(base_path) as base:
                base_manifest = json.loads(base.read(PREFIX + 'MANIFEST.json'))
                removed = set(base_manifest) - set(final_manifest)
                if removed:
                    raise ValueError(f'File removals need an explicit migration: {sorted(removed)}')
                changed.update(name for name, expected in final_manifest.items()
                               if base_manifest.get(name) != expected)
        files = {}
        for name in sorted(changed):
            safe_name(name)
            data = target.read(PREFIX + name)
            if digest(data) != final_manifest[name]:
                raise ValueError(f'Target release content differs from its manifest: {name}')
            files[name] = data
        files[NOTE_NAME] = NOTE.encode('utf-8')
        final_manifest[NOTE_NAME] = digest(files[NOTE_NAME])
        files['MANIFEST.json'] = (json.dumps(final_manifest, indent=2) + '\n').encode('utf-8')
        with zipfile.ZipFile(OUTPUT, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as update:
            for name, data in files.items():
                safe_name(name)
                update.writestr(name, data)
    checksum = hashlib.file_digest(OUTPUT.open('rb'), 'sha256').hexdigest()
    OUTPUT.with_suffix('.zip.sha256').write_text(checksum + '  ' + OUTPUT.name + '\n', encoding='ascii')
    report = {'artifact': str(OUTPUT), 'bytes': OUTPUT.stat().st_size,
              'sha256': checksum, 'supported_base_versions': ['7', '7.1', '7.2', '7.3'],
              'target_version': '7.4', 'changed_files': sorted(changed),
              'station_data_included': False, 'application_executed': False,
              'tests_run': False, 'hardware_access': False}
    (ROOT / 'artifacts/cumulative-upgrade-7-to-7.4.json').write_text(
        json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
