"""Published-frame vectors and a stateful fake instrument; no serial hardware."""
import struct
import unittest
from unittest.mock import patch

from leanwatts_can.maynuo import (
    LoadIdentity, MaynuoError, MaynuoLoad, SerialSettings, crc16, frame,
)


class FakeSerial:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.timeout = kwargs.get("timeout", 0.05)
        self.buffer = b""
        self.requests = []
        self.closed = False
        self.mode = 7  # deliberately not CMD=1; proves commissioned enum mapping
        self.cc_code, self.cv_code = 7, 11
        self.remote = self.input_on = False
        self.current_set, self.voltage_set = 1.0, 48.0
        self.voltage, self.current = 48.0, 0.0
        self.model, self.firmware, self.faults = 8437, 9, 0
        self.transform = None
        self.ignore_write = False
        self.fragment = 100

    def reset_input_buffer(self):
        self.buffer = b""

    def write(self, request):
        self.requests.append(request)
        address, function = request[:2]
        register, count = struct.unpack(">HH", request[2:6])
        if function == 3:
            full = struct.pack(">ffHHHH", self.voltage, self.current, self.mode, int(self.input_on), self.model, self.firmware)
            if 0x0B00 <= register <= 0x0B07:
                offset = (register - 0x0B00) * 2
                payload = full[offset:offset + count * 2]
            elif 0x0A01 <= register <= 0x0A04:
                offset = (register - 0x0A01) * 2
                payload = struct.pack(">ff", self.current_set, self.voltage_set)[offset:offset + count * 2]
            else:
                raise AssertionError(f"Unexpected register read {register:x}")
            assert len(payload) == count * 2
            response = frame(bytes((address, function, len(payload))) + payload)
        elif function == 1:
            values = {0x0500: int(self.remote), 0x0510: int(self.input_on), 0x0520: self.faults}
            response = frame(bytes((address, function, 1, values[register])))
        elif function == 5:
            assert register == 0x0500
            if not self.ignore_write:
                self.remote = count == 0xFF00
            response = request
        elif function == 16:
            if not self.ignore_write:
                if register == 0x0A00:
                    command = struct.unpack(">H", request[7:9])[0]
                    if command in (1, 2):
                        self.mode = self.cc_code if command == 1 else self.cv_code
                    elif command in (42, 43):
                        self.input_on = command == 42
                    else:
                        raise AssertionError(f"Unexpected command {command}")
                elif register == 0x0A01:
                    self.current_set = struct.unpack(">f", request[7:11])[0]
                elif register == 0x0A03:
                    self.voltage_set = struct.unpack(">f", request[7:11])[0]
                else:
                    raise AssertionError(f"Unexpected register write {register:x}")
            response = frame(request[:6])
        else:
            raise AssertionError(f"Unexpected function {function}")
        self.buffer = self.transform(response) if self.transform else response
        return len(request)

    def read(self, count):
        count = min(count, self.fragment)
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def close(self):
        self.closed = True


class MaynuoTests(unittest.TestCase):
    def setUp(self):
        self.serial = FakeSerial()
        self.load = MaynuoLoad(SerialSettings("COM-test", timeout=0.05), 60, 25, 1500,
                               serial_factory=lambda **kwargs: self.serial)

    def tearDown(self):
        self.load.close()

    def ready(self):
        self.load.open()
        identity = self.load.probe()
        self.load.commission(identity, 7, 11)
        return identity

    def writes(self):
        return [r for r in self.serial.requests if r[1] in (5, 16)]

    def test_published_request_vectors(self):
        # Verbatim byte vectors from 2009 manual, independently checked 2021.
        self.assertEqual(frame(bytes.fromhex("01 01 05 10 00 01")).hex(), "010105100001fcc3")
        self.assertEqual(frame(bytes.fromhex("01 05 05 00 FF 00")).hex(), "01050500ff008cf6")
        self.assertEqual(frame(bytes.fromhex("01 03 0B 00 00 04")).hex(), "01030b000004462d")
        self.assertEqual(frame(bytes.fromhex("01 10 0A 01 00 02 04 40 13 33 33")).hex(), "01100a0100020440133333fc23")
        self.assertEqual(struct.unpack(">f", bytes.fromhex("41 A0 01 61"))[0], 20.000673294067383)

    def test_construct_open_probe_commission_close_do_not_write(self):
        self.assertEqual(self.serial.requests, [])
        identity = self.ready()
        self.assertEqual(identity, LoadIdentity(8437, 9))
        reading = self.load.read()
        self.assertEqual((reading.mode, reading.setpoint), ("CC", 1.0))
        self.load.close()
        self.assertFalse(self.load.commissioned)
        self.assertEqual(self.writes(), [])

    def test_read_before_commission_reports_unknown_without_guessing(self):
        self.load.open()
        self.serial.current = 2.0
        first, second = self.load.read(), self.load.read()
        self.assertEqual((first.voltage, first.current, first.power), (48.0, 2.0, 96.0))
        self.assertEqual((first.mode, first.mode_code, first.setpoint), ("UNKNOWN", 7, None))
        self.assertIn("calculated", first.power_source)
        self.assertGreater(second.sequence, first.sequence)
        self.assertGreater(second.acquired_at, first.acquired_at)

    def test_write_actions_require_commission_and_distinct_codes(self):
        self.load.open()
        with self.assertRaises(MaynuoError):
            self.load.enable_remote()
        with self.assertRaises(MaynuoError):
            self.load.commission(LoadIdentity(8437, 9), 7, 11)
        identity = self.load.probe()
        with self.assertRaises(ValueError):
            self.load.commission(identity, 7, 7)
        with self.assertRaises(MaynuoError):
            self.load.set_input(True)
        self.assertEqual(self.writes(), [])

    def test_remote_configure_cc_cv_input_and_local_confirmed(self):
        self.ready()
        self.assertTrue(self.load.enable_remote())
        self.assertTrue(self.load.configure("CV", 48.5))
        self.assertEqual(self.load.read().mode_code, 11)
        self.assertTrue(self.load.set_input(True))
        with self.assertRaises(MaynuoError):
            self.load.configure("CC", 1.0)
        self.assertTrue(self.load.set_input(False))
        self.assertTrue(self.load.configure("CC", 1.0))
        self.assertTrue(self.load.disable_remote())
        self.assertFalse(self.serial.remote)
        for request in self.writes():
            self.assertIn(struct.unpack(">H", request[2:4])[0], (0x0500, 0x0A00, 0x0A01, 0x0A03))

    def test_ack_without_remote_readback_is_failure(self):
        self.ready()
        self.serial.ignore_write = True
        with self.assertRaisesRegex(MaynuoError, "remote control"):
            self.load.enable_remote()

    def test_ack_without_setpoint_and_mode_readback_is_failure(self):
        self.ready()
        self.load.enable_remote()
        self.serial.ignore_write = True
        with self.assertRaisesRegex(MaynuoError, "readback"):
            self.load.configure("CV", 51.0)
        self.assertFalse(self.serial.input_on)

    def test_wrong_identity_disarms_writes(self):
        self.ready()
        self.serial.model = 9999
        with self.assertRaisesRegex(MaynuoError, "identity"):
            self.load.read()
        self.assertFalse(self.load.commissioned)
        with self.assertRaises(MaynuoError):
            self.load.set_input(True)

    def test_crc_unit_function_byte_count_and_exception_rejected(self):
        self.load.open()
        transforms = (
            lambda r: r[:-1] + bytes((r[-1] ^ 1,)),
            lambda r: frame(bytes((2,)) + r[1:-2]),
            lambda r: frame(r[:1] + bytes((4,)) + r[2:-2]),
            lambda r: r[:2] + bytes((2,)) + r[3:],
            lambda r: frame(bytes((1, 0x83, 2))),
        )
        for transform in transforms:
            with self.subTest(transform=transform):
                self.serial.transform = transform
                with self.assertRaises(MaynuoError):
                    self.load.read()

    def test_no_response_times_out_without_retry(self):
        self.load.open()
        self.serial.transform = lambda r: b""
        with self.assertRaisesRegex(MaynuoError, "timed out"):
            self.load.read()
        self.assertEqual(len(self.serial.requests), 1)

    def test_fragmented_valid_response_supported(self):
        self.serial.fragment = 1
        self.ready()
        self.assertEqual(self.load.read().voltage, 48.0)

    def test_limits_faults_and_invalid_values_block_on(self):
        self.ready()
        self.load.enable_remote()
        self.serial.voltage = 61.0
        with self.assertRaisesRegex(MaynuoError, "limits"):
            self.load.set_input(True)
        self.serial.voltage = 48.0
        self.serial.faults = 1 << 3
        with self.assertRaisesRegex(MaynuoError, "not ready"):
            self.load.set_input(True)
        self.assertEqual(self.load.read().faults, ("Over-temperature",))
        self.serial.voltage = float("nan")
        with self.assertRaises(MaynuoError):
            self.load.read()
        self.assertFalse(self.serial.input_on)

    def test_setpoint_bounds_block_before_writes(self):
        self.ready()
        self.load.enable_remote()
        prior = len(self.writes())
        for mode, value in (("CV", 61), ("CC", 26), ("CC", -1), ("CC", float("nan")), ("CR", 1)):
            with self.subTest(mode=mode, value=value), self.assertRaises(ValueError):
                self.load.configure(mode, value)
        self.assertEqual(len(self.writes()), prior)

    def test_unreg_is_preserved_separately_and_does_not_hide_protection(self):
        self.ready()
        self.serial.faults = (1 << 5) | (1 << 3)
        reading = self.load.read()
        self.assertTrue(reading.unregulated)
        self.assertEqual(reading.faults, ("Over-temperature",))
        self.load.enable_remote()
        with self.assertRaisesRegex(MaynuoError, "not ready"):
            self.load.set_input(True)
        self.assertFalse(self.serial.input_on)

    def test_unregulated_idle_load_can_start_but_cannot_take_cc_ramp_steps(self):
        self.ready()
        self.load.enable_remote()
        self.serial.faults = 1 << 5
        self.assertTrue(self.load.read().unregulated)
        self.assertEqual(self.load.read().faults, ())
        self.load.configure("CC", 2.0)
        self.assertTrue(self.load.set_input(True))
        prior = len(self.writes())
        with self.assertRaisesRegex(MaynuoError, "healthy CC"):
            self.load.set_load_current(1.5)
        self.assertEqual(len(self.writes()), prior)
        self.assertTrue(self.load.set_input(False))

    def test_continuous_cc_write_never_sends_input_toggle(self):
        self.ready()
        self.load.enable_remote()
        self.load.configure("CC", 2.0)
        self.load.set_input(True)
        prior = len(self.writes())
        self.assertTrue(self.load.set_load_current(1.5))
        new = self.writes()[prior:]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0][2:4], bytes.fromhex("0A 01"))
        self.assertTrue(self.serial.input_on)

    def test_serial_validation_and_lazy_import(self):
        for settings in (SerialSettings(""), SerialSettings("X", address=0), SerialSettings("X", parity="Q"),
                         SerialSettings("X", baudrate=123), SerialSettings("X", timeout=100)):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                MaynuoLoad(settings, 60, 25, 1500)
        with patch("builtins.__import__", side_effect=AssertionError("No imports needed for construction")):
            load = MaynuoLoad(SerialSettings("X"), 60, 25, 1500)
            self.assertFalse(load.commissioned)


if __name__ == "__main__":
    unittest.main()
