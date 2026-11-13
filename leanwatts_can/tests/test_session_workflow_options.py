"""Operator workflow selection with no physical instrument access."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

from leanwatts_can.session_page import SessionPage, SessionModeDialog
from leanwatts_can.session_store import SessionStore
from leanwatts_can.tests.test_session_ui_v8 import profile


class SessionWorkflowOptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.page = SessionPage(self.data, None, SessionStore(self.data))
        self.page.timer.stop()
        self.page.accounts.upsert_operator("OP", "Operator", "password", role="admin")
        operator = self.page.accounts.authenticate("OP", "password")
        self.page._session = self.page.store.start(operator)
        self.page._authenticated_operator = operator
        self.page.live_run_factory = Mock(return_value=Mock())
        self.page.live_readiness = Mock(return_value="")
        self.page.set_profiles([dict(recipe=profile().to_dict(), revision=1)])
        self.page.serial.setText("WO-S00430-1-001")
        # Keep start requests queued so only explicit worker tests can execute.
        self.page._executor.shutdown(wait=True)
        self.page._executor = Mock()
        self.page._executor.submit.side_effect = lambda *args, **kwargs: Future()

    def tearDown(self):
        self.page._future = None
        self.page.snapshot = None
        self.page.dispose()
        self.page.close()
        self.tmp.cleanup()

    def test_buttons_and_scanner_select_the_intended_workflow(self):
        with patch.object(self.page, "start") as start:
            self.page.calibrate.click()
            self.page.test_only.click()
            self.page.serial.returnPressed.emit()
        self.assertEqual([call.args for call in start.call_args_list],
                         [("calibration",), ("test_only",), ("calibration",)])
        self.assertFalse(hasattr(self.page, "calibrate_and_test"))
        self.assertEqual(self.page.test_only.text(), "Test")

    def test_test_only_never_builds_calibration_factory_or_selects_seed(self):
        recipe = profile()
        recipe.device_settings = {}
        self.page.set_profiles([dict(recipe=recipe.to_dict(), revision=1, production_ready=False)])
        self.page.live_readiness.reset_mock()
        with patch("leanwatts_can.session_page.select_session_seed", side_effect=AssertionError("No calibration seed for tests")):
            self.page.start("test_only")
        self.assertEqual([call.args for call in self.page.live_readiness.call_args_list], [("test_only",)])
        self.assertEqual([call.args[1] for call in self.page.live_run_factory.call_args_list], ["test_only"])
        args = self.page._executor.submit.call_args.args
        self.assertIsNone(args[-3])
        self.assertTrue(callable(args[-2]))
        self.assertIs(args[-1], True)
        self.assertIs(self.page._executor.submit.call_args.kwargs["run_calibration"], False)
        self.assertIsNone(self.page._executor.submit.call_args.kwargs["starting_point"])
        unit = self.page.store.active()["units"][-1]
        self.assertEqual(unit["workflow_mode"], "test_only")
        self.assertEqual(unit["calibration_result"], "NOT RUN")

    def test_calibration_only_ignores_test_setup_and_does_not_build_test_factory(self):
        self.page.live_readiness.side_effect = lambda process: "Complete the test load setup." if process == "test_only" else ""
        self.page._lock_controls()
        self.assertTrue(self.page.calibrate.isEnabled())
        self.assertFalse(self.page.test_only.isEnabled())
        self.assertIn("test load setup", self.page.test_only.toolTip())
        self.page.live_readiness.reset_mock()
        self.page.start()
        self.assertEqual([call.args for call in self.page.live_readiness.call_args_list], [("calibration",)])
        self.assertEqual([call.args[1] for call in self.page.live_run_factory.call_args_list], ["calibration"])
        args = self.page._executor.submit.call_args.args
        self.assertIsNone(args[-2])
        self.assertIs(args[-1], False)
        self.page.live_run_factory.return_value.assert_not_called()

    def test_calibration_only_validation_does_not_require_a_post_test_plan(self):
        recipe = profile()
        recipe.termination_test = {"enabled": False}
        self.page.set_profiles([dict(recipe=recipe.to_dict(), revision=1, production_ready=True)])
        self.assertEqual(self.page._readiness("calibration"), "")
        self.assertTrue(self.page._readiness("calibration_test"))

    def test_combined_workflow_is_no_longer_an_operator_action(self):
        self.page.live_readiness.reset_mock()
        self.page.start("calibration_test")
        self.assertIn("Choose Parameters + Calibration or Test", self.page.detail.text())
        self.page.live_run_factory.assert_not_called()
        self.page._executor.submit.assert_not_called()

    def test_draft_blocks_calibration_but_allows_valid_independent_tests(self):
        self.page.set_profiles([dict(recipe=profile().to_dict(), revision=1, production_ready=False)])
        self.assertFalse(self.page.calibrate.isEnabled())
        self.assertTrue(self.page.test_only.isEnabled())
        for process in ("calibration",):
            self.page.start(process)
            self.assertIn("Draft", self.page.detail.text())
        self.page.live_run_factory.assert_not_called()
        self.page._executor.submit.assert_not_called()

    def test_both_workflows_block_reentry_and_share_stop(self):
        for process in ("calibration", "test_only"):
            with self.subTest(process=process):
                self.page.start(process)
                self.assertFalse(self.page.calibrate.isEnabled())
                self.assertFalse(self.page.test_only.isEnabled())
                self.assertTrue(self.page.stop_button.isEnabled())
                calls = self.page._executor.submit.call_count
                self.page.start("calibration")
                self.page.start("test_only")
                self.page.start("calibration_test")
                self.assertEqual(self.page._executor.submit.call_count, calls)
                self.page.stop_button.click()
                self.assertTrue(self.page._cancel.is_set())
                self.assertFalse(self.page.stop_button.isEnabled())
                self.page._future = None
                self.page.store.finish_unit(self.page._attempt_id,
                    dict(outcome="STOPPED", calibration_result="NOT RUN", test_result="NOT RUN"))
                self.page._lock_controls()

    def test_worker_receives_frozen_workflow_choice(self):
        for run_tests in (False, True):
            with self.subTest(run_tests=run_tests), patch("leanwatts_can.session_page.ProductionFlow") as flow:
                flow.return_value.snapshot.return_value = {"is_running": True}
                cal_factory = Mock()
                test_factory = Mock() if run_tests else None
                self.page._run_tests = not run_tests
                self.page._start_worker(profile(), "WO-S00430-1-001", {"id": "OP"}, 1,
                                        cal_factory, test_factory, run_tests)
                self.assertIs(flow.call_args.kwargs["run_tests"], run_tests)
                self.assertIs(flow.call_args.args[-1], test_factory)
                flow.return_value.start.assert_called_once_with()
                cal_factory.assert_not_called()

    def test_start_freezes_first_successful_session_reference_for_worker(self):
        from leanwatts_can.tests.test_session_seed import completed_unit
        r = profile()
        attempt = self.page.store.begin_unit("WO-S00430-1-000", r, 1, run_tests=False)
        self.page.store.finish_unit(attempt, completed_unit("WO-S00430-1-000", recipe=r))
        self.page.start("calibration")
        seed = self.page._executor.submit.call_args.kwargs["starting_point"]
        self.assertEqual(seed["source_serial"], "WO-S00430-1-000")
        self.assertEqual(seed["current_duty"], 69.5)
        self.assertEqual(seed["voltage_duty"], 81.1)

    def test_bad_serial_blocks_all_workflows_before_factories(self):
        self.page.serial.setText("UNIT-1")
        for process in ("calibration", "test_only"):
            self.page.start(process)
            self.assertIn("Other number", self.page.detail.text())
        self.page.live_run_factory.assert_not_called()
        self.page._executor.submit.assert_not_called()

    def test_other_number_choice_is_recorded_frozen_and_disabled_while_busy(self):
        self.page.serial.setText("  CUSTOM-007  ")
        self.page.other_number.click()
        self.assertTrue(self.page.other_number.isChecked())
        self.assertEqual(self.page.serial.placeholderText(), "Enter other charger number")
        self.page.start("test_only")
        self.assertIs(self.page._executor.submit.call_args.kwargs["serial_override"], True)
        self.assertEqual(self.page._executor.submit.call_args.args[2], "CUSTOM-007")
        self.assertTrue(self.page.store.active()["units"][-1]["serial_override"])
        self.assertFalse(self.page.other_number.isEnabled())
        self.page.other_number.click()
        self.assertTrue(self.page.other_number.isChecked())

    def test_other_number_resets_after_finished_attempt_without_clearing_result(self):
        self.page.serial.setText("CUSTOM-007")
        self.page.other_number.click()
        result = {"is_running": False, "stage": "Finished", "run_calibration": False,
                  "run_tests": True, "unit_result": {"outcome": "FAIL"}}
        future = Future()
        future.set_result(result)
        self.page._future = future
        self.page.refresh()
        self.assertFalse(self.page.other_number.isChecked())
        self.assertEqual(self.page.serial.placeholderText(), "WO-S00430-1-001")
        self.assertEqual(self.page.snapshot, result)
        self.assertIn("Other number", self.page._readiness("test_only"))

    def test_other_number_resets_on_session_close(self):
        self.page.other_number.click()
        self.page._close_session()
        self.assertFalse(self.page.other_number.isChecked())
        self.assertIsNone(self.page.store.active())

    def test_calibration_completion_displays_not_run_and_complete_progress(self):
        self.page._render_run({"is_running": False, "run_tests": False,
            "stage": "Calibration completed", "unit_result": {"outcome": "CALIBRATED"},
            "stage_details": [{"name": name, "status": "PASS"} for name in self.page.STAGES[:5]]})
        self.assertEqual([self.page.results.item(row, 4).text() for row in range(5, 8)], ["NOT RUN"] * 3)
        self.assertEqual(self.page.progress.value(), 100)
        self.assertIn("NOT RUN", self.page.stage_info.text())
        self.assertTrue(self.page.report_status.text().startswith("CALIBRATED"))

    def test_full_workflow_progress_waits_for_post_tests(self):
        self.page._render_run({"is_running": True, "run_tests": True,
            "stage_details": [{"name": name, "status": "PASS"} for name in self.page.STAGES[:5]]})
        self.assertEqual(self.page.progress.value(), round(5 / 8 * 100))
        self.assertEqual(self.page.results.item(5, 4).text(), "Pending")

    def test_test_only_progress_counts_only_test_stages(self):
        self.page._render_run({"is_running": False, "run_calibration": False, "run_tests": True,
            "stage_details": [{"name": name, "status": "PASS"} for name in self.page.STAGES[5:]],
            "unit_result": {"outcome": "PASS"}})
        self.assertEqual(self.page.progress.value(), 100)
        self.assertEqual([self.page.results.item(row, 4).text() for row in range(5)], ["NOT RUN"] * 5)
        self.assertIn("Parameters and calibration: NOT RUN", self.page.stage_info.text())

    def test_session_totals_separate_calibrated_units_from_passes(self):
        self.page._session.pop("unit_summaries", None)
        self.page._session["units"] = [{"outcome": "CALIBRATED"}, {"outcome": "PASS"}, {"outcome": "FAIL"}]
        self.page._render_session()
        self.assertEqual(self.page.session_totals.text(), "3 chargers  ·  1 calibrated  ·  1 pass  ·  1 unsuccessful")

    def both_required(self):
        self.page.store.close()
        self.page._session = self.page.store.start(self.page._authenticated_operator, mode="both_required")
        self.page._lock_controls()

    def complete_calibration(self):
        from leanwatts_can.tests.test_session_pairing import calibrated
        attempt = self.page.store.begin_unit(self.page.serial.text(), profile(), 1, run_tests=False,
                                            serial_override=self.page.other_number.isChecked())
        self.page.store.finish_unit(attempt, calibrated())
        self.page._session = self.page.store.active()
        self.page._lock_controls()

    def test_session_mode_dialog_is_clear_and_defaults_to_independent(self):
        choice = SessionModeDialog()
        self.assertEqual(choice.mode, "independent")
        self.assertEqual([choice.mode_select.itemText(i) for i in range(choice.mode_select.count())],
                         ["Parameters + Calibration only", "Test only", "Both independently", "Both mandatorily"])
        choice.mode_select.setCurrentIndex(3)
        self.assertEqual(choice.mode, "both_required")
        choice.close()

    def test_both_required_disables_test_until_calibration_then_freezes_identity(self):
        self.both_required()
        self.assertTrue(self.page.calibrate.isEnabled())
        self.assertFalse(self.page.test_only.isEnabled())
        self.assertIn("before Test", self.page.test_only.toolTip())
        self.complete_calibration()
        self.assertTrue(self.page.test_only.isEnabled())
        self.assertFalse(self.page.serial.isEnabled())
        self.assertFalse(self.page.variant.isEnabled())
        self.assertTrue(self.page.close_session.isEnabled())
        self.page.serial.setText("WO-S00430-1-002")
        self.assertEqual(self.page.serial.text(), "WO-S00430-1-001")
        self.page.start("test_only")
        self.assertEqual(self.page.live_run_factory.call_args.kwargs, {"test_start_method": "external_restart"})

    def test_updated_saved_recipe_does_not_replace_pending_frozen_recipe(self):
        self.both_required()
        self.complete_calibration()
        revised = profile()
        revised.end_voltage = 57.3
        self.page.set_profiles([dict(recipe=revised.to_dict(), revision=2)])
        current = self.page.records[self.page.variant.currentIndex()]
        self.assertEqual(current["recipe"], profile().to_dict())
        self.assertEqual(current["revision"], 1)
        self.assertIn("Session copy", self.page.variant.currentText())
        self.assertEqual(self.page._readiness("test_only"), "")

    def test_custom_serial_and_close_remain_available_after_stopped_required_attempt(self):
        self.both_required()
        self.page.serial.setText("CUSTOM-007")
        self.page.other_number.click()
        self.page.start("calibration")
        self.page.store.finish_unit(self.page._attempt_id, dict(outcome="STOPPED",
            calibration_result="FAIL", test_result="NOT RUN"))
        self.page._future = None
        self.page._session = self.page.store.active()
        self.page._reset_serial_mode()
        self.page._lock_controls()
        self.assertTrue(self.page.other_number.isChecked())
        self.assertEqual(self.page.serial.text(), "CUSTOM-007")
        self.assertTrue(self.page.calibrate.isEnabled())
        self.assertFalse(self.page.test_only.isEnabled())
        self.assertTrue(self.page.close_session.isEnabled())
        session_id = self.page._session["session_id"]
        self.page._close_session()
        self.assertIsNone(self.page.store.active())
        closed = next(record for record in self.page.store.list_sessions() if record["session_id"] == session_id)
        self.assertEqual(closed["completion_state"], "INCOMPLETE")

    def test_successful_pair_unlocks_next_unit_and_history_counts_one_charger(self):
        from leanwatts_can.tests.test_session_pairing import tested
        from leanwatts_can.session_history import SessionHistoryPage
        self.both_required()
        self.complete_calibration()
        attempt = self.page.store.begin_unit(self.page.serial.text(), profile(), 1, run_calibration=False)
        self.page.store.finish_unit(attempt, tested())
        self.page._session = self.page.store.active()
        self.page.refresh()
        self.assertTrue(self.page.serial.isEnabled())
        self.assertTrue(self.page.variant.isEnabled())
        self.assertIn("1 chargers", self.page.session_totals.text())
        self.assertIn("1 pass", self.page.session_totals.text())
        history = SessionHistoryPage(self.data, self.page.store)
        row = next(i for i, record in enumerate(history._shown)
                   if record["session_id"] == self.page._session["session_id"])
        self.assertEqual(history.table.item(row, 3).text(), "1")
        history.table.selectRow(row)
        self.assertEqual(history.units.rowCount(), 1)
        self.assertEqual(history.units.item(0, 3).text(), "PASS")
        self.assertEqual(history.units.item(0, 4).text(), "PASS")
        history.close()

    def test_required_test_displays_completed_calibration_and_native_test_names(self):
        self.both_required()
        self.complete_calibration()
        self.page._attempt_id = self.page.store.begin_unit(self.page.serial.text(), profile(), 1, run_calibration=False)
        self.page._session = self.page.store.active()
        self.page._render_run(dict(is_running=True, run_calibration=False, run_tests=True,
            stage_details=[dict(name="Current calibration", status="NOT RUN"),
                           dict(name="Open-circuit voltage", after=57.3, status="PASS", duration_seconds=5),
                           dict(name="Full current", after=20, status="PASS", duration_seconds=21),
                           dict(name="End current / observed shutdown", after=1.1, status="PASS", duration_seconds=8)]))
        self.assertEqual(self.page.results.item(3, 4).text(), "PASS")
        self.assertEqual(self.page.results.item(4, 4).text(), "PASS")
        self.assertEqual(self.page.results.item(5, 3).text(), "5 s")
        self.assertEqual(self.page.results.item(6, 3).text(), "21 s")
        self.assertEqual(self.page.results.item(7, 3).text(), "8 s")

    def test_single_process_modes_disable_other_button_and_block_direct_start(self):
        for mode, allowed, blocked in (("calibration_only", "calibration", "test_only"),
                                      ("test_only", "test_only", "calibration")):
            with self.subTest(mode=mode):
                self.page.store.close()
                self.page._session = self.page.store.start(self.page._authenticated_operator, mode=mode)
                self.page._lock_controls()
                self.assertEqual(self.page.calibrate.isEnabled(), allowed == "calibration")
                self.assertEqual(self.page.test_only.isEnabled(), allowed == "test_only")
                self.page.live_run_factory.reset_mock()
                self.page._executor.submit.reset_mock()
                self.page.start(blocked)
                self.assertIn("This session is", self.page.detail.text())
                self.page.live_run_factory.assert_not_called()
                self.page._executor.submit.assert_not_called()

    def test_test_only_scanner_and_ready_hint_use_allowed_process(self):
        self.page.store.close()
        self.page._session = self.page.store.start(self.page._authenticated_operator, mode="test_only")
        self.page._last_readiness = None
        self.page.refresh()
        self.assertNotIn("Start a new session", self.page.detail.text())
        with patch.object(self.page, "start") as start:
            self.page.serial.returnPressed.emit()
        start.assert_called_once_with("test_only")
        self.assertEqual(self.page.STAGES[5:], ("Open-circuit voltage test", "Full-current test", "End-current cutoff test"))


if __name__ == "__main__":
    unittest.main()
