# Leanwatts Testbench Gen2

End-of-line calibration and test workstation for the JK CAN battery charger, using a
Maynuo DC electronic load as the independent reference. Windows desktop app
(Python 3.12 + PySide6). Current version: **1.1.11** (`leanwatts_can/version.py`).

For each charger, an authenticated operator's session:

1. reads the charger's 16 parameters over CAN, compares them with the variant recipe and
   writes/verifies any mismatch;
2. calibrates full current, then voltage, by trimming duty against the load's readings and
   saving each;
3. runs the post-tests after an **operator AC restart** with zero application CAN commands:
   open-circuit voltage, full current at CV 49 V, and the end-current cutoff ramp
   (see `leanwatts_can/test_programs/README.txt`);
4. records the attempt in the session's JSON/CSV/HTML report.

Operator guide: [README-Portable.txt](README-Portable.txt). Feature overview:
[README-CAN-Calibration.md](README-CAN-Calibration.md). Update publishing:
[tools/README-UPDATE-PUBLISHING.txt](tools/README-UPDATE-PUBLISHING.txt).

## Validation status

Software tests use fake instruments only. One supervised bench run (1.1.8, 12 Nov 2026)
passed baseline and calibration; the 1.1.9+ external-restart test flow has not yet run on
hardware. Calibration persistence after an AC power cycle is not yet verified.

## Layout

| Path | Contents |
|---|---|
| `leanwatts_can/` | Application package and its unit tests (`leanwatts_can/tests`) |
| `tools/` | Release/installer build scripts, C# launcher/installer/CAN bridge sources, bench probes |
| `calibration_app/` | Earlier standalone C# calibration core and tests |
| `research/` | Hardware, load-bank, adapter and GD-Link notes |
| `reverse_engineering/` | Charger CAN protocol report and IL-dump tooling (vendor dumps not included) |

Not in this repository: built installers and archives (release assets), station data,
vendor software, firmware images, vendor manuals and decompiled vendor code.

## Running the tests

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
cd leanwatts_can/tests
QT_QPA_PLATFORM=offscreen PYTHONPATH=../.. ../../.venv/bin/python -m unittest discover -s . -p 'test_*.py'
```

On Windows, drop `QT_QPA_PLATFORM` and use `..\..\.venv\Scripts\python`.
Tests never open serial ports, CAN adapters or the network.
