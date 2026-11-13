"""Render the actual Qt application at laptop size; never opens device transports."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
from pathlib import Path
from PySide6.QtWidgets import QApplication
from leanwatts_can import theme
from leanwatts_can.gui import CalibrationWindow

app = QApplication([])
theme.load_fonts()
app.setFont(theme.ui_font(14))
app.setStyleSheet(theme.STYLESHEET)
out = Path("artifacts")
out.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as data:
    window = CalibrationWindow(Path(data))
    window.timer.stop()
    window.resize(1280, 720)
    window.show()
    app.processEvents()
    window.grab().save(str(out / "calibration-ready.png"))
    window.start_auto()
    for _ in range(40):
        window._tick()
        if window.engine.state == "Auto current trim":
            break
    app.processEvents()
    window.grab().save(str(out / "calibration-current.png"))
    for _ in range(110):
        window._tick()
        if not window.engine.is_running:
            break
    window.tabs.setCurrentIndex(1)
    app.processEvents()
    window.grab().save(str(out / "calibration-profiles.png"))
    window.tabs.setCurrentIndex(2)
    app.processEvents()
    window.grab().save(str(out / "calibration-history.png"))
    window.tabs.setCurrentWidget(window.legacy_page)
    app.processEvents()
    window.grab().save(str(out / "original-ui-english.png"))
    window.close()
print("Rendered actual application screens at 1280 x 720.")
