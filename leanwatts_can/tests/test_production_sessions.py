"""Production records and workflows with fake instruments only."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from leanwatts_can.automation import AutomationSettings, DemoBench
from leanwatts_can.bench_session import BenchSession, BenchSessionError
from leanwatts_can.core import Recipe
from leanwatts_can.production_flow import ProductionFlow
from leanwatts_can.protocol import CONFIG, command
from leanwatts_can.session_store import OperatorStore, SessionStore
from leanwatts_can.tests.test_bench_session import FakeCan, FakeEvent, FakeLoad, Station


def production_recipe():
    r = Recipe(name="Production", cc_current=15, end_voltage=58.4, end_current=1.1,
        current_tolerance=.1, voltage_tolerance=.1, end_current_tolerance=.1,
        current_cal_load_voltage=48, voltage_cal_load_current=1,
        cc_test_load_voltage=49, voltage_test_load_current=1.5,
        max_voltage=65, max_current=20, max_power=1200,
        programmed_end_current=1.5)
    r.device_settings = {key: 0 for key in CONFIG if key not in r.charger_values()}
    r.termination_test = dict(enabled=True, start_current=5, minimum_current=.1,
        step_current=.1, dwell_seconds=3, off_voltage=5, off_current=.05,
        confirm_seconds=3, max_seconds=600)
    return r


class SessionRecordsTests(unittest.TestCase):
    def test_passwords_are_hashed_and_last_admin_is_protected(self):
        with tempfile.TemporaryDirectory() as folder:
            store = OperatorStore(folder)
            store.upsert_operator("1", "Manager", "abcd", role="admin")
            store.upsert_operator("2", "Operator", "efgh")
            self.assertEqual(store.authenticate("2", "efgh")["name"], "Operator")
            with self.assertRaises(ValueError):
                store.authenticate("2", "wrong")
            raw = Path(folder, "operators.json").read_text()
            self.assertNotIn('"abcd"', raw)
            self.assertNotIn('"efgh"', raw)
            with self.assertRaises(ValueError):
                store.delete_operator("1")
            with self.assertRaises(ValueError):
                store.upsert_operator("1", "Manager", "", enabled=False, role="admin")
            self.assertEqual(store.authenticate("1", "abcd")["role"], "admin")

    def test_crash_recovery_marks_unfinished_and_aggregate_keeps_repeat_serials(self):
        with tempfile.TemporaryDirectory() as folder:
            operator = dict(id="A", name="Tester", enabled=True, role="operator")
            store = SessionStore(folder)
            session = store.start(operator)
            first = store.begin_unit("SERIAL", production_recipe(), 1, serial_override=True)
            recovered = SessionStore(folder)
            self.assertEqual(recovered.active()["units"][0]["outcome"], "INTERRUPTED")
            second = recovered.begin_unit("SERIAL", production_recipe(), 1, serial_override=True)
            self.assertNotEqual(first, second)
            recovered.finish_unit(second, dict(outcome="FAIL", calibration_result="PASS", test_result="FAIL", stage_details=[]))
            closed = recovered.close()
            self.assertEqual(len(closed["units"]), 2)
            self.assertIsNone(recovered.active())
            for path in recovered.report_paths(session["session_id"]).values():
                self.assertTrue(Path(path).is_file())
            self.assertIn("INTERRUPTED", Path(closed["report_paths"]["html"]).read_text())

    def test_incomplete_result_cannot_be_promoted_to_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(folder)
            store.start(dict(id="A", name="Tester", role="operator", enabled=True))
            attempt = store.begin_unit("SERIAL", production_recipe(), 1, serial_override=True)
            with self.assertRaises(ValueError):
                store.finish_unit(attempt, dict(outcome="PASS", calibration_result="PASS", test_result="NOT RUN"))
            with self.assertRaises(ValueError):
                store.close()


class FlowTests(unittest.TestCase):
    def test_combined_demo_has_two_saves_three_tests_and_never_production_pass(self):
        r = production_recipe()
        self.assertEqual(r.validate_production(), "")
        clock = [100.0]
        cal = DemoBench(r)
        test = DemoBench(r)
        flow = ProductionFlow(r, "S", {"id": "A", "name": "Tester"}, 1,
            lambda: (cal, AutomationSettings.demo()), lambda: (test, AutomationSettings.demo()), serial_override=True)
        with patch("time.monotonic", lambda: clock[0]):
            flow.start()
            for _ in range(2500):
                clock[0] += .25
                flow.tick()
                if not flow.is_running:
                    break
        self.assertFalse(flow.is_running)
        result = flow.snapshot()["unit_result"]
        self.assertEqual(result["outcome"], "SIMULATION")
        self.assertEqual(len(result["test"]["results"]), 3)
        self.assertEqual(result["test"]["startup_can_tx_count"], 0)
        self.assertEqual(result["test"]["planned_start_count"], 0)
        self.assertEqual(result["test"]["can_tx_count"], 0)
        self.assertEqual(sum(item[0] == "save_current" for item in cal.commands), 1)
        self.assertEqual(sum(item[0] == "save_voltage" for item in cal.commands), 1)
        cutoff_start = next(n for n, item in enumerate(test.commands) if item[0] == "load_cc_step")
        self.assertFalse(any(item[0] == "load_off" for item in test.commands[cutoff_start:-2]))
        self.assertTrue(all(stage["before"] is not None for stage in result["stage_details"] if stage["name"] != "Test cleanup"))

    def test_calibration_failure_never_starts_test(self):
        r = production_recipe()
        called = []
        def failed_factory():
            raise RuntimeError("load timeout")
        flow = ProductionFlow(r, "S", "Tester", 1, failed_factory, lambda: called.append(True), serial_override=True)
        flow.start()
        self.assertEqual(called, [])
        self.assertEqual(flow.snapshot()["unit_result"]["outcome"], "FAIL")
        self.assertEqual(flow.snapshot()["unit_result"]["test_result"], "NOT RUN")


class AdvancingEvent(FakeEvent):
    def __init__(self, station):
        super().__init__()
        self.station = station
    def wait(self, seconds):
        self.station.now += seconds
        return super().wait(seconds)


def config_frames(values, include_adjustable=False):
    groups = {}
    for key, spec in CONFIG.items():
        if spec["response"] == 0x18FF56E5 and not include_adjustable:
            continue
        data = groups.setdefault(spec["response"], [0]*8)
        raw, offset = round(values[key]*spec["scale"]), spec["offset"]
        if spec.get("width") == 1:
            data[offset] = raw
        else:
            data[offset:offset+2] = [raw >> 8, raw & 255]
    return [dict(id=identity, extended=True, remote=False, dlc=8, data=data) for identity, data in groups.items()]


class ProductionBenchTests(unittest.TestCase):
    def make(self, expected, actual=None, include_adjustable=False, **options):
        station = Station()
        actual = dict(expected if actual is None else actual)
        def send(frame):
            selector = frame["data"][1]
            if frame == command("shutdown"):
                station.voltage = station.current = 0
            elif selector == 0x0B:
                station.frames.extend(config_frames(actual, include_adjustable))
            else:
                for key, spec in CONFIG.items():
                    if selector == spec["selector"]:
                        actual[key] = int.from_bytes(bytes(frame["data"][2:4]), "big") / spec["scale"]
        station.on_send = send
        session = BenchSession(lambda: FakeLoad(station), monitor_factory=lambda: FakeCan(station),
            settings=AutomationSettings.demo(), protocol_approved=True, cc_mode_code=10, cv_mode_code=20,
            exclusive_confirmed=True, cancellation_event=AdvancingEvent(station), clock=lambda: station.now,
            charger_parameters=expected, **options)
        session.probe()
        self.addCleanup(session.release)
        return station, session

    def values(self):
        r = production_recipe()
        return dict(r.device_settings, **r.charger_values())

    def test_missing_optional_zero_readbacks_do_not_force_write(self):
        s, bench = self.make(self.values())
        bench.acquire("calibration")
        result = bench.prepare_charger_parameters()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["optional_unavailable"]), 4)
        self.assertEqual(result["updated_fields"], [])
        self.assertTrue(all(call[1]["data"][1] == 0x0B for call in s.calls if call[0] == "can.send"))

    def test_missing_nonzero_adjustable_is_not_accepted(self):
        expected = self.values()
        expected["adjustVoltageMax"] = 60
        s, bench = self.make(expected)
        bench.acquire("calibration")
        with self.assertRaisesRegex(BenchSessionError, "Missing required"):
            bench.prepare_charger_parameters()
        self.assertEqual(bench.preflight_result["status"], "FAIL")
        self.assertTrue(all(call[1]["data"][1] == 0x0B for call in s.calls if call[0] == "can.send"))

    def test_mismatch_writes_once_after_shutdown_and_verifies(self):
        expected = self.values()
        actual = dict(expected, ccCurrent=14)
        s, bench = self.make(expected, actual)
        bench.acquire("calibration")
        result = bench.prepare_charger_parameters()
        sends = [call[1] for call in s.calls if call[0] == "can.send"]
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["after"]["ccCurrent"], 15)
        self.assertEqual(result["updated_fields"], ["ccCurrent"])
        config_positions = [n for n, frame in enumerate(sends) if frame["data"][1] in {spec["selector"] for spec in CONFIG.values()}]
        self.assertEqual(len(config_positions), 16)
        self.assertLess(sends.index(command("shutdown")), min(config_positions))

    def test_retired_post_test_can_start_is_rejected_before_hardware(self):
        # 1.1.9+: CAN Start for tests is retired; tests start by operator AC restart.
        with self.assertRaisesRegex(ValueError, "retired"):
            self.make(self.values(), post_test_can_start=True)

    def test_test_session_sends_no_charger_command(self):
        s, bench = self.make(self.values(), standalone_ready=True)
        bench.acquire("test")
        bench.load_off()
        bench.configure_load("CV", 49)
        bench.load_on()
        with self.assertRaises(BenchSessionError):
            bench.charger_shutdown()
        self.assertEqual([call for call in s.calls if call[0] == "can.send"], [])
        self.assertEqual(bench.can_tx_count, 0)

if __name__ == "__main__":
    unittest.main()
