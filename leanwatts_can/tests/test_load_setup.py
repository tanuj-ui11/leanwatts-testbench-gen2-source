"""One-time setup persistence, read-only worker and invalidation checks."""
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEvent

from leanwatts_can.load_setup import LoadSetupPage
from leanwatts_can.maynuo import LoadIdentity, LoadReading, MaynuoLoad, frame
from leanwatts_can.tests.test_maynuo import FakeSerial


class LoadSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.serials = []
        self.mode = 7

        def factory(settings, *limits):
            serial = FakeSerial()
            serial.mode = self.mode
            self.serials.append(serial)
            return MaynuoLoad(settings, *limits, serial_factory=lambda **kwargs: serial)

        self.page = LoadSetupPage(Path(self.temp.name), load_factory=factory)
        self.page.port.setCurrentText("COM-test")

    def tearDown(self):
        self.page.dispose()
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def capture(self, kind, code):
        reading = LoadReading(48, 0, 0, code, "UNKNOWN", None, False, (), time.monotonic(),
                              1, False, 8437, 9)
        self.page._accept_probe(self.page._serial_settings(), kind, LoadIdentity(8437, 9), reading)

    def approved(self):
        self.capture("CC", 7)
        self.capture("CV", 11)
        self.page.readings_match.setChecked(True)
        self.page.protocol_approved.setChecked(True)

    def test_default_is_unapproved_and_never_opens_port(self):
        self.assertEqual(self.page.settings_dict, {})
        self.assertEqual(self.serials, [])
        self.assertFalse(self.page.protocol_approved.isChecked())
        self.assertFalse(self.page.duty_approved.isChecked())
        self.assertFalse(self.page.termination_ramp_approved.isChecked())
        # Bounds are prefilled drafts (start 80, bounds 60-85); they do not
        # enable tuning on their own because duty approval stays unchecked.
        for axis in ("current", "voltage"):
            self.assertEqual(self.page.duty_fields[axis + "_initial"].text(), "80.0")
            self.assertEqual(self.page.duty_fields[axis + "_min"].text(), "60.0")
            self.assertEqual(self.page.duty_fields[axis + "_max"].text(), "85.0")
        self.assertEqual(self.page.directions["current"].currentData(), 0)
        self.assertEqual(self.page.directions["voltage"].currentData(), 0)

    def test_calibration_connection_saves_fixed_profile_without_opening_port(self):
        self.page.save_calibration_connection()
        saved = self.page.settings_dict
        self.assertEqual(self.serials, [])
        self.assertEqual(saved["calibration_profile"], LoadSetupPage.CALIBRATION_PROFILE)
        self.assertEqual((saved["model_code"], saved["firmware_code"]), (140, 42))
        self.assertEqual((saved["cc_mode_code"], saved["cv_mode_code"]), (0x3B01, 0x3B02))
        self.assertFalse(saved["termination_ramp_approved"])
        self.assertNotIn("observations", saved)
        restored = LoadSetupPage(Path(self.temp.name))
        try:
            self.assertEqual(restored.settings_dict["calibration_profile"], LoadSetupPage.CALIBRATION_PROFILE)
            # A saved profile is an expected identity, not a fresh observation.
            self.assertIsNone(restored._identity)
        finally:
            restored.dispose()
            restored.close()

    def test_persisted_profile_with_forged_identity_rejected(self):
        self.page.save_calibration_connection()
        path = Path(self.temp.name) / "station-load-setup.json"
        data = json.loads(path.read_text())
        data["model_code"] = 8437
        path.write_text(json.dumps(data))
        restored = LoadSetupPage(Path(self.temp.name))
        try:
            self.assertEqual(restored.settings_dict, {})
            self.assertIn("needs review", restored.status.text())
        finally:
            restored.dispose()
            restored.close()

    def test_unregulated_status_stays_visible_alongside_protection_fault(self):
        reading = LoadReading(48, 0, 0, 0x3B01, "UNKNOWN", None, False,
                              ("Over-temperature",), time.monotonic(), 1, False,
                              8437, 9, unregulated=True)
        self.page._accept_probe(self.page._serial_settings(), "probe", LoadIdentity(8437, 9), reading)
        self.assertIn("Over-temperature", self.page.reading_label.text())
        self.assertIn("Load unregulated (not at setpoint)", self.page.reading_label.text())
        self.assertEqual(self.page.settings_dict, {})

    def test_visible_port_picker_selects_only_and_invalidates_old_identity(self):
        self.approved()
        self.page.save_manual_settings()
        self.page._available_ports = [("COM7", "USB serial load"), ("COM9", "Other USB serial")]
        with patch.object(self.page, "refresh_ports"):
            self.page.refresh_button.click()
        dialog = self.page._port_dialog
        self.assertIsNotNone(dialog)
        self.assertEqual(dialog.ports.count(), 2)
        self.assertFalse(dialog.use_button.isEnabled())
        dialog.ports.setCurrentRow(0)
        self.assertTrue(dialog.use_button.isEnabled())
        dialog.use_button.click()
        self.app.processEvents()
        self.assertEqual(self.page.port.currentText(), "COM7")
        self.assertEqual(self.page.settings_dict, {})
        self.assertEqual(self.page._observations, {})
        self.assertIsNone(self.page._port_dialog)
        self.assertEqual(self.serials, [])
        self.assertIn("COM7 selected", self.page.status.text())
        self.assertIn("Save the calibration connection", self.page.status.text())

    def test_cancel_port_picker_keeps_saved_setup_and_opens_no_instrument(self):
        self.approved()
        self.page.save_manual_settings()
        saved = self.page.settings_dict
        self.page._available_ports = [("COM-test", "Current load"), ("COM7", "USB serial")]
        with patch.object(self.page, "refresh_ports"):
            self.page.choose_port()
        dialog = self.page._port_dialog
        dialog.ports.setCurrentRow(1)
        dialog.reject()
        self.app.processEvents()
        self.assertEqual(self.page.port.currentText(), "COM-test")
        self.assertEqual(self.page.settings_dict, saved)
        self.assertEqual(self.serials, [])

    def test_empty_inventory_does_not_open_a_picker_or_instrument(self):
        with patch.object(self.page, "refresh_ports"):
            self.page.choose_port()
        self.assertIsNone(self.page._port_dialog)
        self.assertEqual(self.page.port.currentText(), "COM-test")
        self.assertEqual(self.serials, [])

    def test_setup_reads_on_worker_and_never_writes(self):
        self.page.start_probe("CC")
        self.assertTrue(self.page.busy)
        self.assertFalse(self.page.probe_button.isEnabled())
        deadline = time.monotonic() + 3
        while self.page._future is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(self.page._future)
        self.assertFalse(self.page.busy)
        self.assertEqual(self.page._observations["CC"]["mode_code"], 7)
        self.assertTrue(self.serials[0].closed)
        self.assertTrue(all(r[1] in (1, 3) for r in self.serials[0].requests))
        self.assertTrue(self.page.probe_button.isEnabled())

    def test_failed_probe_saves_and_copies_exact_reply_without_enabling_setup(self):
        original_factory = self.page._load_factory
        bad_reply = frame(bytes.fromhex("01 03 02 41 20"))

        def failing_factory(settings, *limits):
            load = original_factory(settings, *limits)
            self.serials[-1].transform = lambda response: bad_reply
            return load

        self.page._load_factory = failing_factory
        self.page.port.setCurrentText("COM11")
        self.page.start_probe()
        deadline = time.monotonic() + 3
        while self.page.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertFalse(self.page.busy)
        self.assertIn("got 2, expected 4", self.page.status.text())
        self.assertTrue(self.page.copy_diagnostics_button.isEnabled())
        self.page.copy_diagnostics_button.click()
        copied = json.loads(self.app.clipboard().text())
        saved_path = Path(self.temp.name) / "diagnostics/load-probe-latest.json"
        self.assertEqual(json.loads(saved_path.read_text()), copied)
        self.assertEqual(copied["serial_settings"]["port"], "COM11")
        self.assertEqual(copied["transactions"][-1]["rx_hex"], bad_reply.hex(" ").upper())
        self.assertFalse(copied["instrument_writes"])
        self.assertEqual(self.page.settings_dict, {})
        self.assertIsNone(self.page._identity)
        self.assertTrue(self.serials[-1].closed)
        self.assertTrue(all(r[1] in (1, 3) for r in self.serials[-1].requests))

    def test_failed_report_save_preserves_original_error_and_copyable_evidence(self):
        def unavailable_factory(*args):
            raise OSError("Port not available")

        self.page._load_factory = unavailable_factory
        with patch.object(Path, "mkdir", side_effect=PermissionError("Folder is read-only")):
            result, error, report = self.page._probe_worker(self.page._serial_settings(), "probe")
        self.assertIsNone(result)
        self.assertEqual(error, "Port not available")
        self.assertEqual(report["error"], error)
        self.assertIn("read-only", report["save_error"])
        self.assertNotIn("saved_file", report)
        self.assertEqual(report["transactions"], [])

    def test_same_mode_codes_cannot_be_saved(self):
        self.approved()
        self.capture("CV", 7)
        self.page.save_manual_settings()
        self.assertEqual(self.page.settings_dict, {})
        self.assertIn("same mode code", self.page.status.text())

    def test_both_readings_and_protocol_approval_required(self):
        self.capture("CC", 7)
        self.capture("CV", 11)
        self.page.save_manual_settings()
        self.assertEqual(self.page.settings_dict, {})
        self.assertFalse((Path(self.temp.name) / "station-load-setup.json").exists())

    def test_valid_load_setup_persists_without_duty_approval(self):
        self.approved()
        self.page.save_manual_settings()
        saved = self.page.settings_dict
        self.assertTrue(saved["protocol_approved"])
        self.assertFalse(saved["automation_settings"]["commissioned"])
        self.assertFalse(saved["termination_ramp_approved"])
        restored = LoadSetupPage(Path(self.temp.name))
        try:
            self.assertEqual(restored.settings_dict["cc_mode_code"], 7)
            self.assertEqual(restored.settings_dict["cv_mode_code"], 11)
            self.assertFalse(restored.settings_dict["automation_settings"]["commissioned"])
        finally:
            restored.dispose()
            restored.close()

    def test_serial_change_invalidates_identity_and_observations(self):
        self.approved()
        self.page.save_manual_settings()
        events = []
        self.page.configured_changed.connect(events.append)
        self.page.address.setValue(2)
        self.assertEqual(self.page.settings_dict, {})
        self.assertEqual(self.page._observations, {})
        self.assertIsNone(self.page._identity)
        self.assertFalse(self.page.protocol_approved.isChecked())
        self.assertEqual(events[-1], {})

    def test_unapproved_draft_bounds_and_unknown_directions_survive_restart(self):
        self.approved()
        for axis in ("current", "voltage"):
            self.page.duty_fields[axis + "_min"].setText("60.0")
            self.page.duty_fields[axis + "_max"].setText("85.0")
        self.page.save_manual_settings()
        restored = LoadSetupPage(Path(self.temp.name))
        try:
            auto = restored.settings_dict["automation_settings"]
            self.assertFalse(auto["commissioned"])
            self.assertFalse(restored.duty_approved.isChecked())
            for axis in ("current", "voltage"):
                self.assertEqual(auto[axis + "_min"], 60.0)
                self.assertEqual(auto[axis + "_max"], 85.0)
                self.assertEqual(auto[axis + "_initial"], 80.0)
                self.assertEqual(restored.duty_fields[axis + "_min"].text(), "60.0")
                self.assertEqual(restored.directions[axis].currentData(), 0)
        finally:
            restored.dispose()
            restored.close()

    def test_unapproved_partial_draft_omits_invalid_fields_and_can_set_up_tests(self):
        self.approved()
        self.page.duty_fields["current_min"].setText("60.0")
        self.page.duty_fields["current_max"].setText("nan")
        self.page.duty_fields["voltage_initial"].setText("")
        self.page.save_manual_settings()
        auto = self.page.settings_dict["automation_settings"]
        self.assertFalse(auto["commissioned"])
        self.assertEqual(auto["current_min"], 60.0)
        self.assertNotIn("current_max", auto)
        self.assertNotIn("voltage_initial", auto)
        from leanwatts_can.automation import AutomationSettings
        settings = AutomationSettings(**auto)
        self.assertEqual(settings.validate(calibration=False), "")
        self.assertNotEqual(settings.validate(), "")

    def test_duty_approval_with_unknown_directions_requires_automatic_detection(self):
        # "Detect automatically" (0) is now a supported choice: the page always
        # saves auto_direction=True so calibration learns the direction. An
        # unknown direction without automatic detection must still be rejected.
        self.approved()
        for axis in ("current", "voltage"):
            self.page.duty_fields[axis + "_min"].setText("60.0")
            self.page.duty_fields[axis + "_max"].setText("85.0")
        self.page.duty_approved.setChecked(True)
        self.page.save_manual_settings()
        auto = self.page.settings_dict["automation_settings"]
        self.assertTrue(auto["commissioned"])
        self.assertTrue(auto["auto_direction"])
        self.assertEqual((auto["current_direction"], auto["voltage_direction"]), (0, 0))
        from leanwatts_can.automation import AutomationSettings
        fixed = AutomationSettings(**dict(auto, auto_direction=False))
        self.assertIn("direction", fixed.validate())

    def test_other_instrument_invalidates_captured_modes(self):
        self.approved()
        self.page.save_manual_settings()
        reading = LoadReading(48, 0, 0, 7, "UNKNOWN", None, False, (), time.monotonic(),
                              1, False, 1234, 9)
        self.page._accept_probe(self.page._serial_settings(), "probe", LoadIdentity(1234, 9), reading)
        self.assertEqual(self.page._observations, {})
        self.assertEqual(self.page.settings_dict, {})

    def test_duty_approval_requires_valid_bounds_and_persists_valid_values(self):
        self.approved()
        # Clear the prefilled draft bounds: approval must not save without them.
        for axis in ("current", "voltage"):
            self.page.duty_fields[axis + "_min"].setText("")
            self.page.duty_fields[axis + "_max"].setText("")
        self.page.duty_approved.setChecked(True)
        self.page.save_manual_settings()
        self.assertEqual(self.page.settings_dict, {})
        self.assertIn("bounds", self.page.status.text())
        for axis in ("current", "voltage"):
            self.page.duty_fields[axis + "_min"].setText("78.0")
            self.page.duty_fields[axis + "_max"].setText("82.0")
            self.page.directions[axis].setCurrentIndex(self.page.directions[axis].findData(-1))
        self.page.termination_ramp_approved.setChecked(True)
        self.page.save_manual_settings()
        saved = self.page.settings_dict
        self.assertTrue(saved["automation_settings"]["commissioned"])
        self.assertEqual(saved["automation_settings"]["current_min"], 78)
        self.assertTrue(saved["termination_ramp_approved"])
        restored = LoadSetupPage(Path(self.temp.name))
        try:
            self.assertTrue(restored.duty_approved.isChecked())
            self.assertEqual(restored.directions["current"].currentData(), -1)
            self.assertEqual(restored.directions["voltage"].currentData(), -1)
        finally:
            restored.dispose()
            restored.close()
        self.page.duty_fields["current_min"].setText("85.0")
        self.page.save_manual_settings()
        self.assertEqual(self.page.settings_dict, {})

    def test_persisted_forged_observation_identity_rejected(self):
        self.approved()
        self.page.save_manual_settings()
        path = Path(self.temp.name) / "station-load-setup.json"
        data = json.loads(path.read_text())
        data["observations"]["CC"]["model_code"] = 9
        path.write_text(json.dumps(data))
        restored = LoadSetupPage(Path(self.temp.name))
        try:
            self.assertEqual(restored.settings_dict, {})
            self.assertIn("needs review", restored.status.text())
        finally:
            restored.dispose()
            restored.close()


if __name__ == "__main__":
    unittest.main()
