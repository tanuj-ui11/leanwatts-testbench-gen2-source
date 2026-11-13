"""Bounded control check for the identified M8437B; never enables its input.

Uses operator-observed CC/CV codes. Saves original settings before any write,
checks independent readback after each operation, restores settings and local
control, and records every transaction. No CAN or load calibration commands.
"""
import json
import math
import struct
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from serial.tools import list_ports
from leanwatts_can.maynuo import LoadIdentity, MaynuoLoad, SerialSettings


class RecordedLoad(MaynuoLoad):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.all_transactions = []

    def _exchange(self, function, body, reply_bytes=None):
        # Explicit independent allowlist: read, remote PC1, CC/CV/OFF, IFIX/UFIX.
        if function == 5:
            assert body in (bytes.fromhex('05 00 FF 00'), bytes.fromhex('05 00 00 00'))
        elif function == 16:
            address, count, size = struct.unpack('>HHB', body[:5])
            assert (address == 0x0A00 and count == 1 and size == 2
                    and int.from_bytes(body[5:], 'big') in (1, 2, 43)) or (
                        address in (0x0A01, 0x0A03) and count == 2 and size == 4)
        else:
            assert function in (1, 3)
        try:
            return super()._exchange(function, body, reply_bytes)
        finally:
            self.all_transactions.append(self.diagnostics()['transactions'][-1])


ports = [p for p in list_ports.comports()
         if p.vid == 0x1A86 and p.pid == 0x55D3 and p.serial_number == '58B5065450']
if len(ports) != 1:
    raise SystemExit('Identified CH343 adapter is not uniquely present; no port opened.')
load = RecordedLoad(SerialSettings(ports[0].device, 9600, 1, 'N', 0.5), 500, 120, 3700)
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
destination = ROOT / 'artifacts' / f'load-off-control-check-{stamp}.json'
report = {'checked_at': datetime.now(timezone.utc).isoformat(),
          'input_on_command_permitted': False, 'can_accessed': False,
          'operator_observed_modes': {'CC': 0x3B01, 'CV': 0x3B02},
          'steps': [], 'success': False}
original = None
remote_attempted = False


def save():
    report['transactions'] = load.all_transactions
    destination.write_text(json.dumps(report, indent=2), encoding='utf-8')


def off_read():
    reading = load.read()
    if reading.input_on or reading.mode not in ('CC', 'CV'):
        raise RuntimeError('Expected input OFF and a verified CC/CV mode; no further test writes.')
    # UNREG was observed with input OFF; it is recorded, never cleared here.
    if any(not fault.startswith('UNREG') for fault in reading.faults):
        raise RuntimeError('A load protection/error flag is present; no test writes.')
    return reading


def step(name, action):
    before = off_read()
    action()
    after = off_read()
    report['steps'].append({'step': name, 'before': asdict(before), 'after': asdict(after)})
    save()


try:
    load.open()
    identity = load.probe()
    if identity != LoadIdentity(140, 42):
        raise RuntimeError('Load identity differs from the verified device; no writes.')
    load.commission(identity, 0x3B01, 0x3B02)
    before = off_read()
    if before.remote:
        raise RuntimeError('Load already in remote control; stop to avoid another controller.')
    current = struct.unpack('>f', load._registers(0x0A01, 2))[0]
    voltage = struct.unpack('>f', load._registers(0x0A03, 2))[0]
    if not (math.isfinite(current) and 0.1 < current <= 120
            and math.isfinite(voltage) and 0.1 < voltage <= 500):
        raise RuntimeError('Existing setpoints unsuitable for bounded OFF-only check; no writes.')
    original = {'mode': before.mode, 'current': current, 'voltage': voltage,
                'reading': asdict(before)}
    report['original'] = original
    save()  # Persist restoration values before the first setting write.
    remote_attempted = True
    step('Enable remote; verify input stays OFF', load.enable_remote)
    step('Confirm OFF command readback', lambda: load.set_input(False))
    step('CC: reduce saved setpoint by 0.1 A with input OFF',
         lambda: load.configure('CC', round(current - 0.1, 4)))
    step('CC: restore original setpoint', lambda: load.configure('CC', current))
    step('CV: reduce saved setpoint by 0.1 V with input OFF',
         lambda: load.configure('CV', round(voltage - 0.1, 4)))
    step('CV: restore original setpoint', lambda: load.configure('CV', voltage))
    report['control_checks_passed'] = True
except Exception as exc:
    report['error'] = f'{type(exc).__name__}: {exc}'
finally:
    if remote_attempted and original is not None:
        try:
            reading = off_read()
            if reading.remote:
                # Restore both setpoints and the initial mode, with independent
                # readback; never enable input, even during error cleanup.
                order = ('CV', 'CC') if original['mode'] == 'CC' else ('CC', 'CV')
                for mode in order:
                    value = original['current' if mode == 'CC' else 'voltage']
                    step(f'Final restore {mode}', lambda m=mode, v=value: load.configure(m, v))
                step('Return front-panel control', load.disable_remote)
            final = off_read()
            actual_current = struct.unpack('>f', load._registers(0x0A01, 2))[0]
            actual_voltage = struct.unpack('>f', load._registers(0x0A03, 2))[0]
            report['final'] = asdict(final)
            report['restored'] = (not final.remote and final.mode == original['mode']
                                  and actual_current == original['current']
                                  and actual_voltage == original['voltage'])
            report['success'] = bool(report.get('control_checks_passed') and report['restored'])
        except Exception as exc:
            report['restoration_error'] = f'{type(exc).__name__}: {exc}'
    load.close()
    report['port_closed'] = True
    save()
summary = {k: v for k, v in report.items() if k not in ('transactions', 'steps')}
summary['steps_completed'] = [s['step'] for s in report['steps']]
summary['transactions_count'] = len(load.all_transactions)
summary['saved_file'] = str(destination)
print(json.dumps(summary, indent=2))
