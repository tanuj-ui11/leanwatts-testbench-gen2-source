"""Battery-absent transition regressions using deterministic instruments only."""
import unittest
from dataclasses import replace
from unittest.mock import patch

from leanwatts_can.automation import AutomationEngine, AutomationSettings, DemoBench
from leanwatts_can.protocol import describe_charger_fault
from leanwatts_can.tests.test_automation import recipe


def cutoff_recipe():
    """Standalone tests require the end-current cutoff test to be enabled."""
    profile = recipe()
    profile.termination_test = dict(enabled=True, start_current=1, minimum_current=.2, step_current=.05,
        dwell_seconds=1, off_voltage=5, off_current=.05, confirm_seconds=3, max_seconds=90)
    return profile


class DiagnosticBench(DemoBench):
    def __init__(self):
        super().__init__(recipe())
        self.off_word = 0x20
        self.active_word = None  # retain the last diagnostic until explicitly replaced
        self.last_word = self.last_observed = None
        self.transform = lambda reading: reading

    def read(self, now):
        reading = super().read(now)
        word = self.active_word if self._charger_on else self.off_word
        if word is not None:
            self.last_word, self.last_observed = word, now
        return self.transform(replace(reading, charger_fault_word=self.last_word,
                                      charger_fault_observed_at=self.last_observed))


class ChargerFaultWorkflowTests(unittest.TestCase):
    def make(self, bench=None, **settings):
        bench = bench or DiagnosticBench()
        engine = AutomationEngine(recipe(), "FAULT-TEST", "Tester", bench=bench,
            settings=replace(AutomationSettings.demo(), **settings))
        engine.start_calibration(now=0)
        return engine, bench

    def start_axis(self, engine):
        for now in (.25, .5):
            engine.tick(now)
            self.assertTrue(engine.is_running, engine.detail)
            self.assertEqual(engine.phase, "off_current")
            self.assertFalse(any(c[0] == "load_setup" for c in engine.bench.commands))
        engine.tick(.75)
        self.assertTrue(engine.is_running, engine.detail)
        self.assertEqual(engine.phase, "cal_current")

    def assert_no_trim_save(self, bench):
        self.assertFalse(any(c[0] in ("three_duty_pairs", "save_current", "save_voltage")
                             for c in bench.commands))

    def test_off_battery_absent_requires_three_independent_low_readings(self):
        engine, bench = self.make()
        self.start_axis(engine)
        self.assertEqual(len([s for s in engine.samples if s["phase"] == "off_current"]), 3)
        self.assertEqual(engine.samples[-1]["charger_fault_word"], 0x20)
        self.assert_no_trim_save(bench)

    def test_cached_prestart_bit_waits_for_fresh_clear_then_calibrates(self):
        engine, bench = self.make()
        self.start_axis(engine)
        for now in (1, 1.25, 1.5, 2, 2.5, 3):
            engine.tick(now)
            self.assertTrue(engine.is_running, engine.detail)
            self.assertEqual(engine.stable_count, 0)
            self.assertIn("Waiting for fresh charger diagnostics", engine.detail)
        self.assert_no_trim_save(bench)
        self.assertEqual(bench.last_observed, .75)  # waiting never re-ages the indication
        bench.active_word = 0
        for n in range(13, 250):
            engine.tick(n * .25)
            if not engine.is_running:
                break
        self.assertIn("CALIBRATED", engine.outcome, engine.detail)
        self.assertEqual([c[0] for c in bench.commands].count("save_current"), 1)
        self.assertEqual([c[0] for c in bench.commands].count("save_voltage"), 1)

    def test_ageout_or_missing_clear_cannot_enable_tuning_or_save(self):
        engine, bench = self.make(shutdown_timeout=3)
        self.start_axis(engine)
        for n in range(4, 18):
            engine.tick(n * .25)
            if not engine.is_running:
                break
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("no fresh clear", engine.detail)
        self.assert_no_trim_save(bench)

    def test_transient_fresh_startup_battery_absent_waits_then_fresh_clear_proceeds(self):
        engine, bench = self.make()
        self.start_axis(engine)
        bench.active_word = 0x20
        for now in (1, 1.25, 1.5):
            engine.tick(now)
            self.assertTrue(engine.is_running, engine.detail)
            self.assertEqual(engine.stable_count, 0)
            self.assert_no_trim_save(bench)
        bench.active_word = 0
        engine.tick(1.75)
        self.assertTrue(engine.is_running, engine.detail)
        self.assertIsNone(engine._charger_startup_after)
        bench.active_word = 0x20
        engine.tick(2)
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("0x0020", engine.detail)
        self.assert_no_trim_save(bench)

    def test_fresh_battery_absent_first_seen_after_start_uses_original_deadline(self):
        bench = DiagnosticBench()
        bench.off_word = 0
        engine, bench = self.make(bench, shutdown_timeout=3, settling_seconds=4)
        self.start_axis(engine)
        deadline = engine._charger_clear_deadline
        bench.active_word = 0x20
        engine.tick(2.5)
        self.assertTrue(engine.is_running, engine.detail)
        # The start anchor stays at .75; a releasing clear must be fresher than
        # the latest battery-absent observation (2.5), which is the stricter
        # gate. Only the deadline is fixed to the original start.
        self.assertEqual(engine._charger_startup_after, .75)
        self.assertEqual(engine._charger_clear_after, 2.5)
        self.assertEqual(engine._charger_clear_deadline, deadline)
        engine.tick(deadline)
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("startup deadline", engine.detail)
        self.assert_no_trim_save(bench)

    def test_fresh_clear_on_normal_start_disarms_startup_exception(self):
        bench = DiagnosticBench()
        bench.off_word = 0
        bench.active_word = 0
        engine, bench = self.make(bench)
        self.start_axis(engine)
        engine.tick(1)
        self.assertIsNone(engine._charger_startup_after)
        bench.active_word = 0x20
        engine.tick(1.25)
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("0x0020", engine.detail)

    def test_tuning_measurement_on_normal_start_disarms_startup_exception(self):
        bench = DiagnosticBench()
        bench.off_word = 0
        engine, bench = self.make(bench)
        self.start_axis(engine)
        engine.tick(1.5)
        self.assertIsNone(engine._charger_startup_after)
        bench.active_word = 0x20
        engine.tick(1.75)
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("0x0020", engine.detail)

    def test_old_clear_or_absent_telemetry_cannot_release_startup_wait(self):
        for word, observed in ((0, .5), (None, None)):
            with self.subTest(word=word):
                engine, bench = self.make()
                self.start_axis(engine)
                bench.last_word, bench.last_observed = word, observed
                engine.tick(1)
                self.assertTrue(engine.is_running, engine.detail)
                self.assertIn("Waiting for fresh charger diagnostics", engine.detail)
                self.assert_no_trim_save(bench)

    def test_startup_read_requests_are_once_and_only_after_expected_off_diagnostic(self):
        for off_word, operation, expected in ((0x20, "calibration", 1), (0, "calibration", 0), (0, "test", 0)):
            with self.subTest(off_word=off_word, operation=operation):
                bench = DiagnosticBench()
                bench.off_word = off_word
                calls = []
                def refresh():
                    calls.append(True)
                    bench.active_word = 0
                    return True
                bench.request_charger_diagnostics = refresh
                engine = AutomationEngine(cutoff_recipe() if operation == "test" else recipe(),
                                          "FAULT-TEST", "Tester", bench=bench)
                getattr(engine, "start_" + operation)(now=0)
                for now in (.25, .5, .75, 1, 1.25):
                    engine.tick(now)
                self.assertTrue(engine.is_running, engine.detail)
                self.assertEqual(len(calls), expected)
                self.assertIsNone(engine._charger_clear_after)
                if operation == "test":
                    self.assertEqual(bench.can_tx_count, 0)

    def test_unconfirmed_startup_read_requests_fail_without_tuning_or_save(self):
        bench = DiagnosticBench()
        bench.request_charger_diagnostics = lambda: False
        engine, bench = self.make(bench)
        for now in (.25, .5, .75):
            engine.tick(now)
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("diagnostic requests", engine.detail)
        self.assert_no_trim_save(bench)

    def test_persistent_fresh_startup_bit_polls_at_most_once_a_second_until_fixed_deadline(self):
        bench = DiagnosticBench()
        bench.active_word = 0x20
        calls = []
        bench.request_charger_diagnostics = lambda: calls.append(engine._now) or True
        engine, bench = self.make(bench, shutdown_timeout=3)
        self.start_axis(engine)
        deadline = engine._charger_clear_deadline
        for n in range(4, 30):
            engine.tick(n * .25)
            if not engine.is_running:
                break
            self.assertEqual(engine._charger_clear_deadline, deadline)
            self.assertEqual(engine.stable_count, 0)
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("startup deadline", engine.detail)
        self.assertEqual(calls, [.75, 1.75, 2.75])
        self.assertEqual([c[0] for c in bench.commands].count("calibration_start_and_three_duty_pairs"), 1)
        self.assert_no_trim_save(bench)

    def test_startup_poll_io_counts_toward_deadline_without_reaging_observation(self):
        bench = DiagnosticBench()
        bench.capabilities = replace(bench.capabilities, simulation=False)
        clock = [0.0]
        calls = []
        def refresh():
            calls.append(clock[0])
            clock[0] += 1.5
            return True
        bench.request_charger_diagnostics = refresh
        with patch("leanwatts_can.automation.time.monotonic", side_effect=lambda: clock[0]):
            engine, bench = self.make(bench, shutdown_timeout=3)
            for now in (.25, .5, .75):
                clock[0] = now
                engine.tick(now)
            self.assertTrue(engine.is_running, engine.detail)
            self.assertEqual(engine._now, 2.25)
            self.assertEqual(engine._charger_clear_after, .75)
            self.assertEqual(engine._charger_clear_deadline, 3.75)
            self.assertEqual(bench.last_observed, .75)
            clock[0] = 2.5
            engine.tick(2.5)
            self.assertEqual(calls, [.75])
            clock[0] = 3.25
            engine.tick(3.25)
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("startup deadline", engine.detail)
        self.assertEqual(calls, [.75, 3.25])
        self.assert_no_trim_save(bench)

    def test_startup_wait_is_capped_at_fifteen_seconds(self):
        engine, _ = self.make(shutdown_timeout=60, stage_timeout=120)
        self.start_axis(engine)
        self.assertEqual(engine._charger_clear_deadline, 15.75)

    def test_duplicate_acquisitions_cannot_extend_startup_deadline(self):
        engine, bench = self.make(shutdown_timeout=3)
        self.start_axis(engine)
        bench.transform = lambda r: replace(r, sequence=engine._last_sequence)
        for n in range(4, 17):
            engine.tick(n * .25)
            if not engine.is_running:
                break
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("startup deadline", engine.detail)
        self.assert_no_trim_save(bench)

    def test_cleanup_battery_absent_requires_accepted_shutdown_and_measured_low_output(self):
        for failure in (None, "shutdown", "high_output", "mixed_fault", "adapter"):
            with self.subTest(failure=failure):
                engine, bench = self.make()
                self.start_axis(engine)
                if failure == "shutdown":
                    bench.charger_shutdown = lambda: False
                    bench.transform = lambda r: replace(r, voltage=0, current=0, power=0)
                elif failure == "high_output":
                    bench.transform = lambda r: replace(r, voltage=48, current=1, power=48)
                elif failure == "mixed_fault":
                    bench.off_word = 0x24
                elif failure == "adapter":
                    bench.transform = lambda r: replace(r, fault="CAN adapter error frame received")
                engine.stop()
                self.assertEqual(bench._load_on, failure is not None)
                if failure is None:
                    self.assertIn("Load OFF confirmed", engine.stop_outcome)
                else:
                    self.assertIn("Load retained", engine.stop_outcome)

    def start_external_test(self, bench):
        polls = []
        bench.request_charger_diagnostics = lambda: polls.append(True) or True
        engine = AutomationEngine(cutoff_recipe(), "FAULT-TEST", "Tester", bench=bench)
        engine.start_test(now=0)
        return engine, polls

    def test_external_restart_battery_absent_window_is_bounded_and_command_free(self):
        # External-restart tests tolerate the restart's battery-absent bit only
        # inside a fixed 15 s window anchored to the observed output rise. The
        # test never polls or transmits; a persistent bit fails at the deadline.
        bench = DiagnosticBench()
        bench.active_word = 0x20
        engine, polls = self.start_external_test(bench)
        deadline = None
        for n in range(1, 200):
            engine.tick(n * .25)
            if engine._charger_clear_deadline is not None:
                deadline = deadline or engine._charger_clear_deadline
                self.assertEqual(engine._charger_clear_deadline, deadline)
            if not engine.is_running:
                break
        self.assertEqual(deadline, engine.external_restart_evidence["rising_observed"]["timestamp"] + 15)
        self.assertEqual(engine._now, deadline)
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("Battery not connected", engine.detail)
        self.assertIn("startup deadline", engine.detail)
        self.assertFalse(any(r["name"] == "Full current" and r["status"] == "PASS" for r in engine.results))
        self.assertEqual(polls, [])
        self.assertEqual(bench.can_tx_count, 0)

    def test_external_restart_battery_absent_after_fresh_clear_fails_immediately(self):
        bench = DiagnosticBench()
        bench.active_word = 0
        engine, polls = self.start_external_test(bench)
        for n in range(1, 13):
            engine.tick(n * .25)
        self.assertTrue(engine.is_running, engine.detail)
        self.assertEqual(engine.phase, "test_current")
        self.assertIsNone(engine._charger_startup_after)  # fresh clear closed the window
        bench.active_word = 0x20
        engine.tick(3.25)
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("0x0020", engine.detail)
        self.assertEqual(polls, [])
        self.assertEqual(bench.can_tx_count, 0)

    def test_mixed_reverse_unknown_and_adapter_faults_never_get_off_exception(self):
        for word, extra in ((0x24, ""), (0x10, ""), (0x8000, ""), (0x20, "CAN adapter error frame received")):
            with self.subTest(word=word, extra=extra):
                bench = DiagnosticBench()
                bench.off_word = word
                bench.transform = lambda r, extra=extra: replace(r, fault=extra)
                engine, bench = self.make(bench)
                engine.tick(.25)
                self.assertIn("ERROR", engine.outcome)
                self.assertFalse(any(c[0] == "load_setup" for c in bench.commands))

    def test_battery_absent_does_not_replace_independent_shutdown_evidence(self):
        bench = DiagnosticBench()
        bench.transform = lambda r: replace(r, voltage=6, current=.2, power=1.2)
        engine, bench = self.make(bench)
        engine.tick(.25)
        self.assertIn("ERROR", engine.outcome)
        self.assertFalse(any(c[0] == "load_setup" for c in bench.commands))

    def test_independent_limits_and_load_state_remain_enforced_while_waiting(self):
        for changed, phrase in (({"voltage": 66}, "bench limits"),
                                ({"power": 1201}, "bench limits"),
                                ({"fault": "Over-temperature"}, "Over-temperature"),
                                ({"load_on": False}, "Load mode")):
            with self.subTest(changed=changed):
                engine, bench = self.make()
                self.start_axis(engine)
                bench.transform = lambda r, changed=changed: replace(r, **changed)
                engine.tick(1)
                self.assertIn("ERROR", engine.outcome)
                self.assertIn(phrase, engine.detail)
                self.assert_no_trim_save(bench)

    def test_bad_diagnostic_timestamps_fail_closed(self):
        for stamp in (None, float("nan"), float("inf"), 100):
            with self.subTest(stamp=stamp):
                bench = DiagnosticBench()
                bench.transform = lambda r, stamp=stamp: replace(r, charger_fault_observed_at=stamp)
                engine, _ = self.make(bench)
                engine.tick(.25)
                self.assertIn("ERROR", engine.outcome)
                self.assertIn("observation time", engine.detail)

    def test_default_mapping_labels_only_evidenced_bit_and_retains_other_bits(self):
        self.assertIn("Battery not connected", describe_charger_fault(0x20))
        self.assertNotIn("shutdown", describe_charger_fault(0x20).lower())
        self.assertIn("0x8020", describe_charger_fault(0x8020))
        self.assertIn("other fault bits 0x8000", describe_charger_fault(0x8020))
        self.assertIn("clear", describe_charger_fault(0))
        for word in (True, -1, 65536, None):
            with self.assertRaises(ValueError):
                describe_charger_fault(word)


if __name__ == "__main__":
    unittest.main()
