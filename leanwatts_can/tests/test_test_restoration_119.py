"""Temporary charger parameter changes are retired; deterministic instruments, no hardware.

Releases up to 1.1.8 temporarily wrote startVoltage=0 for CAN-started tests and restored
it afterwards. From 1.1.9 tests start only by operator AC restart, so no parameter is ever
changed for a test and no restoration is required. These tests guard that boundary.
"""
import unittest

from leanwatts_can.bench_session import BenchSessionError
from leanwatts_can.protocol import encode_config
from leanwatts_can.tests import test_bench_session as fixtures


class RestorationRetiredTests(unittest.TestCase):
    setUp = fixtures.BenchSessionTests.setUp
    tearDown = fixtures.BenchSessionTests.tearDown
    make = fixtures.BenchSessionTests.make
    ready = fixtures.BenchSessionTests.ready
    sends = fixtures.BenchSessionTests.sends

    def test_parameter_modifying_test_methods_cannot_be_constructed(self):
        for options in (dict(test_start_method="normal"), dict(test_start_method="calibration_start_only"),
                        dict(post_test_can_start=True)):
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, "operator AC restart|retired"):
                    self.make(**options)
        self.assertEqual(self.station.calls, [])

    def test_external_restart_test_never_requires_restoration(self):
        bench = self.ready("test")
        self.assertEqual(bench.test_parameter_restore_result["status"], "NOT_REQUIRED")
        self.assertFalse(bench.test_parameter_restore_result["required"])

    def test_finishing_a_test_sends_no_restore_or_parameter_frame(self):
        bench = self.ready("test")
        bench.finish_test()
        self.assertEqual(self.sends(), [])
        self.assertNotIn(encode_config("startVoltage", 12), self.sends())
        self.assertEqual(bench.can_tx_count, 0)

    def test_parameter_write_is_blocked_during_tests(self):
        bench = self.ready("test")
        with self.assertRaises(BenchSessionError):
            bench._send("post_test_start", [encode_config("startVoltage", 0)])
        self.assertEqual(self.sends(), [])


if __name__ == "__main__":
    unittest.main()
