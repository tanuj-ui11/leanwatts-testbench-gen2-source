"""CAN send boundaries and trace capture; fake instruments only."""
from datetime import datetime
import unittest

from leanwatts_can.bench_session import BenchSessionError, TraceCaptureError
from leanwatts_can.protocol import command, encode_config, encode_duty
from leanwatts_can.tests import test_bench_session as fixtures


class DiagnosticBenchTests(unittest.TestCase):
    setUp = fixtures.BenchSessionTests.setUp
    tearDown = fixtures.BenchSessionTests.tearDown
    make = fixtures.BenchSessionTests.make
    ready = fixtures.BenchSessionTests.ready
    sends = fixtures.BenchSessionTests.sends

    def test_retired_methods_are_rejected_before_hardware(self):
        # CAN-start and temporary-parameter test methods were retired in 1.1.9;
        # tests now start only by operator AC restart with zero charger commands.
        for options in (dict(test_start_method="normal"), dict(test_start_method="calibration_start_only"),
                        dict(post_test_can_start=True)):
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, "operator AC restart|retired"):
                    self.make(**options)
        self.assertEqual(self.station.calls, [])

    def test_final_send_boundary_rejects_every_charger_frame_during_tests(self):
        bench = self.ready("test")
        forbidden = [encode_config("endVoltage", 56), encode_duty("voltage", 80),
                     encode_duty("current", 80), command("saveVoltage"), command("saveFullCurrent"),
                     command("saveHalfCurrent"), command("calibrationStart"), command("workStart"),
                     command("shutdown")]
        for frame in forbidden:
            with self.subTest(frame=frame):
                with self.assertRaises(BenchSessionError):
                    bench._send("post_test_start", [frame])
                with self.assertRaises(BenchSessionError):
                    bench._send("post_test_start", [command("calibrationStart"), frame])
        bench._started = bench._shutdown_accepted = True
        bench._applied_duties = {"current": 80, "voltage": 80}
        for method, args in ((bench.charger_start_calibration, (80, 80)), (bench.charger_apply_duties, (80, 80)),
                             (bench.charger_save, ("current",)), (bench.charger_shutdown, ())):
            with self.assertRaises(BenchSessionError):
                method(*args)
        self.assertEqual(self.sends(), [])
        self.assertEqual(bench.can_tx_count, 0)

    def test_trace_has_timestamped_tx_attempt_and_separate_result_with_rx_capture(self):
        captured = []
        bench = self.ready("calibration", trace_sink=captured.append)
        bench.trace_phase = "calibration_shutdown"
        self.assertTrue(bench.charger_shutdown())
        frame = {"id": 0x18FF50E5, "extended": True, "remote": False, "error": False,
                 "dlc": 8, "data": [2, 48, 0, 200, 0, 0, 0, 0], "timestamp_us": 123456}
        self.station.frames = [frame]
        bench.read(self.station.now)
        tx = [item for item in captured if item["direction"] == "TX"]
        results = [item for item in captured if item["direction"] == "TX_RESULT"]
        rx = [item for item in captured if item["direction"] == "RX"]
        self.assertEqual(len(tx), 1)
        self.assertEqual(tx[0]["data"], command("shutdown")["data"])
        self.assertEqual(tx[0]["phase"], "calibration_shutdown")
        self.assertEqual(results[0]["correlates_to"], tx[0]["event_id"])
        self.assertIn("API ACCEPTED", results[0]["outcome"])
        self.assertEqual(tx[0]["capture_id"], results[0]["capture_id"])
        self.assertEqual(len(rx), 1)
        self.assertEqual(rx[0]["data"], frame["data"])
        self.assertEqual(rx[0]["timestamp_us"], 123456)
        self.assertIsNotNone(datetime.fromisoformat(rx[0]["timestamp"]).tzinfo)
        self.assertLessEqual(rx[0]["monotonic"], self.station.now)
        self.assertEqual(len(bench.can_trace), 1)  # Existing TX-only API is preserved.

    def test_two_stage_sessions_have_distinct_capture_ids_for_repeated_event_numbers(self):
        captures = []
        identifiers = []
        for _ in range(2):
            bench = self.ready(trace_sink=captures.append)
            bench.charger_shutdown()
            identifiers.append(bench.capture_id)
            bench.release()
        self.assertNotEqual(*identifiers)
        events = {(item["capture_id"], item["event_id"]): item for item in captures}
        self.assertEqual(len(events), len(captures))
        for item in captures:
            if item["direction"] == "TX_RESULT":
                attempt = events[(item["capture_id"], item["correlates_to"])]
                self.assertEqual(attempt["direction"], "TX")
                self.assertEqual(attempt["data"], item["data"])

    def test_parameter_backlog_frames_are_captured_even_when_not_used_for_fresh_readback(self):
        bench = self.ready()
        bench.can_observations["ratedVoltage"] = 48
        frame = {"id": 0x18FF53E5, "extended": True, "remote": False, "error": False,
                 "dlc": 8, "data": [0, 1, 1, 224, 1, 224, 0, 60], "timestamp_us": 9}
        self.station.frames = [frame]
        bench._drain_configuration()
        rx = [item for item in bench.trace_events if item["direction"] == "RX"]
        self.assertEqual(len(rx), 1)
        self.assertEqual(rx[0]["action"], "parameter_backlog_drain")
        self.assertEqual(rx[0]["data"], frame["data"])
        self.assertNotIn("ratedVoltage", bench.can_observations)
        self.assertEqual(self.sends(), [])

    def test_trace_failure_before_write_blocks_normal_command_but_not_emergency_shutdown(self):
        bench = self.ready()
        def broken(_):
            raise OSError("disk full")
        bench.trace_sink = broken
        with self.assertRaises(TraceCaptureError):
            bench._send("baseline_start", [command("workStart")])
        self.assertEqual(self.sends(), [])
        self.assertEqual(bench.can_tx_count, 0)
        self.assertFalse(bench.trace_capture_complete)
        self.assertIn("disk full", bench.trace_capture_error)
        self.assertTrue(bench.charger_shutdown())
        self.assertEqual(self.sends(), [command("shutdown")])
        self.assertEqual(bench.can_accepted_count, 1)

    def test_receive_capture_failure_does_not_close_working_can_before_abort_shutdown(self):
        bench = self.ready()
        def broken(_):
            raise OSError("storage lost")
        bench.trace_sink = broken
        self.station.frames = [{"id": 0x123, "extended": False, "remote": False,
                                "dlc": 1, "data": [0], "timestamp_us": 1}]
        with self.assertRaises(TraceCaptureError):
            bench.read(self.station.now)
        self.assertFalse(bench._can_failed)
        self.assertTrue(bench.charger_shutdown())
        self.assertEqual(self.sends(), [command("shutdown")])

    def test_send_error_is_recorded_as_unconfirmed_and_never_retried(self):
        captured = []
        bench = self.ready("calibration", trace_sink=captured.append)
        self.station.fail_send = True
        with self.assertRaisesRegex(RuntimeError, "SDK send failed"):
            bench.charger_shutdown()
        self.assertEqual(self.sends(), [command("shutdown")])
        result = [item for item in captured if item["direction"] == "TX_RESULT"][-1]
        self.assertIn("ERROR; outcome unconfirmed", result["outcome"])
        self.assertEqual(bench.can_accepted_count, 0)
        self.station.fail_send = False
        with self.assertRaises(BenchSessionError):
            bench.charger_shutdown()
        self.assertEqual(self.sends(), [command("shutdown")])

if __name__ == "__main__":
    unittest.main()
