"""External AC restart tests use no charger commands and preserve load continuity."""
import struct
import unittest
from unittest.mock import patch

from leanwatts_can.automation import AutomationEngine, AutomationSettings
from leanwatts_can.bench_session import BenchSession, BenchSessionError
from leanwatts_can.maynuo import MaynuoError, MaynuoLoad, SerialSettings
from leanwatts_can.protocol import command, encode_config, poll
from leanwatts_can.tests.test_bench_session import FakeCan, FakeEvent, FakeLoad, Station
from leanwatts_can.tests.test_maynuo import FakeSerial
from leanwatts_can.tests.test_production_sessions import production_recipe


class LiveLoad(FakeLoad):
    def transition_to_cc(self, current):
        self.station.calls.append(("load.transition", current))
        self.station.mode, self.station.setpoint = "CC", current
        if getattr(self.station, "interrupt_transition", False):
            self.station.input_on = False
        return True


class ExternalBenchTests(unittest.TestCase):
    def setUp(self):
        self.station = Station()
        self.bench = BenchSession(lambda: LiveLoad(self.station),
            monitor_factory=lambda: FakeCan(self.station), settings=AutomationSettings.demo(),
            protocol_approved=True, cc_mode_code=10, cv_mode_code=20,
            standalone_ready=True, exclusive_confirmed=True, termination_ramp_approved=True,
            cancellation_event=FakeEvent(), clock=lambda: self.station.now)
        self.bench.probe()
        self.bench.acquire("test")

    def tearDown(self):
        if self.bench._owned:
            try:
                self.bench.release()
            except BenchSessionError:
                self.assertEqual(self.bench.test_parameter_restore_result["status"], "HOLD")

    def test_ack_enabled_but_every_charger_command_is_blocked(self):
        self.assertTrue(self.bench.enter_passive())
        self.assertEqual(self.bench._can.state, "active")
        self.assertFalse(self.bench._can.listen_only)
        for frame in (command("workStart"), command("shutdown"), command("calibrationStart"),
                      encode_config("startVoltage", 0), encode_config("startVoltage", 12), poll(1)):
            with self.assertRaisesRegex(BenchSessionError, "commands are disabled"):
                self.bench._send("attempt", [frame], allow_abort=True)
        with self.assertRaises(BenchSessionError):
            self.bench.begin_post_test()
        self.assertEqual(self.bench.can_tx_count, 0)
        self.assertFalse(any(call[0] == "can.send" for call in self.station.calls))

    def conducting_cv(self):
        self.station.mode, self.station.setpoint = "CV", 49
        self.station.voltage, self.station.current = 49, 20
        self.station.input_on = True
        self.bench._load_configuration = ("CV", 49)

    def test_live_transition_sends_no_input_toggle_or_charger_command(self):
        self.conducting_cv()
        self.station.calls.clear()
        self.assertTrue(self.bench.transition_load_to_cc(5))
        self.assertEqual(self.bench._load_configuration, ("CC", 5))
        self.assertTrue(self.station.input_on)
        self.assertFalse(any(c[0] in ("load.input", "can.send", "load.configure") for c in self.station.calls))
        self.assertEqual(self.bench.load_transition_events[-1]["status"], "READBACK_CONFIRMED")

    def test_observed_transition_gap_cannot_be_accepted(self):
        self.conducting_cv()
        self.station.interrupt_transition = True
        with self.assertRaisesRegex(BenchSessionError, "interrupted"):
            self.bench.transition_load_to_cc(5)
        self.assertEqual(self.bench.load_transition_events[-1]["status"], "FAILED")
        self.assertFalse(any(c[0] == "load.input" for c in self.station.calls))

    def test_stop_cleanup_turns_load_off_without_restoration_or_charger_shutdown(self):
        self.conducting_cv()
        self.bench.request_abort()
        self.assertTrue(self.bench.finish_test())
        result = self.bench.test_parameter_restore_result
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["parameters_changed"])
        self.assertFalse(result["restoration_required"])
        self.assertIsNone(result["restored"])
        self.assertTrue(result["load_off_confirmed"])
        self.assertEqual(self.bench.can_tx_count, 0)
        self.assertTrue(self.bench.finish_test())
        self.assertEqual([c for c in self.station.calls if c == ("load.input", False)], [("load.input", False)])

    def test_unconfirmed_load_off_is_hold_and_never_sends_can(self):
        self.conducting_cv()
        self.station.ignore_input = True
        self.assertFalse(self.bench.finish_test())
        self.assertEqual(self.bench.test_parameter_restore_result["status"], "HOLD")
        self.assertEqual(self.bench.can_tx_count, 0)
        with self.assertRaisesRegex(BenchSessionError, "shutdown is unconfirmed"):
            self.bench.release()

    def test_retired_parameter_modifying_test_methods_rejected(self):
        for options in ({"test_start_method": "normal"}, {"test_start_method": "calibration_start_only"},
                        {"post_test_can_start": True}):
            with self.assertRaises(ValueError):
                BenchSession(lambda: LiveLoad(self.station), **options)


class RealAdapterExternalFlowTests(unittest.TestCase):
    def run_flow(self, *, cutoff=True, current_ramp_seconds=17):
        station = Station()
        recipe = production_recipe()
        station.voltage = station.current = 0
        station.limits = (recipe.max_voltage, recipe.max_current, recipe.max_power)
        start = station.now
        engaged = [None]
        timeline = []
        engine_ref = []

        class ObservedLoad(LiveLoad):
            def set_input(self, enabled):
                timeline.append((station.now, "input", enabled,
                    [item["name"] for item in engine_ref[0].results] if engine_ref else []))
                return super().set_input(enabled)

            def transition_to_cc(self, current):
                timeline.append((station.now, "transition", current))
                return super().transition_to_cc(current)

            def set_load_current(self, current):
                timeline.append((station.now, "ramp", current))
                return super().set_load_current(current)

        def sample():
            if station.now < start + 1:
                station.voltage = station.current = 0
            elif not station.input_on:
                # A temporary early plateau above49V is not settled OCV.
                station.voltage = 50 if station.now < start + 4 else min(
                    recipe.end_voltage, 50 + (station.now-start-4)*4)
                station.current = 0
            elif station.mode == "CV":
                if engaged[0] is None:
                    engaged[0] = station.now
                station.voltage = recipe.test_cv_voltage
                station.current = min(recipe.cc_current, max(.1,
                    (station.now - engaged[0]) * recipe.cc_current / current_ramp_seconds))
            elif cutoff and station.setpoint <= recipe.end_current + 1e-9:
                station.voltage = station.current = 0
            else:
                station.voltage, station.current = recipe.end_voltage, station.setpoint

        station.sample = sample
        bench = BenchSession(lambda: ObservedLoad(station), monitor_factory=lambda: FakeCan(station),
            settings=AutomationSettings.demo(), protocol_approved=True,
            cc_mode_code=10, cv_mode_code=20, standalone_ready=True, exclusive_confirmed=True,
            termination_ramp_approved=True, cancellation_event=FakeEvent(), clock=lambda: station.now)
        bench.probe()
        engine = AutomationEngine(recipe, "WO-S00430-1-001", "Operator", bench=bench, settings=AutomationSettings.demo())
        engine_ref.append(engine)
        clock = patch("leanwatts_can.automation.time.monotonic", side_effect=lambda: station.now)
        clock.start()
        self.addCleanup(clock.stop)
        engine.start_test(now=station.now)
        try:
            for _ in range(2000):
                if not engine.is_running:
                    break
                station.now += .25
                engine.tick(now=station.now)
            self.assertFalse(engine.is_running, engine.detail)
            self.assertEqual([c for c in station.calls if c[0] == "load.input"],
                             [("load.input", False), ("load.input", True), ("load.input", False)])
            self.assertFalse(any(c[0] == "can.send" for c in station.calls))
            self.assertEqual(bench.test_parameter_restore_result["status"], "PASS")
            self.assertEqual(len(bench.load_transition_events), 1)
            return engine, station, timeline, engaged[0]
        finally:
            if engine.is_running:
                engine.stop()

    def test_complete_engine_uses_real_session_ocv_first_and_exact_ramp(self):
        engine, station, timeline, engaged = self.run_flow()
        self.assertEqual(engine.outcome, "PASS", engine.detail)
        self.assertEqual(len(engine.results), 3)
        self.assertTrue(all(item["status"] == "PASS" for item in engine.results))
        self.assertEqual(engine.results[0]["name"], "Open-circuit voltage")
        self.assertEqual(engine.results[1]["name"], "Full current")
        self.assertIn("shutdown", engine.results[2]["name"])
        first_on = next(item for item in timeline if item[1:3] == ("input", True))
        self.assertEqual(first_on[3], [engine.results[0]["name"]])
        self.assertGreater(first_on[0], 106)  # No load at the premature50V plateau.
        transition = next(item for item in timeline if item[1] == "transition")
        ramps = [item for item in timeline if item[1] == "ramp"]
        self.assertEqual(transition[2], 5)
        self.assertEqual(ramps[0][2], 4.9)
        self.assertLessEqual(ramps[0][0]-transition[0], .5)
        expected = [round(4.9-.1*i, 1) for i in range(len(ramps))]
        self.assertEqual([round(item[2], 1) for item in ramps], expected)
        self.assertGreaterEqual(transition[0]-engaged, 18)

    def test_slow_current_ramp_waits_for_peak_instead_of_early_benchmark(self):
        engine, station, timeline, engaged = self.run_flow(current_ramp_seconds=30)
        self.assertEqual(engine.outcome, "PASS", engine.detail)
        transition = next(item for item in timeline if item[1] == "transition")
        self.assertGreaterEqual(transition[0]-engaged, 33)
        self.assertAlmostEqual(engine.results[1]["measured"], 15, delta=.1)

    def test_no_cutoff_reaches_point_five_without_skips_and_fails(self):
        engine, station, timeline, engaged = self.run_flow(cutoff=False)
        self.assertEqual(engine.outcome, "FAIL", engine.detail)
        ramps = [round(item[2], 1) for item in timeline if item[1] == "ramp"]
        self.assertEqual(ramps, [round(4.9-.1*i, 1) for i in range(45)])
        self.assertEqual(engine.results[-1]["status"], "FAIL")
        self.assertIn("0.5 A", engine.results[-1]["detail"])
        self.assertIn("without actual cutoff", engine.results[-1]["detail"])
        self.assertEqual(engine.stage_details[-1]["detail"], engine.results[-1]["detail"])


class LiveModeSerial(FakeSerial):
    drop_input = False
    drop_output = False
    ignore_current = False

    def write(self, request):
        if self.ignore_current and request[1] == 16 and request[2:4] == b"\x0a\x01":
            self.ignore_write = True
        result = super().write(request)
        self.ignore_write = False
        if request[1] == 16 and request[2:4] == b"\x0a\x00" and request[7:9] == b"\x00\x01":
            if self.drop_input:
                self.input_on = False
            if self.drop_output:
                self.voltage = self.current = 0
        return result


class ExternalMaynuoTests(unittest.TestCase):
    def setUp(self):
        self.serial = LiveModeSerial()
        self.load = MaynuoLoad(SerialSettings("COM-TEST", timeout=.05), 60, 25, 1500,
                               serial_factory=lambda **kwargs: self.serial)
        self.load.open()
        self.load.commission(self.load.probe(), 7, 11)
        self.load.enable_remote()
        self.load.configure("CV", 49)
        self.load.set_input(True)
        self.serial.voltage, self.serial.current = 49, 20
        self.serial.requests.clear()

    def tearDown(self):
        self.load.close()

    def writes(self):
        return [r for r in self.serial.requests if r[1] in (5, 16)]

    def test_preload_current_then_mode_once_without_on_off(self):
        self.assertTrue(self.load.transition_to_cc(5))
        writes = self.writes()
        self.assertEqual([struct.unpack(">H", r[2:4])[0] for r in writes], [0x0A01, 0x0A00])
        self.assertEqual(writes[0][7:11], struct.pack(">f", 5))
        self.assertEqual(writes[1][7:9], b"\x00\x01")
        self.assertTrue(self.serial.input_on)
        self.assertEqual(self.load.read().mode, "CC")

    def test_input_gap_is_not_restarted(self):
        self.serial.drop_input = True
        with self.assertRaisesRegex(MaynuoError, "not confirmed"):
            self.load.transition_to_cc(5)
        self.assertEqual(len(self.writes()), 2)
        self.assertFalse(self.serial.input_on)

    def test_lost_output_is_not_cutoff_evidence(self):
        self.serial.drop_output = True
        with self.assertRaisesRegex(MaynuoError, "not a cutoff"):
            self.load.transition_to_cc(5)

    def test_unconfirmed_preloaded_current_never_switches_mode(self):
        self.serial.ignore_current = True
        with self.assertRaisesRegex(MaynuoError, "not confirmed before"):
            self.load.transition_to_cc(5)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.serial.mode, self.serial.cv_code)

    def test_bad_live_conditions_and_limits_block_writes(self):
        for key, value in (("input_on", False), ("current", 0), ("faults", 32)):
            with self.subTest(key=key):
                old = getattr(self.serial, key)
                setattr(self.serial, key, value)
                with self.assertRaises(MaynuoError):
                    self.load.transition_to_cc(5)
                setattr(self.serial, key, old)
        with self.assertRaises(ValueError):
            self.load.transition_to_cc(26)
        self.assertEqual(self.writes(), [])
