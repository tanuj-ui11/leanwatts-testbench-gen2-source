"""Real adapter orchestration tested with deterministic fake instruments only."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from leanwatts_can.automation import AutomationEngine, AutomationSettings
from leanwatts_can.bench_session import BenchSession, BenchSessionError
from leanwatts_can.core import Recipe
from leanwatts_can.maynuo import LoadIdentity, LoadReading
from leanwatts_can.protocol import command, encode_duty, poll


class FakeEvent:
    def __init__(self):
        self.cancelled = False
        self.waits = []

    def is_set(self):
        return self.cancelled

    def set(self):
        self.cancelled = True

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.cancelled


class FakeLoad:
    def __init__(self, station):
        self.station = station
        self.settings = SimpleNamespace(port="COM8", address=1)
        self.max_voltage, self.max_current, self.max_power = station.limits
        self.commissioned = False
        self.sequence = 0

    def open(self):
        self.station.calls.append(("load.open",))
        return True

    def probe(self):
        self.station.calls.append(("load.probe",))
        return self.station.identity

    def commission(self, identity, cc, cv):
        self.station.calls.append(("load.commission", cc, cv))
        self.commissioned = True
        return True

    def read(self):
        s = self.station
        self.sequence += 1
        s.calls.append(("load.read",))
        if s.on_read is not None:
            callback, s.on_read = s.on_read, None
            callback()
        if s.sample is not None:
            s.sample()
        return LoadReading(s.voltage, s.current, s.voltage * s.current, 10 if s.mode == "CC" else 20,
                           s.mode if self.commissioned else "UNKNOWN",
                           s.setpoint if self.commissioned else None, s.input_on, s.faults,
                           s.now - s.age, 1 if s.repeat_sequence else self.sequence, s.remote,
                           s.identity.model_code, s.identity.firmware_code, unregulated=s.unregulated)

    def enable_remote(self):
        self.station.calls.append(("load.remote",))
        if not self.station.ignore_remote:
            self.station.remote = True
        return True

    def set_input(self, enabled):
        self.station.calls.append(("load.input", enabled))
        if not self.station.ignore_input:
            self.station.input_on = enabled
        return True

    def configure(self, mode, setpoint):
        self.station.calls.append(("load.configure", mode, setpoint))
        if not self.station.ignore_configuration:
            self.station.mode, self.station.setpoint = mode, setpoint
        return True

    def set_load_current(self, current):
        self.station.calls.append(("load.ramp", current))
        self.station.setpoint = current
        if self.station.drop_input_during_ramp:
            self.station.input_on = False
        return True

    def close(self):
        self.station.calls.append(("load.close",))


class FakeCan:
    def __init__(self, station):
        self.station = station
        self.state = "closed"
        self.listen_only = True

    def open(self, index, channel):
        self.station.calls.append(("can.open", index, channel))
        self.state = "open"
        return {"state": self.state, "listen_only": True}

    def start(self, bitrate, listen_only):
        self.station.calls.append(("can.start", bitrate, listen_only))
        self.listen_only = listen_only if not self.station.wrong_can_mode else not listen_only
        self.state = "listening" if self.listen_only else "active"
        return {"state": self.state, "listen_only": self.listen_only}

    def send(self, frame):
        s = self.station
        s.calls.append(("can.send", frame))
        if s.on_send is not None:
            s.on_send(frame)
        if s.fail_send:
            raise RuntimeError("SDK send failed")
        return {"accepted": 1, "acknowledged": False}

    def receive(self):
        self.station.calls.append(("can.receive",))
        frames, self.station.frames = self.station.frames, []
        return {"frames": frames}

    def close(self):
        self.station.calls.append(("can.close",))
        self.state, self.listen_only = "closed", True


class Station:
    def __init__(self):
        self.now = 100.0
        self.identity = LoadIdentity(0x8437, 1)
        self.calls = []
        self.limits = (100, 30, 3000)
        self.voltage, self.current = 48.0, 15.0
        self.mode, self.setpoint = "CC", 1.0
        self.input_on = self.remote = False
        self.age = 0.0
        self.faults = ()
        self.unregulated = False
        self.frames = []
        self.repeat_sequence = False
        self.ignore_remote = self.ignore_input = self.ignore_configuration = False
        self.drop_input_during_ramp = self.wrong_can_mode = self.fail_send = False
        self.on_read = self.on_send = None
        self.sample = None


class BenchSessionTests(unittest.TestCase):
    def setUp(self):
        self.station = Station()
        self.sessions = []

    def tearDown(self):
        for session in self.sessions:
            session.release()

    def make(self, **kwargs):
        s = self.station
        options = dict(load_factory=lambda: FakeLoad(s), monitor_factory=lambda: FakeCan(s),
                       settings=AutomationSettings.demo(), protocol_approved=True,
                       cc_mode_code=10, cv_mode_code=20, standalone_ready=True,
                       exclusive_confirmed=True, clock=lambda: s.now, cancellation_event=FakeEvent())
        options.update(kwargs)
        session = BenchSession(**options)
        self.sessions.append(session)
        return session

    def ready(self, operation="calibration", **kwargs):
        session = self.make(**kwargs)
        session.probe()
        session.acquire(operation)
        return session

    def sends(self):
        return [call[1] for call in self.station.calls if call[0] == "can.send"]

    def test_construction_and_probe_do_not_open_can_or_write_instruments(self):
        session = self.make()
        self.assertEqual(self.station.calls, [])
        self.assertFalse(session.capabilities.ready)
        result = session.probe()
        self.assertEqual(result["identity"], {"model_code": 0x8437, "firmware_code": 1})
        self.assertFalse(result["instrument_writes"])
        self.assertFalse(result["can_accessed"])
        self.assertIn("COM8", result["reference_identity"])
        self.assertTrue(session.capabilities.ready)
        allowed = {"load.open", "load.probe", "load.commission", "load.read", "load.close"}
        self.assertTrue(all(call[0] in allowed for call in self.station.calls))

    def test_missing_protocol_mapping_exclusivity_or_trim_commissioning_blocks(self):
        cases = ({"protocol_approved": False}, {"cc_mode_code": None}, {"cv_mode_code": 10},
                 {"exclusive_confirmed": False}, {"settings": AutomationSettings()})
        for options in cases:
            with self.subTest(options=options):
                session = self.make(**options)
                session.probe()
                with self.assertRaises(BenchSessionError):
                    session.acquire("calibration")
                self.assertEqual(self.sends(), [])
                self.assertFalse(any(call[0] == "can.open" for call in self.station.calls))

    def test_invalid_or_excessive_factory_hardware_limits_block_before_port_open(self):
        for limits in ((501, 30, 3000), (100, 121, 3000), (100, 30, 3701), (0, 1, 1)):
            with self.subTest(limits=limits):
                self.station.limits = limits
                with self.assertRaises(BenchSessionError):
                    self.make().probe()
                self.assertEqual(self.station.calls, [])

    def test_actual_probe_identity_is_rechecked_before_any_control_write(self):
        session = self.make()
        session.probe()
        self.station.identity = LoadIdentity(0x8438, 1)
        with self.assertRaises(BenchSessionError):
            session.acquire("calibration")
        self.assertFalse(any(call[0] in ("load.remote", "can.open", "can.send") for call in self.station.calls))

    def test_calibration_start_duties_and_saves_use_only_expected_can_frames(self):
        session = self.ready()
        self.assertIn(("can.start", 250000, False), self.station.calls)
        session.charger_shutdown()
        session.charger_start_calibration(80, 80)
        self.assertEqual(session._abort.waits, [.1] * 3)
        session.charger_apply_duties(80.1, 80.2)
        session.charger_save("current")
        session.charger_save("voltage")
        expected = ([command("shutdown"), command("calibrationStart")]
                    + [encode_duty("voltage", 80), encode_duty("current", 80)] * 3
                    + [encode_duty("voltage", 80.1), encode_duty("current", 80.2)] * 3
                    + [command("saveFullCurrent"), command("saveVoltage")])
        self.assertEqual(self.sends(), expected)
        self.assertEqual(session.can_tx_count, len(expected))
        self.assertEqual(session.can_accepted_count, len(expected))
        self.assertEqual(session._abort.waits, [.1] * 5)
        with self.assertRaises(BenchSessionError):
            session.charger_save("current")
        self.assertEqual(self.sends(), expected)

    def test_standalone_keeps_ack_enabled_and_never_sends_commands_even_abort_or_release(self):
        session = self.ready("test")
        self.assertIn(("can.start", 250000, False), self.station.calls)
        self.assertTrue(session.enter_passive())
        session.load_off()
        session.configure_load("CV", 48)
        session.load_on()
        session.read(self.station.now)
        with self.assertRaises(BenchSessionError):
            session.charger_shutdown()
        session.request_abort()
        session.load_off()
        session.release()
        self.assertEqual(self.sends(), [])
        self.assertEqual(session.can_tx_count, 0)
        self.assertEqual([call for call in self.station.calls if call[0] == "can.start"], [("can.start", 250000, False)])

    def test_wrong_can_mode_does_not_enable_remote_control(self):
        self.station.wrong_can_mode = True
        session = self.make()
        session.probe()
        with self.assertRaises(BenchSessionError):
            session.acquire("test")
        self.assertFalse(any(call[0] == "load.remote" for call in self.station.calls))
        self.assertEqual(self.sends(), [])

    def test_probe_measurements_are_independent_and_never_replaced_by_can_values(self):
        session = self.ready()
        self.station.frames = [{"id": 0x18FF50E5, "extended": True, "remote": False,
                                "dlc": 8, "data": [0, 120, 0, 10, 0, 0, 0, 0]}]
        reading = session.read(self.station.now)
        self.assertEqual((reading.voltage, reading.current), (48, 15))
        self.assertEqual(session.can_observations["outputVoltage"], 12)
        self.assertEqual(reading.timestamp, self.station.now)
        self.assertEqual(reading.source, session.capabilities.reference_identity)
        self.assertEqual(self.sends(), [])

    def test_stale_repeated_faulted_or_local_measurements_are_rejected(self):
        session = self.ready()
        session.read(self.station.now)
        for field, value in (("age", 3), ("faults", ("Over-power",)), ("remote", False), ("repeat_sequence", True)):
            with self.subTest(field=field):
                previous = getattr(self.station, field)
                setattr(self.station, field, value)
                with self.assertRaises(BenchSessionError):
                    session.read(self.station.now)
                setattr(self.station, field, previous)

    def test_unreg_passes_through_idle_probe_and_startup_without_hiding_faults(self):
        self.station.unregulated = True
        session = self.make(termination_ramp_approved=True)
        result = session.probe()
        self.assertTrue(result["reading"]["unregulated"])
        self.assertEqual(result["reading"]["faults"], ())
        session.acquire("test")
        session.configure_load("CC", 5)
        session.load_on()
        self.assertTrue(session.read(self.station.now).unregulated)
        with self.assertRaisesRegex(BenchSessionError, "regulated CC"):
            session.set_load_current(4)
        self.assertFalse(any(call[0] == "load.ramp" for call in self.station.calls))
        self.station.faults = ("Over-temperature",)
        with self.assertRaisesRegex(BenchSessionError, "Over-temperature"):
            session.read(self.station.now)
        self.station.faults = ()
        self.assertTrue(session.load_off())

    def test_can_diagnostic_retains_original_timestamp_for_workflow_freshness(self):
        session = self.ready()
        self.station.frames = [{"id": 0x18FF52E5, "extended": True, "remote": False,
                                "dlc": 8, "data": [0, 25, 0, 25, 0, 4, 0, 0]}]
        reading = session.read(self.station.now)
        self.assertEqual(reading.charger_fault_word, 4)
        self.assertEqual(reading.charger_fault_observed_at, self.station.now)
        self.assertEqual(reading.fault, "")
        self.station.now += 3
        stale = session.read(self.station.now)
        self.assertEqual(stale.charger_fault_word, 4)
        self.assertEqual(stale.charger_fault_observed_at, reading.charger_fault_observed_at)
        self.assertEqual(self.sends(), [])

    def test_calibration_diagnostic_refresh_sends_only_six_read_requests_and_keeps_cache(self):
        session = self.ready()
        session.can_observations["faultWord"] = 0x20
        session._can_observed_at["faultWord"] = self.station.now
        with self.assertRaises(BenchSessionError):
            session.request_charger_diagnostics()
        session.charger_shutdown()
        session.charger_start_calibration(80, 80)
        before = len(self.sends())
        self.assertTrue(session.request_charger_diagnostics())
        self.assertEqual(self.sends()[before:], [poll(group) for group in range(1, 7)])
        self.assertEqual(session.can_observations["faultWord"], 0x20)
        self.assertEqual(session._can_observed_at["faultWord"], self.station.now)

    def test_standalone_diagnostic_refresh_is_prohibited(self):
        session = self.ready("test")
        with self.assertRaises(BenchSessionError):
            session.request_charger_diagnostics()
        self.assertEqual(self.sends(), [])

    def test_external_abort_before_acquire_is_not_cleared(self):
        session = self.make()
        session.probe()
        before = list(self.station.calls)
        session.request_abort()
        with self.assertRaises(BenchSessionError):
            session.acquire("calibration")
        self.assertEqual(self.station.calls, before)

    def test_abort_mid_startup_cancels_unsent_frames_but_explicit_shutdown_still_works(self):
        session = self.ready()
        session.charger_shutdown()
        self.station.on_send = lambda frame: session.request_abort() if len(self.sends()) == 3 else None
        with self.assertRaises(BenchSessionError):
            session.charger_start_calibration(80, 80)
        self.assertIsNone(session._applied_duties)
        self.assertFalse(session._started)
        session.charger_shutdown()
        self.assertEqual(self.sends(), [command("shutdown"), command("calibrationStart"), encode_duty("voltage", 80), command("shutdown")])

    def test_failed_send_is_counted_uncertain_and_never_retried_or_reopened(self):
        session = self.ready()
        session.charger_shutdown()
        self.station.fail_send = True
        with self.assertRaises(RuntimeError):
            session.charger_start_calibration(80, 80)
        self.assertEqual(session.can_tx_count, 2)
        self.assertEqual(session.can_accepted_count, 1)
        self.assertEqual(session.can_trace[-1]["status"], "OUTCOME UNCONFIRMED")
        with self.assertRaises(BenchSessionError):
            session.charger_shutdown()
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(len([call for call in self.station.calls if call[0] == "can.open"]), 1)

    def test_actual_load_readbacks_must_confirm_input_mode_and_remote(self):
        session = self.ready()
        self.station.ignore_configuration = True
        with self.assertRaises(BenchSessionError):
            session.configure_load("CV", 48)
        self.station.ignore_configuration = False
        session.configure_load("CV", 48)
        self.station.ignore_input = True
        with self.assertRaises(BenchSessionError):
            session.load_on()
        self.assertEqual(self.sends(), [])

    def test_abort_during_prewrite_read_prevents_configuration_write(self):
        session = self.ready()
        self.station.on_read = session.request_abort
        with self.assertRaises(BenchSessionError):
            session.configure_load("CV", 48)
        self.assertFalse(any(call[0] == "load.configure" for call in self.station.calls))

    def test_continuous_ramp_requires_separate_qualification_and_keeps_input_on(self):
        session = self.ready("test")
        session.configure_load("CC", 5)
        session.load_on()
        with self.assertRaises(BenchSessionError):
            session.set_load_current(4)
        self.assertFalse(session.capabilities.termination_ramp)
        session.release()
        session = self.ready("test", termination_ramp_approved=True)
        session.configure_load("CC", 5)
        session.load_on()
        before = len(self.station.calls)
        session.set_load_current(4)
        self.assertNotIn(("load.input", False), self.station.calls[before:])
        self.assertNotIn(("load.input", True), self.station.calls[before:])
        self.assertTrue(self.station.input_on)
        self.assertEqual(self.station.setpoint, 4)
        self.assertEqual(self.sends(), [])
        self.station.drop_input_during_ramp = True
        with self.assertRaises(BenchSessionError):
            session.set_load_current(3)

    def test_parallel_sessions_and_wrong_worker_cannot_share_the_fixture(self):
        first = self.make()
        first.probe()
        other = self.make()
        other.probe()
        first.acquire("calibration")
        with self.assertRaises(BenchSessionError):
            other.acquire("calibration")
        calls = list(self.station.calls)
        with self.assertRaises(BenchSessionError):
            other.probe()
        self.assertEqual(self.station.calls, calls)
        errors = []
        worker = threading.Thread(target=lambda: self._wrong_worker(first, errors))
        before = len(self.station.calls)
        worker.start()
        worker.join(1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], BenchSessionError)
        self.assertEqual(len(self.station.calls), before)

    @staticmethod
    def _wrong_worker(session, errors):
        try:
            session.read(100)
        except Exception as exc:
            errors.append(exc)

    def test_release_only_closes_connections_without_switching_outputs_or_restoring_tx(self):
        session = self.ready()
        before = len(self.station.calls)
        session.release()
        self.assertEqual(self.station.calls[before:], [("can.close",), ("load.close",)])
        self.assertFalse(session._owned)
        self.assertTrue(session._passive)

    def run_engine(self, operation):
        s = self.station
        recipe = Recipe(name="Fake fixture", cc_current=15, end_voltage=58.4, end_current=.5,
                        current_tolerance=.1, voltage_tolerance=.1, end_current_tolerance=.05,
                        current_cal_load_voltage=48, voltage_cal_load_current=1,
                        max_voltage=100, max_current=30, max_power=3000)
        physical = dict(on=operation == "test", voltage_duty=80.0, current_duty=80.0)

        def sample():
            s.voltage = s.current = 0.0
            if s.input_on and physical["on"]:
                if s.mode == "CV":
                    s.voltage = s.setpoint
                    s.current = recipe.cc_current + (0 if operation == "test" else (physical["current_duty"] - 80.6) * .5)
                else:
                    s.current = s.setpoint
                    s.voltage = recipe.end_voltage + (0 if operation == "test" else (physical["voltage_duty"] - 80.8) * .5)

        def send(frame):
            data = frame["data"]
            if data[1] == 0x14:
                physical["on"] = data[-1] == 1
            elif data[1] in (0x15, 0x16):
                physical["voltage_duty" if data[1] == 0x15 else "current_duty"] = (data[2] * 256 + data[3]) / 10

        s.sample, s.on_send = sample, send
        session = self.make()
        session.probe()
        engine = AutomationEngine(recipe, "FAKE-001", "test", bench=session, settings=session.settings)
        with patch("leanwatts_can.automation.time.monotonic", side_effect=lambda: s.now):
            getattr(engine, "start_calibration" if operation == "calibration" else "start_test")(now=s.now)
            for _ in range(300):
                if not engine.is_running:
                    break
                s.now += .25
                engine.tick(now=s.now)
        self.assertFalse(engine.is_running, engine.detail)
        return session, engine

    def test_engine_runs_complete_fake_calibration_through_real_session_contract(self):
        session, engine = self.run_engine("calibration")
        self.assertNotIn("ERROR", engine.outcome, engine.detail)
        self.assertEqual(len([frame for frame in self.sends() if frame == command("saveFullCurrent")]), 1)
        self.assertEqual(len([frame for frame in self.sends() if frame == command("saveVoltage")]), 1)
        self.assertTrue(all(frame["data"][1] in (0x14, 0x15, 0x16, 0x17, 0x18) for frame in self.sends()))
        self.assertFalse(session._owned)
        self.assertFalse(self.station.input_on)

    def test_incomplete_standalone_recipe_cannot_start_a_production_test(self):
        # Complete external-restart adapter flow is covered separately, including
        # the required cutoff and live CV -> CC transition.
        with self.assertRaisesRegex(ValueError, "cutoff"):
            self.run_engine("test")
        self.assertEqual(self.sends(), [])
        self.assertFalse(self.station.input_on)


if __name__ == "__main__":
    unittest.main()
