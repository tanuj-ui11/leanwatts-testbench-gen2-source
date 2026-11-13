"""Test-page interactions do not open instruments or run program-file code."""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication, QScrollArea
from leanwatts_can.test_program_page import TestProgramPage
from leanwatts_can.test_programs import DEFAULT_FILENAME


class TestProgramPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.page = TestProgramPage(self.temp.name)
        self.addCleanup(self.page.close)
        self.page.resize(1268, 640)
        self.page.show()
        self.app.processEvents()

    def ready(self):
        self.page.refresh_context(dict(name="48 V · 20 A"), dict(name="Operator", id="7"))
        self.page.serial.setText("WO-S00430-1-001")

    def test_login_variant_and_serial_are_required_and_busy_locks_inputs(self):
        self.assertFalse(self.page.start_button.isEnabled())
        self.ready()
        self.assertTrue(self.page.start_button.isEnabled())
        self.page.set_busy(True)
        self.assertTrue(self.page.stop_button.isEnabled())
        for widget in (self.page.start_button, self.page.serial, self.page.programs, self.page.refresh_button):
            self.assertFalse(widget.isEnabled())
        self.page.set_busy(False)
        self.page.refresh_context(dict(name="Variant"), dict(id="7"), busy=True)
        self.assertFalse(self.page.start_button.isEnabled())
        self.assertFalse(self.page.stop_button.isEnabled())

    def test_idle_rows_show_ocv_before_loaded_current_and_cutoff(self):
        self.assertEqual([self.page.results.item(row, 0).text() for row in range(5, 8)],
                         ["Open-circuit voltage", "Full current", "End-current cutoff"])
        self.assertEqual(self.page.results.item(0, 0).text(), "Baseline open voltage")

    def test_run_freezes_latest_program_and_has_explicit_serial_override(self):
        self.ready()
        self.page.serial.setText("CUSTOM UNIT")
        self.assertFalse(self.page.start_button.isEnabled())
        self.page.other_number.setChecked(True)
        received = []
        self.page.run_requested.connect(received.append)
        path = Path(self.temp.name) / DEFAULT_FILENAME
        value = json.loads(path.read_text(encoding="utf-8"))
        # Edit a field 1.1.11 still accepts (cutoff_step_a is a retired key).
        value["testing"]["restart_timeout_seconds"] = 90
        path.write_text(json.dumps(value), encoding="utf-8")
        self.page.start_button.click()
        path.write_text("{}", encoding="utf-8")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["program"]["testing"]["restart_timeout_seconds"], 90)
        self.assertEqual(received[0]["program"]["testing"]["start_method"], "external_restart")
        self.assertTrue(received[0]["serial_override"])
        self.assertEqual(received[0]["serial"], "CUSTOM UNIT")
        self.page.set_busy(True)
        self.page.set_busy(False)
        self.assertTrue(self.page.other_number.isChecked())
        self.page.serial.setText("NEXT UNIT")
        self.assertFalse(self.page.other_number.isChecked())

    def test_invalid_program_visible_and_cannot_start(self):
        self.ready()
        (Path(self.temp.name) / "bad.json").write_text("{}", encoding="utf-8")
        self.page.refresh_programs()
        self.page.programs.setCurrentText("bad.json")
        self.assertIn("cannot run", self.page.description.text())
        self.assertFalse(self.page.start_button.isEnabled())

    def test_upgrade_prefers_normal_program_and_preserves_retired_file(self):
        path = Path(self.temp.name) / DEFAULT_FILENAME
        old = json.loads(path.read_text(encoding="utf-8"))
        old["testing"]["start_method"] = "calibration_start_only"
        retired = Path(self.temp.name) / "01-full-cycle-calibration-mode.json"
        original = json.dumps(old).encode()
        retired.write_bytes(original)
        reopened = TestProgramPage(self.temp.name)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.programs.currentText(), DEFAULT_FILENAME)
        self.assertEqual(retired.read_bytes(), original)
        reopened.programs.setCurrentText(retired.name)
        self.assertIn("retired", reopened.description.text())
        self.assertFalse(reopened.start_button.isEnabled())

    def test_results_and_clipboard_bundle_and_no_full_page_scroll(self):
        self.ready()
        self.assertEqual(self.page.findChildren(QScrollArea), [])
        self.page.update_snapshot(dict(stage="Current building", detail="Waiting for stable current",
            reading=dict(current=12.5, voltage=49.1), elapsed_seconds=12,
            stage_details=[dict(name="Baseline current", before=None, after=12.5, unit="A", status="RUNNING")],
            events=["Started trace"]))
        self.assertEqual(self.page.current.value.text(), "12.5")
        self.assertEqual(self.page.results.item(0, 2).text(), "12.5 A")
        self.assertIn("Started trace", self.page.activity.toPlainText())
        bundle = Path(self.temp.name) / "run.zip"
        bundle.write_bytes(b"test")
        self.page.set_result_bundle(bundle)
        self.page.copy_result_file()
        self.assertEqual(Path(QApplication.clipboard().mimeData().urls()[0].toLocalFile()), bundle.resolve())
        self.assertLessEqual(self.page.minimumSizeHint().height(), 640)


if __name__ == "__main__":
    unittest.main()
