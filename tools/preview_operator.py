"""Render the operator screen using isolated simulated data; no hardware access."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PySide6.QtWidgets import QApplication
from leanwatts_can import theme
from leanwatts_can.gui import CalibrationWindow, demo_recipe
from leanwatts_can.automation import AutomationEngine, DemoBench

app = QApplication([])
theme.load_fonts()
app.setFont(theme.ui_font(14))
app.setStyleSheet(theme.STYLESHEET)
with tempfile.TemporaryDirectory() as directory:
    window = CalibrationWindow(Path(directory))
    window.timer.stop()
    page = window.operator_page
    page.timer.stop()
    window.resize(1024, 650)
    window.show()
    page.serial.setText("PREVIEW-001")
    page.operator.setText("Operator")
    recipe = demo_recipe()
    engine = AutomationEngine(recipe, "PREVIEW-001", "Operator", bench=DemoBench(recipe),
                              report_dir=ROOT / "artifacts/operator-demo-reports")
    engine.start_calibration(now=0)
    for i in range(1, 100):
        engine.tick(now=i * .25)
        if engine.phase == "cal_current" and engine.measured is not None:
            break
    page._render_run(engine.snapshot())
    app.processEvents()
    window.grab().save(str(ROOT / "artifacts/operator-current-preview.png"))
    for j in range(i + 1, 400):
        engine.tick(now=j * .25)
        if engine.phase == "cal_voltage" and engine.measured is not None:
            break
    page._render_run(engine.snapshot())
    app.processEvents()
    window.grab().save(str(ROOT / "artifacts/operator-voltage-preview.png"))
    for k in range(j + 1, 800):
        engine.tick(now=k * .25)
        if not engine.is_running:
            break
    test_engine = AutomationEngine(recipe, "PREVIEW-001", "Operator", bench=DemoBench(recipe),
                                   report_dir=ROOT / "artifacts/operator-demo-reports")
    test_engine.start_test(now=0)
    for i in range(1, 300):
        test_engine.tick(now=i * .25)
        if not test_engine.is_running:
            break
    page._render_run(test_engine.snapshot())
    app.processEvents()
    window.grab().save(str(ROOT / "artifacts/operator-test-preview.png"))
    window.legacy_page.mode_select.setCurrentIndex(2)
    app.processEvents()
    window.grab().save(str(ROOT / "artifacts/operator-live-setup.png"))
    window.tabs.setCurrentWidget(window.load_setup)
    app.processEvents()
    window.grab().save(str(ROOT / "artifacts/bench-load-setup-7.png"))
    window.load_setup.tabs.setCurrentIndex(1)
    app.processEvents()
    window.grab().save(str(ROOT / "artifacts/bench-duty-setup-7.png"))
    window.tabs.setCurrentIndex(2)
    app.processEvents()
    window.grab().save(str(ROOT / "artifacts/bench-variant-columns-7.png"))
    window.close()
    app.processEvents()
print("Operator screenshots and explicitly simulated example reports saved.")
