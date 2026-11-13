"""Bounded raw read-only checks on the user-identified Maynuo COM3 connection.

Only the documented voltage/current/model/coil read queries are transmitted.
No port scanning, baud changes, device settings, mode/input or CAN commands.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import serial
from serial.tools import list_ports
from leanwatts_can.maynuo import crc16, frame

parser = argparse.ArgumentParser()
parser.add_argument("--connection", choices=("connected", "db9-unplugged", "cable-loopback"), required=True)
args = parser.parse_args()
ports = [p for p in list_ports.comports() if p.vid == 0x1A86 and p.pid == 0x55D3 and p.serial_number == "58B5065450"]
if len(ports) != 1:
    raise SystemExit("The identified CH343 adapter is not uniquely present; no port was opened.")
port = ports[0]
port_name = port.device

queries = [
    ("voltage_1", "01 03 0B 00 00 02"),
    ("model", "01 03 0B 06 00 01"),
    ("input_state", "01 01 05 10 00 01"),
]
if args.connection != "connected":
    queries = queries[:1]
report = {"checked_at": datetime.now(timezone.utc).isoformat(),
          "port": port_name, "description": port.description, "hwid": port.hwid,
          "baudrate": 9600, "parity": "N", "data_bits": 8, "stop_bits": 1,
          "operator_reported_connection": args.connection,
          "instrument_writes": False, "transactions": []}

def capture(connection, seconds):
    chunks = []
    deadline = time.monotonic() + seconds
    total = 0
    while time.monotonic() < deadline and total < 128:
        data = connection.read(min(64, 128 - total))
        if data:
            chunks.append({"after_ms": round((time.monotonic() - (deadline - seconds)) * 1000, 1),
                           "hex": data.hex(" ").upper()})
            total += len(data)
    return chunks

try:
    with serial.Serial(port_name, baudrate=9600, bytesize=8, parity="N", stopbits=1,
                       timeout=0.05, write_timeout=0.5, xonxoff=False, rtscts=False, dsrdtr=False) as connection:
        report["idle_rx"] = capture(connection, 0.25)
        for name, payload in queries:
            request = frame(bytes.fromhex(payload))
            assert request[1] in (1, 3)
            connection.reset_input_buffer()
            sent = connection.write(request)
            connection.flush()
            chunks = capture(connection, 0.8)
            raw = b"".join(bytes.fromhex(chunk["hex"]) for chunk in chunks)
            expected_bytes = 1 if request[1] == 1 else 2 * int.from_bytes(request[4:6], "big")
            valid_reply = (len(raw) == expected_bytes + 5 and raw[:3] == bytes((1, request[1], expected_bytes))
                           and crc16(raw) == 0)
            report["transactions"].append({"query": name, "tx_hex": request.hex(" ").upper(),
                "sent_bytes": sent, "rx_hex": raw.hex(" ").upper(), "chunks": chunks,
                "whole_rx_crc_valid": len(raw) >= 4 and crc16(raw) == 0,
                "valid_expected_reply": valid_reply, "exact_request_echo": raw == request,
                "loopback_pass": raw == request if args.connection == "cable-loopback" else None})
except Exception as exc:
    report["error"] = f"{type(exc).__name__}: {exc}"
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
destination = ROOT / f"artifacts/local-load-{args.connection}-{stamp}.json"
destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
print("Saved:", destination)
