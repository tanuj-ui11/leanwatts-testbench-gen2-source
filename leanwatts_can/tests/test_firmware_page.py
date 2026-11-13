"""Firmware UI emits requests only; the host controls authorization and I/O."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QDialog, QScrollArea
from leanwatts_can.firmware_page import FirmwarePage, FirmwareProfileDialog, FirmwareProgramConfirmDialog
from leanwatts_can.gdlink_cli import CLITarget


TARGET = dict(identity="fixture-pnp", serial_number="FIXTURE01", description="GD-Link V3")
PROFILE = dict(backend="gdlink_cli", firmware_path="C:/approved/charger.bin", declared_mcu="GD32F103C8T6",
               flash_start=0x08000000, flash_size_kib=64, speed_khz=1000, approved=True)
PREFLIGHT = dict(ac_off_confirmed=True, fixture_power_confirmed=True)


class FirmwarePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.page = FirmwarePage()
        self.addCleanup(self.page.close)
        self.page.resize(1268, 640)
        self.page.show()
        self.app.processEvents()

    def ready(self):
        self.page.refresh_context(dict(name="Charger variant"), dict(name="Operator", id="7"))
        self.page.set_targets([TARGET])
        self.page.set_profile(PROFILE)
        self.page.serial.setText("WO-S00430-1-001")

    def test_requires_context_target_approval_and_serial(self):
        self.assertFalse(self.page.start_button.isEnabled())
        self.ready()
        self.assertTrue(self.page.start_button.isEnabled())
        self.page.set_targets([])
        self.assertFalse(self.page.start_button.isEnabled())
        self.page.set_targets([TARGET])
        self.page.set_profile({**PROFILE, "approved": False})
        self.assertFalse(self.page.start_button.isEnabled())

    def test_discovery_setup_program_are_signals_only(self):
        self.ready()
        events = []
        self.page.discover_requested.connect(lambda: events.append("discover"))
        self.page.setup_requested.connect(lambda: events.append("setup"))
        self.page.program_requested.connect(events.append)
        self.page.find_button.click()
        self.page.setup_button.click()
        with patch.object(self.page, "_confirm_program", return_value=PREFLIGHT):
            self.page.start_button.click()
        self.assertEqual(events[:2], ["discover", "setup"])
        self.assertEqual(events[2]["serial"], "WO-S00430-1-001")
        self.assertEqual(events[2]["target"]["identity"], TARGET["identity"])
        self.assertFalse(events[2]["serial_override"])
        self.assertTrue(events[2]["ac_off_confirmed"])
        self.assertTrue(events[2]["fixture_power_confirmed"])

    def test_backend_target_dataclass_is_preserved_for_controller(self):
        target = CLITarget(**TARGET)
        self.ready()
        self.page.set_targets([target])
        self.assertIsInstance(self.page.selected_target, CLITarget)
        received = []
        self.page.program_requested.connect(received.append)
        with patch.object(self.page, "_confirm_program", return_value=PREFLIGHT):
            self.page.start_button.click()
        self.assertIsInstance(received[0]["target"], CLITarget)
        self.assertEqual(received[0]["target"].identity, TARGET["identity"])

    def test_stop_allowed_before_submission_then_monitoring_continues(self):
        self.ready()
        stopped = []
        self.page.stop_requested.connect(lambda: stopped.append(True))
        self.page.set_busy(True)
        self.assertTrue(self.page.stop_button.isEnabled())
        self.page.stop_button.click()
        self.assertEqual(stopped, [True])
        self.page.update_snapshot(dict(stage="Waiting for result", submitted=True, elapsed_seconds=2,
                                       events=["File submitted; monitoring programmer result"]))
        self.assertFalse(self.page.stop_button.isEnabled())
        self.assertIn("cannot be interrupted", self.page.status.text())
        self.page.stop_button.click()
        self.assertEqual(stopped, [True])
        self.page.update_snapshot(dict(stage="Still waiting", submitted=False))
        self.assertFalse(self.page.stop_button.isEnabled())
        self.assertFalse(self.page.find_button.isEnabled())
        self.assertFalse(self.page.targets.isEnabled())

    def test_custom_serial_reset_result_label_and_no_whole_page_scroll(self):
        self.ready()
        self.page.serial.setText("CUSTOM")
        self.assertFalse(self.page.start_button.isEnabled())
        self.page.other_number.setChecked(True)
        self.assertTrue(self.page.start_button.isEnabled())
        self.page.set_busy(True)
        self.page.update_snapshot(dict(status="VERIFIED", elapsed_seconds=8.4))
        self.page.set_busy(False)
        self.assertEqual(self.page.result.value.text(), "Programmed · readback verified")
        self.assertTrue(self.page.other_number.isChecked())
        self.page.serial.setText("NEXT CUSTOM")
        self.assertFalse(self.page.other_number.isChecked())
        self.assertEqual(self.page.findChildren(QScrollArea), [])
        self.assertLessEqual(self.page.minimumSizeHint().height(), 640)

    def test_profile_dialog_uses_verified_defaults_and_requires_explicit_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "charger.bin"
            image.write_bytes(b"example only")
            cli = Path(temporary) / "GDLink_CLI.exe"
            cli.write_bytes(b"not executed")
            dialog = FirmwareProfileDialog({}, TARGET)
            self.addCleanup(dialog.close)
            self.assertEqual(dialog.flash_start.text(), "0x08000000")
            self.assertEqual(dialog.flash_size.text(), "64")
            self.assertEqual(dialog.declared_mcu.text(), "GD32F103C8T6")
            self.assertEqual(dialog.speed.text(), "1000")
            dialog.firmware_path.setText(str(image))
            dialog._save()
            self.assertIsNone(dialog.profile)
            self.assertIn("GDLink_CLI.exe", dialog.error.text())
            dialog.cli_path.setText(str(cli))
            dialog._save()
            self.assertIsNone(dialog.profile)
            self.assertIn("Confirm", dialog.error.text())
            dialog.confirm.setChecked(True)
            dialog._save()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(dialog.profile["flash_start"], 0x08000000)
            self.assertEqual(dialog.profile["flash_size_kib"], 64)
            self.assertEqual(dialog.profile["programmer_serial"], TARGET["serial_number"])
            self.assertNotIn("config_text", dialog.profile)

    def test_confirmation_is_required_for_each_unit_and_cancel_emits_nothing(self):
        dialog = FirmwareProgramConfirmDialog("WO-S00430-1-001", PROFILE)
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.program_button.isEnabled())
        dialog.ac_off.setChecked(True)
        self.assertFalse(dialog.program_button.isEnabled())
        dialog.fixture_power.setChecked(True)
        self.assertTrue(dialog.program_button.isEnabled())
        next_dialog = FirmwareProgramConfirmDialog("WO-S00430-1-002", PROFILE)
        self.addCleanup(next_dialog.close)
        self.assertFalse(next_dialog.program_button.isEnabled())
        self.ready()
        events = []
        self.page.program_requested.connect(events.append)
        with patch.object(self.page, "_confirm_program", return_value=None):
            self.page.start_button.click()
        self.assertEqual(events, [])

    def test_unconfirmed_running_console_keeps_stop_and_other_controls_disabled(self):
        self.ready()
        self.page.set_busy(True)
        self.page.update_snapshot(dict(status="UNCONFIRMED", process_running=True))
        self.assertFalse(self.page.start_button.isEnabled())
        self.assertFalse(self.page.stop_button.isEnabled())
        self.assertEqual(self.page.stop_button.text(), "WAIT")

    def test_readiness_explains_missing_inputs_and_clears_stale_mode_reason(self):
        self.ready()
        self.page.serial.clear()
        self.assertIn("serial number", self.page.ready_reason.text())
        self.page.serial.setText("WO-S00430-1-001")
        self.page.refresh_context(dict(name="Variant"), dict(id="OP"), readiness="Simulation disabled")
        self.assertIn("Simulation", self.page.ready_reason.text())
        self.page.refresh_context(dict(name="Variant"), dict(id="OP"))
        self.assertIn("Ready", self.page.ready_reason.text())
        self.page.set_targets([])
        self.assertIn("Find programmer", self.page.ready_reason.text())


if __name__ == "__main__":
    unittest.main()
