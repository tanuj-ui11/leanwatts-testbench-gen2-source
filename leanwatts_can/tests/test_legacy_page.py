"""Offline verification of the translated original-layout page."""
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QComboBox
from leanwatts_can import theme
from leanwatts_can.gui import CalibrationWindow
from leanwatts_can.protocol import CONFIG, WRITE_ORDER


class OriginalLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.load_fonts()
        cls.app.setStyleSheet(theme.STYLESHEET)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.window = CalibrationWindow(Path(self.temp.name))
        self.window.operator_page.timer.stop()
        self.page = self.window.legacy_page

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def connect(self):
        self.page.open_device()
        self.page.start_can()

    @staticmethod
    def frame(entry):
        return entry.get("frame", entry)

    def test_original_layout_has_all_english_controls_and_paired_fields(self):
        p = self.page
        self.assertEqual(set(p.settings), set(CONFIG))
        self.assertEqual(set(p.readbacks), set(CONFIG))
        for cls in (QLabel, QPushButton):
            for widget in p.findChildren(cls):
                self.assertIsNone(re.search(r"[\u4e00-\u9fff]", widget.text()), widget.text())
        for widget in p.findChildren(QComboBox):
            self.assertIsNone(re.search(r"[\u4e00-\u9fff]", widget.currentText()))
        for key in CONFIG:
            self.assertTrue(p.readbacks[key].isReadOnly())
        for key in ("work_start", "shutdown", "calibration_start", "save_voltage", "save_full_current", "save_half_current"):
            self.assertIn(key, p.buttons)

    def test_english_text_fits_original_control_positions(self):
        self.window.resize(1280, 720)
        self.window.tabs.setCurrentWidget(self.page)
        self.window.show()
        self.app.processEvents()
        for cls in (QLabel, QPushButton):
            for widget in self.page.canvas.findChildren(cls):
                self.assertLessEqual(widget.fontMetrics().horizontalAdvance(widget.text()), widget.width(), widget.text())

    def test_original_sequence_generates_exact_offline_frames(self):
        p = self.page
        self.connect()
        p.shutdown()
        p.settings["endVoltage"].setText("58.4")
        p.settings["ccCurrent"].setText("15")
        p.settings["endCurrent"].setText("0.5")
        before = len(p.frame_log)
        p.send_settings()
        frames = [self.frame(entry) for entry in p.frame_log[before:]]
        self.assertEqual(len(frames), 16)
        self.assertEqual([f["data"][1] for f in frames], [CONFIG[k]["selector"] for k in WRITE_ORDER])
        self.assertNotEqual(p.readbacks["endVoltage"].text(), "58.4")
        p.complete_readback()
        self.assertEqual(float(p.readbacks["endVoltage"].text()), 58.4)
        p.calibration_start()
        self.assertEqual(self.frame(p.frame_log[-1])["data"], [0, 0x14, 0, 1])
        before = len(p.frame_log)
        p.adjust_duty("current", .1)
        self.assertEqual(len(p.frame_log)-before, 6)
        self.assertEqual(float(p.duty["current"].text()), 80.1)
        self.assertEqual(self.frame(p.frame_log[-1])["data"], [0, 0x16, 3, 33])
        for method, payload in [(p.save_voltage, [0, 0x17]), (p.save_full_current, [0, 0x18]), (p.save_half_current, [0, 0x19])]:
            method()
            f = self.frame(p.frame_log[-1])
            self.assertEqual(f["data"], payload)
            self.assertEqual(f["dlc"], 2)
        self.assertIn("NOT SENT", p.trace.toPlainText())

    def test_invalid_settings_cannot_partially_update(self):
        p = self.page
        self.connect()
        p.settings["endVoltage"].setText("NaN")
        before = len(p.frame_log)
        old = {k: v.text() for k, v in p.readbacks.items()}
        with self.assertRaises(ValueError):
            p.send_settings()
        self.assertEqual(len(p.frame_log), before)
        self.assertEqual({k: v.text() for k, v in p.readbacks.items()}, old)
        p.buttons["send_settings"].click()
        self.assertTrue(p.last_error)
        self.assertEqual(len(p.frame_log), before)

    def test_reset_cancels_readback_and_disables_calibration(self):
        p = self.page
        self.connect()
        p.settings["ccCurrent"].setText("12")
        p.send_settings()
        p.reset_can()
        p.complete_readback()
        self.assertEqual(float(p.readbacks["ccCurrent"].text()), 0)
        self.assertFalse(p.can_started)
        self.assertFalse(p.buttons["save_voltage"].isEnabled())

    def test_shortcuts_do_not_start_hidden_session_run_and_preview_stop_is_offline(self):
        p = self.page
        session = self.window.operator_page
        self.window.tabs.setCurrentWidget(p)
        with patch.object(session, "start", side_effect=AssertionError("F5 must not start a hidden session run")):
            self.window._start_shortcut()
        self.assertFalse(session.busy)
        self.assertIsNone(self.window.session_store.active())
        self.connect()
        p.calibration_start()
        before = len(p.frame_log)
        self.window._stop_shortcut()  # Escape: stops automatic work only; it never touches manual CAN.
        self.assertEqual(len(p.frame_log), before)
        self.assertIsNone(p._live_client)
        p.stop_preview()
        self.assertEqual(p.running_mode, "stopped")
        self.assertFalse(p.can_started)
        self.assertEqual(len(p.frame_log), before)

if __name__ == "__main__":
    unittest.main()
