"""Offline golden vectors recovered from vendor IL; no hardware or app imports."""

import math
import unittest

from leanwatts_can import protocol as p


def rx(frame_id, data):
    return {"id": frame_id, "extended": True, "remote": False, "dlc": 8, "data": data}


READBACKS = [
    rx(0x18FF53E5, [0, 1, 1, 224, 1, 144, 0, 75]),
    rx(0x18FF54E5, [0, 180, 2, 72, 0, 150, 0, 50]),
    rx(0x18FF55E5, [0, 100, 0, 10, 1, 1, 2, 38]),
    rx(0x18FF56E5, [2, 88, 1, 194, 0, 200, 0, 5]),
]
EXPECTED = {
    "batteryType": 1, "ratedVoltage": 48, "startVoltage": 40, "temperatureLimit": 75,
    "maxChargeMinutes": 180, "endVoltage": 58.4, "ccCurrent": 15, "derating110": 50,
    "derating220": 100, "endCurrent": 1, "blindCharge": 1, "rechargeVoltage": 55,
    "adjustCurrentMin": .5, "adjustCurrentMax": 20, "adjustVoltageMin": 45, "adjustVoltageMax": 60,
}


class ProtocolTests(unittest.TestCase):
    def test_golden_action_frames_and_exact_dlc(self):
        vectors = {
            "workStart": [0, 20, 0, 0], "shutdown": [0, 20, 0, 128],
            "calibrationStart": [0, 20, 0, 1], "saveVoltage": [0, 23],
            "saveFullCurrent": [0, 24], "saveHalfCurrent": [0, 25],
        }
        for name, data in vectors.items():
            with self.subTest(name=name):
                self.assertEqual(p.command(name), {"id": 0x1806E550, "extended": True, "remote": False, "dlc": len(data), "data": data})

    def test_golden_duty_config_and_poll_vectors(self):
        vectors = [
            (p.encode_duty("voltage", 80), [0, 21, 3, 32]),
            (p.encode_duty("current", 80.1), [0, 22, 3, 33]),
            (p.encode_config("ratedVoltage", 48), [0, 2, 1, 224]),
            (p.encode_config("ccCurrent", 15), [0, 7, 0, 150]),
            (p.encode_config("endVoltage", 58.4), [0, 6, 2, 72]),
            (p.encode_config("endCurrent", 1), [0, 10, 0, 10]),
            (p.encode_config("blindCharge", 1), [0, 12, 0, 1]),
            (p.poll(1), [0, 11, 0, 1]), (p.poll(6), [0, 11, 0, 6]),
        ]
        for actual, expected in vectors:
            with self.subTest(expected=expected):
                self.assertEqual(actual["data"], expected)

    def test_all_sixteen_readbacks_and_blind_charge_byte(self):
        merged = {}
        for frame in READBACKS:
            decoded = p.decode(frame)
            self.assertEqual(decoded["kind"], "configurationReadback")
            merged.update(decoded["values"])
        self.assertEqual(merged, EXPECTED)

    def test_telemetry_signed_temperature_and_raw_faults(self):
        vectors = [
            (rx(0x18FF50E5, [2, 72, 0, 150, 4, 0, 0, 0]), {"outputVoltage": 58.4, "outputCurrent": 15, "statusByte": 4}),
            (rx(0x18FF51E5, [0, 230, 1, 144, 3, 232, 0, 30]), {"inputVoltage": 230, "pfcVoltage": 400, "maxPower": 1000, "workMinutes": 30}),
            (rx(0x18FF52E5, [0, 75, 255, 246, 2, 4, 0, 0]), {"protectionTemperature": 75, "ambientTemperature": -10, "faultWord": 516}),
        ]
        for frame, values in vectors:
            with self.subTest(frame_id=frame["id"]):
                self.assertEqual(p.decode(frame)["values"], values)

    def test_output_unsigned_but_config_signed(self):
        self.assertEqual(p.decode(rx(0x18FF50E5, [255, 255, 128, 0, 0, 0, 0, 0]))["values"]["outputVoltage"], 6553.5)
        self.assertEqual(p.decode(rx(0x18FF53E5, [255, 255, 128, 0, 0, 0, 0, 0]))["values"]["ratedVoltage"], -3276.8)

    def test_no_invented_save_acknowledgement(self):
        self.assertEqual(p.decode(rx(0x1806E550, [0, 23, 0, 0, 0, 0, 0, 0]))["kind"], "unknown")
        with self.assertRaises(ValueError):
            p.decode(p.command("saveVoltage"))

    def test_reject_invalid_configuration_numbers(self):
        for value in [math.nan, math.inf, -math.inf, -1, 1.23, "15", None, True, 10 ** 1000]:
            with self.subTest(value=str(value)[:30]), self.assertRaises(ValueError):
                p.encode_config("ccCurrent", value)

    def test_reject_unknown_or_unrepresentable_actions(self):
        calls = [lambda: p.encode_duty("current", 1000.1), lambda: p.encode_duty("load", 80),
                 lambda: p.encode_config("bogus", 1), lambda: p.encode_config("blindCharge", 256),
                 lambda: p.encode_config("ratedVoltage", 3276.8), lambda: p.poll(0), lambda: p.poll(7),
                 lambda: p.poll(True), lambda: p.poll(1.0), lambda: p.command("saveEverything")]
        for call in calls:
            with self.assertRaises(ValueError):
                call()

    def test_reject_malformed_receive_frames(self):
        base = rx(0x18FF53E5, [0] * 8)
        for change in [{"extended": False}, {"extended": 1}, {"remote": True}, {"remote": None},
                       {"dlc": 7}, {"id": -1}, {"id": 0x20000000}, {"id": True},
                       {"data": [0] * 7}, {"data": [0] * 7 + [256]},
                       {"data": [0] * 7 + [True]}, {"data": "00000000"}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                p.decode({**base, **change})

    def test_complete_write_sequence_matches_original_timer(self):
        frames = p.encode_all_config(EXPECTED)
        self.assertEqual(len(frames), 16)
        self.assertEqual([f["data"][1] for f in frames], [16, 17, 14, 15, 13, 12, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10])

    def test_complete_config_requires_all_values_and_rejects_extras(self):
        for name in EXPECTED:
            incomplete = dict(EXPECTED)
            del incomplete[name]
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, name):
                p.encode_all_config(incomplete)
        with self.assertRaisesRegex(ValueError, "ccCurrent"):
            p.encode_all_config({**EXPECTED, "ccCurrent": None})
        with self.assertRaisesRegex(ValueError, "unexpected"):
            p.encode_all_config({**EXPECTED, "unexpected": 1})
        with self.assertRaises(ValueError):
            p.encode_all_config(None)

    def test_offline_verify_complete_missing_and_mismatch(self):
        result = p.verify(EXPECTED, READBACKS)
        self.assertTrue(result["matched"])
        self.assertIn("freshness", result["scope"])
        result = p.verify(EXPECTED, READBACKS[:-1])
        self.assertFalse(result["matched"])
        self.assertEqual(set(result["missing"]), {"adjustCurrentMin", "adjustCurrentMax", "adjustVoltageMin", "adjustVoltageMax"})
        result = p.verify({**EXPECTED, "ccCurrent": 16}, READBACKS)
        self.assertEqual(result["mismatches"], {"ccCurrent": {"expected": 16, "actual": 15}})

    def test_offline_verify_latest_group_wins_without_claiming_freshness(self):
        corrected = rx(0x18FF54E5, [0, 180, 2, 72, 0, 160, 0, 50])
        self.assertTrue(p.verify({**EXPECTED, "ccCurrent": 16}, [*READBACKS, corrected])["matched"])
        self.assertFalse(p.verify(EXPECTED, [rx(0x1806E550, [0, 23, 0, 0, 0, 0, 0, 0])])["matched"])


if __name__ == "__main__":
    unittest.main()
