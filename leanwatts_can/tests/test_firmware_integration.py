"""Controller/profile checks with fake programmers; no USB writes permitted."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from leanwatts_can.gdlink_cli import CLITarget, CLI_VERSION, TOOL_FILES
from leanwatts_can.gui import CalibrationWindow
from leanwatts_can.recipe_page import atomic_json
from leanwatts_can.tests.test_production_sessions import production_recipe


class FirmwareIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.tool = dict(cli_path=str(self.folder / "GDLink_CLI.exe"), cli_version=CLI_VERSION,
                         cli_sha256=TOOL_FILES["GDLink_CLI.exe"], tool_files=TOOL_FILES)
        atomic_json(self.folder / "profiles.json", [dict(recipe=production_recipe().to_dict(), revision=1)])
        self.guards = [patch("leanwatts_can.maynuo.MaynuoLoad.open", side_effect=AssertionError("No load")),
                       patch("leanwatts_can.can_monitor.MonitorClient.open", side_effect=AssertionError("No CAN")),
                       patch("leanwatts_can.firmware_controller.discover_programmers", side_effect=AssertionError("No auto USB discovery")),
                       patch("leanwatts_can.firmware_controller.ProgrammingJob", side_effect=AssertionError("No real programmer")),
                       patch("leanwatts_can.firmware_controller.inspect_cli_tool", return_value=self.tool)]
        for guard in self.guards:
            guard.start()
        self.window = CalibrationWindow(self.folder)
        self.window.legacy_page.mode_select.setCurrentIndex(2)
        self.controller = self.window.firmware_controller
        self.target = CLITarget("USB\\VID_28E9&PID_0698\\FIXTURE1", "FIXTURE1")
        self.image = self.folder / "approved.bin"
        self.image.write_bytes(b"\x00\x20\x00\x20\x01\x01\x00\x08" + b"\x00" * (65536 - 8))
        self.values = dict(declared_mcu="GD32F103C8T6", flash_start=0x08000000,
            flash_size_kib=64, speed_khz=1000, approved=True, firmware_path=str(self.image),
            cli_path=self.tool["cli_path"])

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        for guard in self.guards:
            guard.stop()
        self.temp.cleanup()

    def prepare(self):
        page = self.window.operator_page
        page.accounts.upsert_operator("ADMIN", "Administrator", "password", role="admin")
        operator = page.accounts.authenticate("ADMIN", "password")
        self.controller._operator = operator
        self.window.firmware_page.variant.setCurrentIndex(0)
        self.controller.save_profile(self.values, production_recipe(), 1, self.target, operator)
        self.window.firmware_page.set_targets([self.target])
        return operator

    def payload(self):
        return dict(serial="WO-S00430-1-001", serial_override=False, target=self.target,
                    ac_off_confirmed=True, fixture_power_confirmed=True)

    def wait_finished(self):
        deadline = time.monotonic() + 5
        while self.controller.busy and time.monotonic() < deadline:
            self.app.processEvents()
            self.controller.refresh()
            time.sleep(.005)
        self.assertFalse(self.controller.busy)

    def test_no_hardware_on_open_and_no_program_without_login(self):
        self.assertEqual(self.window.tabs.count(), 8)
        self.controller.start(self.payload())
        self.assertFalse(self.controller.busy)
        self.assertIn("Sign in", self.window.firmware_page.status.text())
        self.assertFalse((self.folder / "firmware_runs").exists())

    def test_admin_profile_freezes_image_and_other_roles_cannot_change_it(self):
        with self.assertRaisesRegex(ValueError, "administrator"):
            self.controller.save_profile(self.values, production_recipe(), 1, self.target, dict(id="OP", role="operator"))
        self.prepare()
        saved = self.controller._profiles[production_recipe().name]
        self.assertEqual(Path(saved["firmware_path"]).read_bytes(), self.image.read_bytes())
        self.assertEqual(saved["firmware_sha256"], hashlib.sha256(self.image.read_bytes()).hexdigest())
        self.assertFalse(saved["target_mcu_verified"])
        self.assertEqual(saved["tool_files"], TOOL_FILES)
        self.assertEqual(saved["backend"], "gdlink_cli")

    def test_image_tampering_blocks_job_and_is_recorded(self):
        self.prepare()
        saved = self.controller._profiles[production_recipe().name]
        Path(saved["firmware_path"]).write_bytes(b"changed")
        with patch("leanwatts_can.firmware_controller.ProgrammingJob") as job:
            self.controller.start(self.payload())
            self.wait_finished()
            job.assert_not_called()
        self.assertEqual(self.controller.snapshot["status"], "FAILED")
        self.assertFalse(self.controller.snapshot["submitted"])
        self.assertTrue(Path(self.controller.snapshot["report_path"]).is_file())

    def test_variant_change_requires_new_approval(self):
        self.prepare()
        records = copy.deepcopy(self.window.firmware_page.identity.records)
        records[0]["recipe"]["name"] = "Different variant"
        self.window.firmware_page.set_profiles(records)
        self.controller.start(self.payload())
        self.assertFalse(self.controller.busy)
        self.assertIn("approve firmware", self.window.firmware_page.status.text())

    def test_firmware_context_is_independent_and_parameter_edits_do_not_change_approval(self):
        self.prepare()
        self.assertIsNone(self.window.session_store.active())
        selected = self.controller._context()[0].name
        self.window.operator_page.variant.setCurrentIndex(-1)
        self.assertEqual(self.controller._context()[0].name, selected)
        records = copy.deepcopy(self.window.firmware_page.identity.records)
        records[0]["recipe"]["cc_current"] += 1
        records[0]["revision"] += 1
        self.window.firmware_page.set_profiles(records)
        self.controller.refresh()
        self.assertTrue(self.window.firmware_page._profile["approved"])
        self.assertEqual(self.controller._context()[1]["revision"], 2)

    def test_named_draft_can_have_firmware_without_enabling_calibration(self):
        operator = self.prepare()
        from leanwatts_can.core import Recipe
        draft = Recipe(name="Firmware only")
        record = dict(recipe=draft.to_dict(), revision=1, production_ready=False)
        self.window.firmware_page.set_profiles([record])
        self.controller.save_profile(self.values, draft, 1, self.target, operator)
        self.controller.refresh()
        self.window.firmware_page.serial.setText("WO-S00430-1-001")
        self.assertTrue(self.window.firmware_page.start_button.isEnabled())
        self.assertIsNone(self.window.session_store.active())
        self.assertEqual(self.controller._context()[0].cc_current, 0)

    def test_own_sign_in_does_not_start_an_auto_calibration_session(self):
        operator = self.prepare()
        self.controller._operator = None
        with patch("leanwatts_can.firmware_controller.sign_in", return_value=operator) as login:
            self.controller.sign_in()
            login.assert_called_once()
        self.assertEqual(self.controller._context()[2]["id"], "ADMIN")
        self.assertIsNone(self.window.operator_page._session)
        self.assertIsNone(self.window.session_store.active())

    def test_physical_confirmations_and_exact_programmer_are_required(self):
        self.prepare()
        for flag in ("ac_off_confirmed", "fixture_power_confirmed"):
            payload = self.payload()
            payload[flag] = False
            self.controller.start(payload)
            self.assertFalse(self.controller.busy)
            self.assertIn("Confirm charger AC", self.window.firmware_page.status.text())
        payload = self.payload()
        payload["target"] = CLITarget("other", "OTHER01")
        self.controller.start(payload)
        self.assertFalse(self.controller.busy)
        self.assertIn("programmer approved", self.window.firmware_page.status.text())
        self.assertFalse((self.folder / "firmware_runs").exists())

    def test_unsupported_tool_cannot_be_approved(self):
        self.prepare()
        operator = self.controller._operator
        before = self.controller.profile_path.read_bytes()
        with patch("leanwatts_can.firmware_controller.inspect_cli_tool", side_effect=ValueError("Unsupported vendor file")):
            with self.assertRaisesRegex(ValueError, "Unsupported vendor"):
                self.controller.save_profile(self.values, production_recipe(), 1, self.target, operator)
        self.assertEqual(self.controller.profile_path.read_bytes(), before)

    def test_programming_locks_other_workflows_and_keeps_monitoring_after_stop(self):
        self.prepare()
        submitted, finish = threading.Event(), threading.Event()
        class FakeJob:
            def __init__(self, target, profile, image, **kwargs):
                self.kwargs = kwargs
                assert kwargs["ac_off_confirmed"] is True
                assert Path(kwargs["evidence_dir"]).is_dir()
            def run(self):
                self.kwargs["event_sink"](dict(phase="programming", detail="Fake submission", submitted=True))
                submitted.set()
                if not finish.wait(3):
                    raise RuntimeError("Test release signal missing")
                return dict(status="VERIFIED", detail="Fake full flash readback matches",
                            submitted=True, independent_readback_verified=True, target_mcu_verified=True,
                            console_closed=True, disconnect_acknowledged=False)
        with patch("leanwatts_can.firmware_controller.ProgrammingJob", FakeJob):
            self.controller.start(self.payload())
            self.assertTrue(submitted.wait(2))
            self.controller.refresh()
            self.assertTrue(self.controller.busy)
            self.assertFalse(self.window.tabs.isTabEnabled(self.window.tabs.indexOf(self.window.operator_page)))
            self.assertFalse(self.window.tabs.isTabEnabled(self.window.tabs.indexOf(self.window.test_page)))
            self.assertIn("firmware", self.window.operator_page.start_guard())
            self.assertFalse(self.window.firmware_page.stop_button.isEnabled())
            self.controller.stop()
            self.assertFalse(self.controller._cancel.is_set())
            finish.set()
            self.wait_finished()
        result = self.controller.snapshot
        self.assertEqual(result["status"], "VERIFIED")
        self.assertTrue(result["independent_readback_verified"])
        self.assertTrue(result["target_mcu_verified"])
        self.assertTrue(result["console_closed"])
        self.assertFalse(result["disconnect_acknowledged"])
        self.assertFalse(result["calibration_started"])
        self.assertIsNone(self.window.session_store.active())
        self.assertTrue(self.window.tabs.isTabEnabled(self.window.tabs.indexOf(self.window.operator_page)))
        self.assertFalse(self.window.test_controller.busy)
        report = json.loads(Path(result["report_path"]).read_text())
        self.assertEqual(report["serial"], "WO-S00430-1-001")

    def test_timed_out_vendor_process_keeps_all_workflows_locked_until_exit(self):
        self.prepare()
        fake = Mock()
        fake.run.return_value = dict(status="UNCONFIRMED", submitted=True, process_running=True,
                                      detail="Vendor process still running")
        with patch("leanwatts_can.firmware_controller.ProgrammingJob", return_value=fake), \
                patch("leanwatts_can.firmware_controller.console_process_running", return_value=True) as running:
            self.controller.start(self.payload())
            deadline = time.monotonic() + 3
            while self.controller._future is not None and time.monotonic() < deadline:
                self.controller.refresh()
                time.sleep(.005)
            self.assertIsNone(self.controller._future)
            self.assertTrue(self.controller.busy)
            self.assertFalse(self.window.tabs.isTabEnabled(self.window.tabs.indexOf(self.window.operator_page)))
            self.assertFalse(self.window.firmware_page.stop_button.isEnabled())
            self.controller.stop()
            self.assertFalse(self.controller._cancel.is_set())
            running.return_value = False
            self.controller.refresh()
            self.assertFalse(self.controller.busy)
            self.assertEqual(self.controller.snapshot["status"], "UNCONFIRMED")
            self.assertTrue(self.window.tabs.isTabEnabled(self.window.tabs.indexOf(self.window.operator_page)))


if __name__ == "__main__":
    unittest.main()
