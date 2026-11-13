"""Strict response evidence and documented small reads, with no hardware I/O."""
import struct
import unittest
from dataclasses import asdict

from leanwatts_can.maynuo import MaynuoError, MaynuoLoad, SerialSettings, frame


class ReplySerial:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []
        self.pending = b""
        self.timeout = 0.05
        self.closed = False

    def reset_input_buffer(self):
        self.pending = b""

    def write(self, request):
        self.requests.append(bytes(request))
        self.pending = self.reply(request) if callable(self.reply) else self.reply
        return len(request)

    def read(self, length):
        # Fragmentation ensures evidence includes every byte, not only the last
        # read() call or the successfully decoded parts of a failed response.
        length = min(length, 2)
        chunk, self.pending = self.pending[:length], self.pending[length:]
        return chunk

    def close(self):
        self.closed = True


class MaynuoDiagnosticTests(unittest.TestCase):
    def load(self, reply):
        settings = SerialSettings("COM11", baudrate=9600, address=1, parity="N", timeout=0.05)
        transport = ReplySerial(reply)
        load = MaynuoLoad(settings, 60, 25, 1500, serial_factory=lambda **kwargs: transport)
        load.open()
        self.addCleanup(load.close)
        return load, transport

    def test_literal_manufacturer_voltage_vector_and_success_evidence(self):
        # 2009 English manual printed p.34, supplied by the user. The displayed
        # '10V' is rounded; preserve the exact four-byte float from its example.
        request = bytes.fromhex("01 03 0B 00 00 02 C6 2F")
        response = bytes.fromhex("01 03 04 41 20 00 2A 6E 1A")
        load, serial = self.load(response)
        self.assertEqual(load._registers(0x0B00, 2), bytes.fromhex("41 20 00 2A"))
        self.assertEqual(serial.requests, [request])
        diagnostic = load.diagnostics()
        self.assertEqual(diagnostic["serial_settings"], asdict(load.settings))
        event = diagnostic["transactions"][-1]
        self.assertEqual(event["tx_hex"], request.hex(" ").upper())
        self.assertEqual(event["rx_hex"], response.hex(" ").upper())
        self.assertEqual(event["expected_byte_count"], 4)
        self.assertEqual(event["received_byte_count"], 4)
        self.assertTrue(event["crc_valid"])
        self.assertIsNone(event["error"])

    def test_crc_valid_short_response_is_rejected_without_retry_or_reinterpretation(self):
        response = frame(bytes.fromhex("01 03 02 41 20"))
        load, serial = self.load(response)
        with self.assertRaises(MaynuoError) as raised:
            load._registers(0x0B00, 2)
        self.assertIn("count", str(raised.exception).lower())
        self.assertEqual(len(serial.requests), 1)
        event = raised.exception.diagnostics["transactions"][-1]
        self.assertEqual(event["expected_byte_count"], 4)
        self.assertEqual(event["received_byte_count"], 2)
        self.assertEqual(event["rx_hex"], response.hex(" ").upper())
        self.assertTrue(event["crc_valid"])
        self.assertTrue(event["error"])

    def test_exact_request_echo_is_rejected_with_complete_raw_evidence(self):
        load, serial = self.load(lambda request: request)
        with self.assertRaises(MaynuoError) as raised:
            load._registers(0x0B00, 2)
        self.assertIn("echo", str(raised.exception).lower())
        self.assertEqual(len(serial.requests), 1)
        event = raised.exception.diagnostics["transactions"][-1]
        self.assertEqual(event["tx_hex"], "01 03 0B 00 00 02 C6 2F")
        self.assertEqual(event["rx_hex"], event["tx_hex"])
        self.assertEqual(event["expected_byte_count"], 4)
        self.assertTrue(event["error"])

    def test_partial_timeout_retains_all_received_bytes(self):
        partial = bytes.fromhex("01 03 04 41 20")
        load, serial = self.load(partial)
        with self.assertRaises(MaynuoError) as raised:
            load._registers(0x0B00, 2)
        self.assertIn("timed out", str(raised.exception).lower())
        self.assertEqual(len(serial.requests), 1)
        event = raised.exception.diagnostics["transactions"][-1]
        self.assertEqual(event["rx_hex"], "01 03 04 41 20")
        self.assertEqual(event["expected_byte_count"], 4)
        self.assertEqual(event["received_byte_count"], 4)
        self.assertIsNone(event["crc_valid"])
        self.assertTrue(event["error"])

    def test_coil_request_echo_is_rejected_and_fully_captured(self):
        load, serial = self.load(lambda request: request)
        with self.assertRaisesRegex(MaynuoError, "echo") as raised:
            load._coils(0x0510, 1)
        event = raised.exception.diagnostics["transactions"][-1]
        self.assertEqual(event["rx_hex"], "01 01 05 10 00 01 FC C3")
        self.assertTrue(event["request_echo"])
        self.assertEqual(len(serial.requests), 1)

    def test_bad_crc_diagnostic_does_not_label_frame_valid(self):
        response = bytes.fromhex("01 03 04 41 20 00 2A 6E 00")
        load, serial = self.load(response)
        with self.assertRaises(MaynuoError) as raised:
            load._registers(0x0B00, 2)
        self.assertIn("CRC", str(raised.exception))
        event = raised.exception.diagnostics["transactions"][-1]
        self.assertEqual(event["rx_hex"], response.hex(" ").upper())
        self.assertIs(event["crc_valid"], False)

    def test_probe_uses_documented_scalar_reads_and_no_writes(self):
        values = {
            0x0B00: struct.pack(">f", 48.0), 0x0B02: struct.pack(">f", 1.25),
            0x0B04: struct.pack(">H", 7), 0x0B05: struct.pack(">H", 0),
            0x0B06: struct.pack(">H", 8437), 0x0B07: struct.pack(">H", 9),
            0x0A01: struct.pack(">f", 1.0), 0x0A03: struct.pack(">f", 48.0),
        }

        def reply(request):
            address, function = request[:2]
            register, count = struct.unpack(">HH", request[2:6])
            if function == 3:
                self.assertIn(register, values)
                payload = values[register]
                self.assertEqual(count * 2, len(payload), "Mixed/bulk register reads are not used for commissioning")
            elif function == 1:
                self.assertIn(register, (0x0500, 0x0510, 0x0520))
                payload = b"\x00"
            else:
                self.fail("Read-only probe attempted a write")
            return frame(bytes((address, function, len(payload))) + payload)

        load, serial = self.load(reply)
        identity = load.probe()
        reading = load.read()
        self.assertEqual((identity.model_code, identity.firmware_code), (8437, 9))
        self.assertEqual((reading.voltage, reading.current, reading.power), (48, 1.25, 60))
        addresses = {struct.unpack(">H", r[2:4])[0] for r in serial.requests if r[1] == 3}
        self.assertTrue({0x0B00, 0x0B02, 0x0B04, 0x0B06, 0x0B07, 0x0A01, 0x0A03}.issubset(addresses))
        self.assertTrue(all(r[1] in (1, 3) for r in serial.requests))


if __name__ == "__main__":
    unittest.main()
