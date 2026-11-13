"""Documented M97-compatible Modbus RTU load adapter; no I/O on import/construction.

Source: Maynuo Electronics 2009 manual, pp. 31-40, supplied by the user:
http://www.maynuo.com/downloadfile/2010011957866497.pdf
Cross-checked against official M97 V3.9 (2021), sections 4.9.4-4.9.10.
M84 compatibility is user asserted, NOT established by those manuals. Every
connection starts read-only. Engineering commissioning supplies the panel-
observed CC/CV SETMODE codes because the manuals do not define that enum.
No SCPI, load calibration registers, remote-sense or local-lockout writes.
"""
from __future__ import annotations

import math
import copy
import struct
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable


SOURCE = "Maynuo M97 2009 manual §§4.8.7–4.8.9; M84 compatibility user asserted"
FAULTS = ("Over-current", "Over-voltage", "Over-power", "Over-temperature",
          "Reverse polarity", None, "EEPROM error", "Load calibration error")


class MaynuoError(RuntimeError):
    def __init__(self, message, *, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def frame(data: bytes) -> bytes:
    return data + struct.pack("<H", crc16(data))


def _number(value, label: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number.")
    value = float(value)
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise ValueError(f"{label} must be finite and {'positive' if positive else 'non-negative'}.")
    return value


@dataclass(frozen=True)
class SerialSettings:
    port: str
    baudrate: int = 9600
    address: int = 1
    parity: str = "N"
    timeout: float = 0.5

    def validate(self):
        if not isinstance(self.port, str) or not self.port.strip():
            raise ValueError("Choose the load-bank COM port.")
        if type(self.baudrate) is not int or self.baudrate not in (2400, 9600, 14400, 28800, 57600, 115200):
            raise ValueError("Choose a baud rate documented by the Maynuo manual.")
        if type(self.address) is not int or not 1 <= self.address <= 200:
            raise ValueError("Load-bank address must be 1 to 200; broadcasts are not supported.")
        if self.parity not in ("N", "O", "E"):
            raise ValueError("Parity must be N, O or E.")
        if not 0.05 <= _number(self.timeout, "Timeout", True) <= 2.0:
            raise ValueError("Transaction timeout must be 0.05 to 2 seconds.")


@dataclass(frozen=True)
class LoadIdentity:
    model_code: int
    firmware_code: int

    @property
    def label(self) -> str:
        return f"Maynuo-compatible load · model code 0x{self.model_code:04X} · firmware 0x{self.firmware_code:04X}"

    @property
    def source(self) -> str:
        return SOURCE


@dataclass(frozen=True)
class LoadReading:
    voltage: float
    current: float
    power: float
    mode_code: int
    mode: str
    setpoint: float | None
    input_on: bool
    faults: tuple[str, ...]
    acquired_at: float
    sequence: int
    remote: bool
    model_code: int
    firmware_code: int
    power_source: str = "calculated V × I"
    # Manual §§4.1.1.1/4.1.3.1: UNREG means the load cannot reach its
    # setpoint. It is expected without a source or during source shutdown,
    # and must not be confused with the independent protection/error bits.
    unregulated: bool = False


class MaynuoLoad:
    """Synchronous bounded driver, intended for one background bench worker.

    All state-changing methods return True only after independent readback;
    Modbus acknowledgement alone is insufficient. Methods do not retry writes.
    close() performs no protocol writes, so a read-only probe stays read-only.
    """
    def __init__(self, settings: SerialSettings, max_voltage: float,
                 max_current: float, max_power: float, *, serial_factory: Callable | None = None):
        settings.validate()
        self.settings = settings
        self.max_voltage = _number(max_voltage, "Bench voltage limit", True)
        self.max_current = _number(max_current, "Bench current limit", True)
        self.max_power = _number(max_power, "Bench power limit", True)
        self._serial_factory = serial_factory
        self._serial = None
        self._lock = threading.RLock()
        self._last_transaction = 0.0
        self._identity: LoadIdentity | None = None
        self._modes: dict[int, str] = {}
        self._commissioned = False
        self._sequence = 0
        self._transactions = deque(maxlen=64)
        self._active_rx = None

    def diagnostics(self) -> dict:
        """Bounded, detached serial evidence; never performs instrument I/O."""
        with self._lock:
            return {"serial_settings": asdict(self.settings),
                    "transactions": copy.deepcopy(list(self._transactions))}

    @property
    def commissioned(self) -> bool:
        return self._commissioned and self._serial is not None

    def open(self) -> bool:
        with self._lock:
            if self._serial is not None:
                raise MaynuoError("Load-bank port is already open.")
            factory = self._serial_factory
            self._transactions.clear()
            if factory is None:
                import serial  # lazy: importing this module never opens hardware
                factory = serial.Serial
            try:
                self._serial = factory(port=self.settings.port, baudrate=self.settings.baudrate,
                    bytesize=8, parity=self.settings.parity, stopbits=1,
                    timeout=self.settings.timeout, write_timeout=self.settings.timeout)
            except Exception as exc:
                raise MaynuoError(f"Could not open load-bank port: {exc}", diagnostics=self.diagnostics()) from exc
            self._identity = None
            self._modes.clear()
            self._commissioned = False
            self._sequence = 0
            return True

    def close(self) -> None:
        with self._lock:
            port, self._serial = self._serial, None
            self._commissioned = False
            self._identity = None
            self._modes.clear()
            if port is not None:
                port.close()

    def _exact(self, count: int, deadline: float) -> bytes:
        result = bytearray()
        while len(result) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MaynuoError("Load-bank response timed out; command result is unconfirmed.")
            self._serial.timeout = min(self.settings.timeout, remaining)
            part = self._serial.read(count - len(result))
            if not isinstance(part, bytes) or len(part) > count - len(result):
                raise MaynuoError("Malformed serial response.")
            result.extend(part)
            if self._active_rx is not None:
                self._active_rx.extend(part)
            if not part:
                time.sleep(min(0.001, remaining))
        return bytes(result)

    def _exchange(self, function: int, body: bytes, reply_bytes: int | None = None) -> bytes:
        if self._serial is None:
            raise MaynuoError("Load-bank port is not open.")
        request = frame(bytes((self.settings.address, function)) + body)
        # Manual requires >=3.5 characters; use its conservative 11 bits/char.
        gap = max(0.004, 3.5 * 11 / self.settings.baudrate)
        remaining = gap - (time.monotonic() - self._last_transaction)
        if remaining > 0:
            time.sleep(remaining)
        started = time.monotonic()
        record = {"started_at": datetime.now(timezone.utc).isoformat(),
                  "tx_hex": request.hex(" ").upper(), "rx_hex": "",
                  "expected_address": self.settings.address, "received_address": None,
                  "expected_function": function, "received_function": None,
                  "expected_byte_count": reply_bytes, "received_byte_count": None,
                  "crc_valid": None, "request_echo": False, "error": None}
        self._active_rx = bytearray()
        error = None
        try:
            self._serial.reset_input_buffer()
            if self._serial.write(request) != len(request):
                raise MaynuoError("Incomplete load-bank request; result is unconfirmed.")
            deadline = time.monotonic() + self.settings.timeout
            head = self._exact(2, deadline)
            record["received_address"], record["received_function"] = head
            if head[1] & 0x80:
                response = head + self._exact(3, deadline)
            elif head[1] in (1, 3):
                count = self._exact(1, deadline)
                record["received_byte_count"] = count[0]
                # The manual permits at most 32 words or 16 coils per read.
                # Never let an untrusted count create an unbounded read.
                maximum = 64 if head[1] == 3 else 2
                if count[0] > maximum:
                    if function in (1, 3) and head + count == request[:3]:
                        # Capture only the rest of the fixed eight-byte read
                        # request to diagnose a possible echo, never its bogus
                        # advertised payload length.
                        self._exact(len(request) - 3, deadline)
                    raise MaynuoError(f"Unexpected load-bank response byte count: got {count[0]}, expected {reply_bytes}; exceeds protocol limit {maximum}.")
                response = head + count + self._exact(count[0] + 2, deadline)
            elif head[1] in (5, 16):
                response = head + self._exact(6, deadline)
            else:
                raise MaynuoError(f"Unexpected load-bank response function: got 0x{head[1]:02X}, expected 0x{function:02X}.")
            record["crc_valid"] = response[-2:] == struct.pack("<H", crc16(response[:-2]))
            if response[0] != self.settings.address:
                raise MaynuoError(f"Load-bank response came from address {response[0]}; expected {self.settings.address}.")
            if not record["crc_valid"]:
                raise MaynuoError("Load-bank response CRC failed.")
            if response[1] == function | 0x80:
                raise MaynuoError(f"Load-bank Modbus exception {response[2]} for function {function}.")
            if response[1] != function:
                raise MaynuoError("Unexpected load-bank response function.")
            if function in (1, 3) and record["received_byte_count"] != reply_bytes:
                raise MaynuoError(f"Unexpected load-bank response byte count: got {record['received_byte_count']}, expected {reply_bytes}.")
            if function == 5 and response != request:
                raise MaynuoError("Load-bank coil acknowledgement does not match request.")
            if function == 16 and response[2:6] != body[:4]:
                raise MaynuoError("Load-bank register acknowledgement does not match request.")
        except Exception as exc:
            error = exc if isinstance(exc, MaynuoError) else MaynuoError(f"Load-bank communication failed: {exc}")
            # Read-request echoes cannot be valid register/coil replies. Report
            # them explicitly, but never skip them or treat them as success.
            if function in (1, 3) and bytes(self._active_rx).startswith(request):
                record["request_echo"] = True
                error = MaynuoError("Load returned an exact read-request echo; no verified reply was accepted.")
            elif (record["received_byte_count"] is not None and record["received_byte_count"] != reply_bytes
                  and "response byte count" not in str(error)):
                error = MaynuoError(f"Unexpected load-bank response byte count: got {record['received_byte_count']}, expected {reply_bytes}. {error}")
            record["error"] = str(error)
        finally:
            self._last_transaction = time.monotonic()
            record["elapsed_ms"] = round((self._last_transaction - started) * 1000, 1)
            record["rx_hex"] = bytes(self._active_rx).hex(" ").upper()
            self._active_rx = None
            self._transactions.append(record)
        if error is not None:
            error.diagnostics = self.diagnostics()
            raise error
        return response[3:-2] if function in (1, 3) else response[2:-2]

    def _registers(self, address: int, count: int) -> bytes:
        return self._exchange(3, struct.pack(">HH", address, count), count * 2)

    def _coils(self, address: int, count: int) -> int:
        return int.from_bytes(self._exchange(1, struct.pack(">HH", address, count), (count + 7) // 8), "little")

    def _write(self, address: int, data: bytes) -> None:
        self._require_commission()
        self._exchange(16, struct.pack(">HHB", address, len(data) // 2, len(data)) + data)

    def _command(self, command: int) -> None:
        if command not in (1, 2, 42, 43):
            raise MaynuoError("Unsupported load command.")
        self._write(0x0A00, struct.pack(">H", command))

    def _require_commission(self):
        if not self.commissioned:
            raise MaynuoError("Complete read-only load commissioning before enabling writes.")

    def probe(self) -> LoadIdentity:
        """Read-only identity and measurement probe, with stable identity check."""
        with self._lock:
            self._commissioned = False
            self._modes.clear()
            first = self.read()
            second = self._registers(0x0B06, 1) + self._registers(0x0B07, 1)
            identity = LoadIdentity(*struct.unpack(">HH", second))
            if identity != LoadIdentity(first.model_code, first.firmware_code):
                raise MaynuoError("Load identity changed during the read-only check.")
            self._identity = identity
            return identity

    def commission(self, identity: LoadIdentity, cc_mode_code: int, cv_mode_code: int) -> bool:
        """Local opt-in after matching panel readings; performs read operations only."""
        with self._lock:
            self._commissioned = False
            if self._identity is None or identity != self._identity:
                raise MaynuoError("Load identity must match this connection's read-only probe.")
            if any(type(value) is not int or not 0 <= value <= 0xFFFF for value in (cc_mode_code, cv_mode_code)):
                raise ValueError("Observed CC/CV mode codes must be 16-bit integers.")
            if cc_mode_code == cv_mode_code:
                raise ValueError("Observed CC and CV mode codes must be different.")
            reading = self.read()
            if identity != LoadIdentity(reading.model_code, reading.firmware_code):
                raise MaynuoError("Load identity no longer matches the commissioned device.")
            self._modes = {cc_mode_code: "CC", cv_mode_code: "CV"}
            self._commissioned = True
            return True

    def read(self) -> LoadReading:
        with self._lock:
            acquired_at = time.monotonic()
            # Small field reads follow the manual's two-word voltage example.
            # A short bulk reply is never accepted or padded with made-up data.
            voltage = struct.unpack(">f", self._registers(0x0B00, 2))[0]
            current = struct.unpack(">f", self._registers(0x0B02, 2))[0]
            mode_code = struct.unpack(">H", self._registers(0x0B04, 1))[0]
            model = struct.unpack(">H", self._registers(0x0B06, 1))[0]
            firmware = struct.unpack(">H", self._registers(0x0B07, 1))[0]
            try:
                _number(voltage, "Measured voltage")
                _number(current, "Measured current")
            except ValueError as exc:
                raise MaynuoError(str(exc)) from exc
            if self._identity is not None and LoadIdentity(model, firmware) != self._identity:
                self._commissioned = False
                raise MaynuoError("Load identity changed; writes are disabled.")
            input_on = bool(self._coils(0x0510, 1) & 1)
            faults = self._coils(0x0520, 8)
            remote = bool(self._coils(0x0500, 1) & 1)
            current_set = struct.unpack(">f", self._registers(0x0A01, 2))[0]
            voltage_set = struct.unpack(">f", self._registers(0x0A03, 2))[0]
            if any(not math.isfinite(value) or value < 0 for value in (current_set, voltage_set)):
                raise MaynuoError("Invalid load setpoint readback.")
            mode = self._modes.get(mode_code, "UNKNOWN")
            setpoint = current_set if mode == "CC" else voltage_set if mode == "CV" else None
            self._sequence += 1
            return LoadReading(voltage, current, voltage * current, mode_code, mode, setpoint,
                input_on, tuple(name for bit, name in enumerate(FAULTS) if name and faults & (1 << bit)),
                acquired_at, self._sequence, remote, model, firmware,
                unregulated=bool(faults & (1 << 5)))

    def enable_remote(self) -> bool:
        with self._lock:
            self._require_commission()
            self.read()  # verify identity before first write
            self._exchange(5, struct.pack(">HH", 0x0500, 0xFF00))
            if not self._coils(0x0500, 1) & 1:
                raise MaynuoError("Load did not confirm remote control.")
            return True

    def disable_remote(self) -> bool:
        with self._lock:
            self._require_commission()
            if self.read().input_on:
                raise MaynuoError("Turn load input OFF before returning to local control.")
            self._exchange(5, struct.pack(">HH", 0x0500, 0))
            if self._coils(0x0500, 1) & 1:
                raise MaynuoError("Load did not confirm local control.")
            return True

    def _limits(self, reading: LoadReading):
        if reading.voltage > self.max_voltage or reading.current > self.max_current or reading.power > self.max_power:
            raise MaynuoError("Measured load voltage, current or power exceeds bench limits.")

    def _setpoint(self, mode: str, value: float) -> float:
        value = _number(value, "Load setpoint")
        if mode not in ("CC", "CV"):
            raise ValueError("Only CC and CV load modes are supported.")
        limit = self.max_current if mode == "CC" else self.max_voltage
        if value > limit:
            raise ValueError("Load setpoint exceeds the bench limit.")
        return value

    @staticmethod
    def _matches(actual: float | None, requested: float) -> bool:
        return actual is not None and math.isclose(actual, requested, rel_tol=2e-6, abs_tol=1e-6)

    def configure(self, mode: str, setpoint: float) -> bool:
        with self._lock:
            self._require_commission()
            value = self._setpoint(mode, setpoint)
            before = self.read()
            if before.input_on or not before.remote:
                raise MaynuoError("Load must confirm remote control and input OFF before changing mode.")
            self._write(0x0A01 if mode == "CC" else 0x0A03, struct.pack(">f", value))
            self._command(1 if mode == "CC" else 2)
            after = self.read()
            if after.mode != mode or not self._matches(after.setpoint, value) or after.input_on or not after.remote:
                raise MaynuoError("Load mode/setpoint/input-state readback did not match; input was not enabled.")
            return True

    def set_input(self, enabled: bool) -> bool:
        with self._lock:
            if type(enabled) is not bool:
                raise ValueError("Load input must be True or False.")
            self._require_commission()
            if enabled:
                before = self.read()
                self._limits(before)
                if not before.remote or before.mode not in ("CC", "CV") or before.faults:
                    raise MaynuoError("Load is not ready for input ON.")
                self._setpoint(before.mode, before.setpoint)
            # OFF deliberately needs no prior telemetry, so it remains attemptable
            # after a measurement timeout. Echo and independent ISTATE still required.
            self._command(42 if enabled else 43)
            if bool(self._coils(0x0510, 1) & 1) != enabled:
                raise MaynuoError("Load input state did not confirm the requested change.")
            return True

    def set_load_current(self, current: float) -> bool:
        """Change the CC current of a running load; never toggle input.

        Bench evidence: with the input ON, writing IFIX alone is read back correctly
        but the running regulator keeps drawing the old current (the end-current
        sweep stayed at 5 A and failed at the 4.8 A step). The new IFIX is therefore
        applied the way the CV -> CC transition applies it: prepare and read back
        IFIX, then issue the documented CC mode command once, with the input state
        verified before and after. No input ON/OFF command is ever sent.
        """
        with self._lock:
            self._require_commission()
            value = self._setpoint("CC", current)
            before = self.read()
            self._limits(before)
            if before.mode != "CC" or not before.input_on or not before.remote or before.faults or before.unregulated:
                raise MaynuoError("Continuous-current adjustment requires healthy CC input ON.")
            self._write(0x0A01, struct.pack(">f", value))
            prepared = struct.unpack(">f", self._registers(0x0A01, 2))[0]
            if not self._matches(prepared, value):
                raise MaynuoError("Continuous-current adjustment was not confirmed.")
            if not self._coils(0x0510, 1) & 1:
                raise MaynuoError("Load input turned OFF while preparing the continuous-current adjustment.")
            self._command(1)
            after = self.read()
            if after.mode != "CC" or not after.input_on or not after.remote or not self._matches(after.setpoint, value):
                raise MaynuoError("Continuous-current adjustment was not confirmed after applying the CC command.")
            return True

    def transition_to_cc(self, current: float) -> bool:
        """CV -> CC without an input toggle; reject an observed interruption.

        IFIX is prepared and read back while CV is still active, then the
        documented CC mode command is issued once. A command acknowledgement
        alone does not establish that input remained ON. Readbacks cannot prove
        the absence of a shorter transient between serial measurements.
        """
        with self._lock:
            self._require_commission()
            value = self._setpoint("CC", current)
            if value <= 0:
                raise ValueError("Live CC transition requires a positive current.")
            before = self.read()
            self._limits(before)
            if (before.mode != "CV" or not before.input_on or not before.remote
                    or before.faults or before.unregulated or before.voltage <= 0 or before.current <= 0):
                raise MaynuoError("Live CC transition requires conducting, regulated CV input ON.")
            self._write(0x0A01, struct.pack(">f", value))
            prepared = struct.unpack(">f", self._registers(0x0A01, 2))[0]
            if not self._matches(prepared, value):
                raise MaynuoError("CC current was not confirmed before changing the active mode.")
            if not self._coils(0x0510, 1) & 1:
                raise MaynuoError("Load input turned OFF while preparing the live CC transition.")
            self._command(1)
            after = self.read()
            self._limits(after)
            if (after.mode != "CC" or not after.input_on or not after.remote
                    or not self._matches(after.setpoint, value) or after.faults):
                raise MaynuoError("Live CV to CC transition was not confirmed; no input restart was attempted.")
            if after.voltage <= 0 or after.current <= 0:
                raise MaynuoError("Output was lost during the live CV to CC transition; this is not a cutoff result.")
            return True
