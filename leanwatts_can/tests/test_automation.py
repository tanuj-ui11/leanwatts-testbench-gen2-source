"""Automation contract tests use deterministic fake instruments only."""
import json
import os
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from leanwatts_can.automation import (
    AutomationEngine, AutomationSettings, DemoBench, UnavailableBench,
)
from leanwatts_can.core import Recipe


def recipe():
    return Recipe(name="58.4 V variant", cc_current=15, end_voltage=58.4,
                  end_current=.5, current_tolerance=.1, voltage_tolerance=.1,
                  end_current_tolerance=.05, current_cal_load_voltage=48,
                  voltage_cal_load_current=1, max_voltage=65, max_current=20, max_power=1200)


class ChangingBench(DemoBench):
    def __init__(self, profile, transform):
        super().__init__(profile)
        self.transform = transform

    def read(self, now):
        return self.transform(super().read(now), self)


class AutomationTests(unittest.TestCase):
    def make(self, bench=None, settings=None, report_dir=None):
        profile = recipe()
        profile.end_current_tolerance = .1
        profile.termination_test = dict(enabled=True, start_current=1, minimum_current=.2,
            step_current=.05, dwell_seconds=1, off_voltage=5, off_current=.05, confirm_seconds=3, max_seconds=90)
        return AutomationEngine(profile, "UNIT-01", "Operator", 2, report_dir,
                                bench if bench is not None else DemoBench(profile), settings)

    def finish(self, engine, limit=800):
        for n in range(1, limit):
            engine.tick(n * .25)
            if not engine.is_running:
                return
        self.fail("Workflow did not finish")

    def until_phase(self, engine, phase):
        for n in range(1, 30):
            engine.tick(n * .25)
            if engine.phase == phase:
                return n
        self.fail("Phase not reached: " + phase)

    def test_unavailable_hardware_never_starts(self):
        engine = self.make(UnavailableBench())
        for action in (engine.start_calibration, engine.start_test):
            with self.assertRaisesRegex(ValueError, "USB command map"):
                action(now=0)
        self.assertFalse(engine.is_running)
        self.assertEqual(engine.run_id, "")

    def test_calibration_two_saves_no_config_and_explicit_persistence(self):
        engine = self.make()
        engine.start_calibration(now=0)
        self.finish(engine)
        names = [command[0] for command in engine.bench.commands]
        self.assertEqual(names.count("save_current"), 1)
        self.assertEqual(names.count("save_voltage"), 1)
        self.assertEqual(names.count("calibration_start_and_three_duty_pairs"), 2)
        self.assertFalse(any("config" in name for name in names))
        self.assertEqual(engine.outcome, "SIMULATION — CALIBRATED — PERSISTENCE UNVERIFIED")
        self.assertEqual(len(engine.results), 2)
        self.assertFalse(engine.snapshot()["persistence_verified"])
        self.assertFalse(engine.bench._load_on)
        self.assertFalse(engine.bench._charger_on)
        self.assertFalse(engine.bench._owned)
        self.assertLess(engine.snapshot()["elapsed_seconds"], 30)
        for sample in engine.samples:
            self.assertIn("SIMULATED", sample["source"])

    def test_standalone_test_is_independent_and_transmit_free(self):
        engine = self.make()
        engine.start_test(now=0)  # no calibration prerequisite
        self.finish(engine)
        self.assertEqual(engine.bench.can_tx_count, 0)
        self.assertEqual(engine.outcome, "SIMULATION — PASS")
        self.assertEqual([item["status"] for item in engine.results], ["PASS", "PASS", "PASS"])
        self.assertGreater(engine.results[-1]["measured"], 0)
        self.assertTrue(engine.bench._passive)
        self.assertFalse(engine.bench._load_on)

    def test_disabled_cutoff_is_rejected_before_io(self):
        profile = recipe()
        bench = DemoBench(profile)
        engine = AutomationEngine(profile, "UNIT", "Tester", bench=bench)
        with self.assertRaisesRegex(ValueError, "cutoff"):
            engine.start_test(now=0)
        self.assertEqual(bench.commands, [])

    def test_test_abort_never_sends_charger_shutdown(self):
        engine = self.make()
        engine.start_test(now=0)
        engine.stop()
        self.assertEqual(engine.bench.can_tx_count, 0)
        self.assertIn("ABORTED", engine.outcome)
        self.assertFalse(engine.bench._load_on)
        self.assertTrue(engine.bench._passive)
        self.assertIn("AC OFF", engine.stop_outcome)

    def test_test_rejects_missing_passive_or_standalone_approval(self):
        for capability in ("passive_can", "standalone_ready", "independent_measurements", "verified_load_protocol", "exclusive_control"):
            bench = DemoBench(recipe())
            bench.capabilities = replace(bench.capabilities, **{capability: False})
            engine = self.make(bench)
            with self.assertRaises(ValueError):
                engine.start_test(now=0)
            self.assertEqual(bench.commands, [])

    def test_uncommissioned_real_calibration_is_blocked(self):
        bench = DemoBench(recipe())
        bench.capabilities = replace(bench.capabilities, simulation=False)
        engine = self.make(bench)
        with self.assertRaisesRegex(ValueError, "calibration duty bounds"):
            engine.start_calibration(now=0)
        self.assertEqual(bench.commands, [])

    def test_invalid_measurement_settings_block_tests(self):
        settings = replace(AutomationSettings.demo(), stable_samples=0)
        engine = self.make(settings=settings)
        with self.assertRaises(ValueError):
            engine.start_test(now=0)

    def test_tx_leak_invalidates_standalone_result(self):
        engine = self.make()
        engine.start_test(now=0)
        engine.bench.can_tx_count += 1
        engine.tick(.25)
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("CAN transmission", engine.detail)
        self.assertEqual(engine.results, [])
        self.assertEqual(engine.bench.can_tx_count, 1)

    def test_stale_nan_overlimit_wrong_source_fault_each_abort(self):
        cases = [
            (lambda r, b: replace(r, timestamp=r.timestamp-10), "stale"),
            (lambda r, b: replace(r, voltage=float("nan")), "non-finite"),
            (lambda r, b: replace(r, voltage=66), "bench limits"),
            (lambda r, b: replace(r, power=1201), "bench limits"),
            (lambda r, b: replace(r, source="charger CAN"), "independent reference"),
            (lambda r, b: replace(r, fault="overtemperature"), "Load fault"),
        ]
        for transform, phrase in cases:
            with self.subTest(phrase=phrase):
                engine = self.make(ChangingBench(recipe(), transform))
                engine.start_test(now=0)
                engine.tick(.25)
                self.assertIn("ERROR", engine.outcome)
                self.assertIn(phrase, engine.detail)
                self.assertEqual(engine.bench.can_tx_count, 0)

    def test_duplicate_samples_cannot_establish_stability(self):
        bench = ChangingBench(recipe(), lambda r, b: replace(r, sequence=1))
        engine = self.make(bench, replace(AutomationSettings.demo(), stage_timeout=2))
        engine.start_test(now=0)
        self.finish(engine)
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("Timed out", engine.detail)
        self.assertEqual(len(engine.samples), 1)

    def test_unreg_startup_waits_without_trim_or_save_then_recovers(self):
        bench = ChangingBench(recipe(), lambda r, b: replace(r, unregulated=b.waiting))
        bench.waiting = True
        engine = self.make(bench)
        engine.start_calibration(now=0)
        self.until_phase(engine, "cal_current")
        for n in range(4, 12):
            engine.tick(n * .25)
        self.assertTrue(engine.is_running)
        self.assertIn("UNREG", engine.detail)
        self.assertEqual(engine.stable_count, 0)
        self.assertEqual(engine.results, [])
        self.assertFalse(any(c[0] in ("three_duty_pairs", "save_current", "save_voltage") for c in bench.commands))
        self.assertTrue(any(s["unregulated"] for s in engine.samples))
        bench.waiting = False
        for n in range(12, 200):
            engine.tick(n * .25)
            if not engine.is_running:
                break
        self.assertEqual(engine.outcome, "SIMULATION — CALIBRATED — PERSISTENCE UNVERIFIED", engine.detail)

    def test_persistent_unreg_times_out_without_trim_save_or_pass(self):
        bench = ChangingBench(recipe(), lambda r, b: replace(r, unregulated=True))
        engine = self.make(bench, replace(AutomationSettings.demo(), stage_timeout=2))
        engine.start_calibration(now=0)
        self.finish(engine)
        self.assertEqual(engine.outcome, "SIMULATION — ERROR")
        self.assertIn("Timed out", engine.detail)
        self.assertFalse(any(c[0] in ("three_duty_pairs", "save_current", "save_voltage") for c in bench.commands))
        self.assertFalse(any(item["status"] == "PASS" for item in engine.results))

    def test_unreg_during_shutdown_does_not_prevent_measured_off_confirmation(self):
        bench = ChangingBench(recipe(), lambda r, b: replace(r, unregulated=not b._charger_on))
        engine = self.make(bench)
        engine.start_calibration(now=0)
        self.finish(engine)
        self.assertEqual(engine.outcome, "SIMULATION — CALIBRATED — PERSISTENCE UNVERIFIED", engine.detail)
        self.assertTrue(any(s["unregulated"] and s["phase"].startswith("off_") for s in engine.samples))
        self.assertFalse(bench._load_on)

    def test_shutdown_requires_independent_low_output_before_mode_change(self):
        bench = ChangingBench(recipe(), lambda r, b: replace(r, voltage=48, current=1, power=48))
        engine = self.make(bench, replace(AutomationSettings.demo(), shutdown_timeout=1))
        engine.start_calibration(now=0)
        self.finish(engine)
        self.assertIn("ERROR", engine.outcome)
        self.assertFalse(any(c[0] == "load_setup" for c in bench.commands))
        self.assertFalse(any(c[0].startswith("save_") for c in bench.commands))
        self.assertIn("Load retained", engine.stop_outcome)

    def test_startup_partial_failure_never_tunes_or_saves(self):
        bench = DemoBench(recipe())
        bench.charger_start_calibration = lambda *args: False
        engine = self.make(bench)
        engine.start_calibration(now=0)
        self.finish(engine)
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("three duty pairs", engine.detail)
        self.assertFalse(any(c[0].startswith("save_") for c in bench.commands))

    def test_duty_bounds_stop_instead_of_clamping_or_saving(self):
        engine = self.make(settings=replace(AutomationSettings.demo(), current_max=80.1))
        engine.start_calibration(now=0)
        self.finish(engine)
        self.assertIn("ERROR", engine.outcome)
        self.assertIn("adjustment bound", engine.detail)
        self.assertEqual(engine.current_duty, 80.1)
        self.assertFalse(any(c[0].startswith("save_") for c in engine.bench.commands))

    def test_no_trim_response_is_detected(self):
        def frozen(r, bench):
            return replace(r, current=14.5, power=r.voltage*14.5) if bench._charger_on else r
        engine = self.make(ChangingBench(recipe(), frozen))
        engine.start_calibration(now=0)
        self.finish(engine)
        self.assertIn("No measurable response", engine.detail)
        self.assertIn("ERROR", engine.outcome)

    def test_wrong_trim_direction_is_detected(self):
        def reversed_response(r, bench):
            if bench._charger_on and bench._mode == "CV":
                current = 14.5 - (bench.current_duty-80)*.5
                return replace(r, current=current, power=r.voltage*current)
            return r
        engine = self.make(ChangingBench(recipe(), reversed_response))
        engine.start_calibration(now=0)
        self.finish(engine)
        self.assertIn("wrong direction", engine.detail)
        self.assertIn("ERROR", engine.outcome)

    def test_ambiguous_save_is_attempted_once_then_errors(self):
        bench = DemoBench(recipe())
        original = bench.charger_save
        def reject(axis):
            original(axis)
            return False
        bench.charger_save = reject
        engine = self.make(bench)
        engine.start_calibration(now=0)
        self.finish(engine)
        for n in range(10):
            engine.tick(200+n)
        saves = [c for c in bench.commands if c[0].startswith("save_")]
        self.assertEqual(saves, [("save_current",)])
        self.assertIn("do not retry", engine.detail)

    def test_failed_standalone_check_is_fail_not_incomplete(self):
        def high_current(r, b):
            return replace(r, current=15.3, power=r.voltage*15.3) if b._mode == "CV" and b._load_on else r
        engine = self.make(ChangingBench(recipe(), high_current))
        engine.start_test(now=0)
        self.finish(engine)
        self.assertEqual(engine.outcome, "SIMULATION — FAIL")
        self.assertEqual(engine.bench.can_tx_count, 0)
        self.assertEqual(engine.results[1]["status"], "FAIL")

    def test_reports_auto_saved_on_done_abort_and_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            for completion in ("done", "abort", "fault"):
                engine = self.make(report_dir=Path(folder))
                engine.unit_serial = '<script>alert("x")</script>'
                engine.operator = "A & B"
                engine.start_test(now=0)
                if completion == "done":
                    self.finish(engine)
                elif completion == "abort":
                    engine.stop()
                else:
                    engine.bench.can_tx_count += 1
                    engine.tick(.25)
                self.assertFalse(engine.report_error)
                for key, path in engine.report_paths.items():
                    self.assertTrue(Path(path).is_file(), key)
                record = json.loads(Path(engine.report_paths["json"]).read_text(encoding="utf-8"))
                self.assertEqual(record["unit_serial"], engine.unit_serial)
                self.assertEqual(record["recipe_revision"], 2)
                self.assertTrue(record["simulation"])
                self.assertIn("samples", record)
                document = Path(engine.report_paths["html"]).read_text(encoding="utf-8")
                self.assertNotIn('<script>', document)
                self.assertIn('&lt;script&gt;', document)
                self.assertIn("SIMULATION ONLY", document)
            self.assertEqual(len(list(Path(folder).glob("*.html"))), 3)

    def test_report_failure_visible_without_changing_measurements(self):
        with tempfile.TemporaryDirectory() as folder:
            bad_path = Path(folder) / "file"
            bad_path.write_text("not a directory")
            engine = self.make(report_dir=bad_path)
            engine.start_test(now=0)
            self.finish(engine)
            self.assertIn("Could not save report", engine.report_error)
            self.assertIn("Could not save report", engine.detail)
            self.assertEqual(len(engine.results), 3)

    def test_partial_report_write_does_not_advertise_successful_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = self.make(report_dir=Path(folder))
            original = os.replace
            def fail_html(source, destination):
                if Path(destination).suffix == ".html":
                    raise OSError("Disk write failure")
                return original(source, destination)
            engine.start_test(now=0)
            with patch("leanwatts_can.automation.os.replace", side_effect=fail_html):
                self.finish(engine)
            self.assertEqual(engine.report_paths, {})
            self.assertIn("Disk write failure", engine.report_error)
            self.assertEqual(list(Path(folder).glob("*.json")), [])

    def test_json_commit_published_after_both_companions(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = self.make(report_dir=Path(folder))
            original = os.replace
            published = []
            def check_order(source, destination):
                published.append(Path(destination).suffix)
                self.assertEqual(engine.report_paths, {})
                if Path(destination).suffix == ".json":
                    self.assertTrue(Path(destination).with_suffix(".csv").is_file())
                    self.assertTrue(Path(destination).with_suffix(".html").is_file())
                return original(source, destination)
            engine.start_test(now=0)
            with patch("leanwatts_can.automation.os.replace", side_effect=check_order):
                self.finish(engine)
            self.assertEqual(published, [".csv", ".html", ".json"])
            record = json.loads(Path(engine.report_paths["json"]).read_text(encoding="utf-8"))
            self.assertEqual(record["report_paths"], engine.report_paths)
            self.assertFalse(record["report_error"])

    def test_second_start_does_not_overlap(self):
        engine = self.make()
        engine.start_calibration(now=0)
        with self.assertRaisesRegex(ValueError, "current process"):
            engine.start_test(now=0)
        self.assertEqual(engine.operation, "calibration")

    def ramp_engine(self, transform=None, profile=None, settings=None):
        from leanwatts_can.tests.test_recipe_automation import ramp_recipe
        profile = profile or ramp_recipe()
        bench = ChangingBench(profile, transform) if transform else DemoBench(profile)
        return AutomationEngine(profile,"UNIT","Operator",bench=bench,settings=settings)

    def test_enabled_cutoff_uses_measured_bracket_and_no_can(self):
        engine = self.ramp_engine()
        engine.start_test(now=0)
        self.finish(engine)
        self.assertEqual(engine.outcome, "SIMULATION — PASS", engine.detail)
        result = engine.results[-1]
        self.assertEqual(result["programmed"],1.5)
        self.assertEqual(result["required"],1.1)
        self.assertEqual(result["first_off_measured_current_a"],0)
        self.assertGreater(result["measured"],1)
        self.assertAlmostEqual(result["observed_interval_a"][0],1.1)
        self.assertAlmostEqual(result["observed_interval_a"][1],1.2)
        self.assertTrue(result["shutdown_observed"])
        self.assertEqual(engine.bench.can_tx_count,0)
        commands=engine.bench.commands
        setups=[command for command in commands if command[0]=="load_setup"]
        self.assertEqual(setups,[("load_setup","CV",49)])
        self.assertIn(("load_transition_cc", 5.0), commands)
        first_step=next(i for i,c in enumerate(commands) if c[0]=="load_cc_step")
        self.assertFalse(any(c[0]=="load_off" for c in commands[first_step:-2]))

    def test_cutoff_interval_overlap_is_inconclusive_not_false_pass(self):
        from leanwatts_can.tests.test_recipe_automation import ramp_recipe
        profile=ramp_recipe()
        profile.end_current_tolerance=.025
        engine=self.ramp_engine(profile=profile)
        engine.start_test(now=0)
        self.finish(engine)
        self.assertEqual(engine.results[-1]["status"],"INDETERMINATE")
        self.assertEqual(engine.outcome,"SIMULATION — INCOMPLETE")

    def test_unreg_after_actual_cutoff_preserves_independent_shutdown_evidence(self):
        engine = self.ramp_engine(lambda r, b: replace(r, unregulated=b._terminated))
        engine.start_test(now=0)
        self.finish(engine)
        self.assertEqual(engine.outcome, "SIMULATION — PASS", engine.detail)
        result = engine.results[-1]
        self.assertTrue(result["shutdown_observed"])
        self.assertTrue(result["first_off"]["unregulated"])
        self.assertEqual(engine.bench.can_tx_count, 0)

    def test_unreg_without_low_output_cannot_step_or_claim_termination(self):
        from leanwatts_can.tests.test_recipe_automation import ramp_recipe
        profile = ramp_recipe()
        profile.termination_test["max_seconds"] = 5
        engine = self.ramp_engine(lambda r, b: replace(r, unregulated=engine.phase == "test_termination"), profile)
        engine.start_test(now=0)
        self.finish(engine)
        self.assertEqual(engine.outcome, "SIMULATION — FAIL", engine.detail)
        self.assertFalse(engine.termination_evidence["shutdown_observed"])
        self.assertEqual([c for c in engine.bench.commands if c[0] == "load_cc_step"], [])

    def test_minimum_current_without_shutdown_is_fail(self):
        def stays_on(r,b):
            if b._termination_ramping:
                return replace(r,voltage=b.recipe.end_voltage,current=b._setpoint,power=b.recipe.end_voltage*b._setpoint)
            return r
        engine=self.ramp_engine(stays_on)
        engine.start_test(now=0)
        self.finish(engine)
        self.assertEqual(engine.outcome,"SIMULATION — FAIL")
        self.assertIn("0.5 A",engine.results[-1]["detail"])
        self.assertIsNone(engine.results[-1]["measured"])

    def test_load_off_fault_and_stale_do_not_count_as_cutoff(self):
        transforms=[lambda r:replace(r,load_on=False,voltage=0,current=0,power=0),
                    lambda r:replace(r,fault="protection"),lambda r:replace(r,timestamp=r.timestamp-10)]
        for transform in transforms:
            engine=self.ramp_engine(lambda r,b,t=transform:t(r) if b._termination_ramping else r)
            engine.start_test(now=0)
            self.finish(engine)
            self.assertEqual(engine.outcome,"SIMULATION — ERROR")
            self.assertEqual(engine.results[-1]["status"],"ERROR")
            self.assertFalse(engine.termination_evidence["shutdown_observed"])
            self.assertEqual(engine.bench.can_tx_count,0)

    def test_cutoff_debounce_rejects_intermittent_low_output(self):
        from leanwatts_can.tests.test_recipe_automation import ramp_recipe
        profile=ramp_recipe()
        profile.termination_test["max_seconds"]=12
        def flicker(r,b):
            if b._terminated and r.sequence%2:
                return replace(r,voltage=b.recipe.end_voltage,current=b._setpoint,power=b.recipe.end_voltage*b._setpoint)
            return r
        engine=self.ramp_engine(flicker,profile)
        engine.start_test(now=0)
        self.finish(engine)
        self.assertEqual(engine.outcome,"SIMULATION — FAIL")
        self.assertIn("chatter",engine.results[-1]["detail"])
        self.assertFalse(engine.termination_evidence["shutdown_observed"])

    def test_cutoff_requires_continuous_input_capability(self):
        engine=self.ramp_engine()
        engine.bench.capabilities=replace(engine.bench.capabilities,termination_ramp=False)
        with self.assertRaisesRegex(ValueError,"continuous-input"):
            engine.start_test(now=0)
        self.assertEqual(engine.bench.commands,[])

    def test_optional_points_are_not_run_in_external_restart_sequence(self):
        from leanwatts_can.tests.test_recipe_automation import ramp_recipe
        profile = ramp_recipe()
        profile.test_plan = dict(voltage_regulation_currents=[3,6], current_regulation_voltages=[49,50], soak_seconds=2, soak_current=5)
        engine = self.ramp_engine(profile=profile)
        engine.start_test(now=0)
        self.finish(engine)
        self.assertEqual(engine.outcome, "SIMULATION — PASS", engine.detail)
        self.assertEqual(len(engine.results), 3)
        self.assertFalse(any(item["name"] == "Sustained-load stability" for item in engine.results))
        self.assertEqual(engine.bench.can_tx_count, 0)



    def test_real_read_latency_counts_against_measurement_freshness(self):
        engine=self.make()
        engine.bench.capabilities=replace(engine.bench.capabilities,simulation=False)
        engine.simulation=False
        with patch("leanwatts_can.automation.time.monotonic", return_value=0):
            engine.start_test(now=0)
        with patch("leanwatts_can.automation.time.monotonic",return_value=10):
            engine.tick(.25)
        self.assertEqual(engine.outcome,"ERROR")
        self.assertIn("stale",engine.detail)

    def test_float32_setpoint_readback_is_accepted_without_relaxing_measurement_limits(self):
        from leanwatts_can.tests.test_recipe_automation import ramp_recipe
        profile=ramp_recipe()
        profile.cc_test_load_voltage=49.9
        def quantized(r,b):
            return replace(r,load_setpoint=struct.unpack(">f",struct.pack(">f",r.load_setpoint))[0])
        engine=AutomationEngine(profile,"UNIT","Operator",bench=ChangingBench(profile,quantized))
        engine.start_test(now=0)
        self.finish(engine)
        self.assertEqual(engine.outcome,"SIMULATION — PASS",engine.detail)

    def test_stability_window_honors_duration_when_more_samples_are_needed(self):
        settings=replace(AutomationSettings.demo(),stable_samples=8,stable_seconds=2)
        engine=self.make(settings=settings)
        engine.start_test(now=0)
        self.finish(engine)
        self.assertEqual(engine.outcome,"SIMULATION — PASS",engine.detail)
        self.assertGreaterEqual(engine.results[1]["sample_count"],9)

    def test_acquire_failure_creates_error_report_without_unowned_stop_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            engine=self.make(report_dir=Path(folder))
            def unavailable(operation):
                raise RuntimeError("Port disconnected during startup")
            engine.bench.acquire=unavailable
            engine.start_calibration(now=0)
            self.assertFalse(engine.is_running)
            self.assertEqual(engine.outcome,"SIMULATION — ERROR")
            self.assertEqual(engine.bench.commands,[])
            self.assertIn("without ownership",engine.stop_outcome)
            record=json.loads(Path(engine.report_paths["json"]).read_text(encoding="utf-8"))
            self.assertIn("Port disconnected",record["detail"])
            self.assertEqual(record["samples"],[])

    def test_reports_preserve_required_programmed_observed_and_hardware_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            engine=self.ramp_engine()
            engine.report_dir=Path(folder)
            engine.bench.can_trace=[dict(action="previous_run",status="API ACCEPTED")]
            engine.bench.can_observations={"outputVoltage":58.4}
            engine.bench._can_observed_at={"outputVoltage":12.5}
            engine.bench.probe_result=dict(protocol_source="USER SUPPLIED PROTOCOL",instrument_writes=False)
            engine.start_test(now=0)
            engine.bench.can_trace.append(dict(action="evidence marker",status="NO TX"))
            self.finish(engine)
            record=json.loads(Path(engine.report_paths["json"]).read_text(encoding="utf-8"))
            evidence=record["hardware_evidence"]
            self.assertEqual(evidence["can_trace_total"],1)
            self.assertEqual(evidence["can_trace"][0]["action"],"evidence marker")
            self.assertEqual(evidence["load_probe"]["protocol_source"],"USER SUPPLIED PROTOCOL")
            self.assertEqual(record["results"][-1]["required"],1.1)
            self.assertEqual(record["results"][-1]["programmed"],1.5)
            self.assertAlmostEqual(record["results"][-1]["measured"],1.15)
            self.assertNotIn("can_trace",engine.snapshot())
            document=Path(engine.report_paths["html"]).read_text(encoding="utf-8")
            self.assertIn("<th>Required</th><th>Programmed*</th><th>Observed</th>",document)
            self.assertIn("<td>1.1</td><td>1.5</td><td>1.15</td>",document)
            self.assertIn("Cutoff bracket:",document)
            self.assertIn("not independently confirmed firmware readbacks",document)


if __name__ == "__main__":
    unittest.main()
