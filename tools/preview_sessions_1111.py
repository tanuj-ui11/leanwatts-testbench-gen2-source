"""Actual session widgets with isolated synthetic records; never access instruments."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from leanwatts_can import theme
from leanwatts_can.gui import CalibrationWindow
from leanwatts_can.session_page import SessionModeDialog
from leanwatts_can.session_store import atomic_json
from leanwatts_can.tests.test_session_ui_v8 import profile
from leanwatts_can.tests.test_session_pairing import calibrated


def forbidden(*args, **kwargs):
    raise AssertionError("No instruments may be accessed in a layout preview.")


out = ROOT / "artifacts" / "gen2-1.1.11-session-preview"
out.mkdir(parents=True, exist_ok=True)
app = QApplication.instance() or QApplication([])
theme.load_fonts()
app.setFont(theme.ui_font(14))
with tempfile.TemporaryDirectory(prefix="gen2-layout-") as folder, \
        patch("leanwatts_can.can_monitor.MonitorClient.open", forbidden), \
        patch("leanwatts_can.maynuo.MaynuoLoad.open", forbidden):
    recipe = profile()
    recipe.name = "57.3 V / 20 A"
    recipe.end_voltage = recipe.programmed_end_voltage = 57.3
    recipe.cc_current = recipe.programmed_cc_current = 20
    recipe.max_current, recipe.max_voltage, recipe.max_power = 25, 65, 1600
    atomic_json(Path(folder) / "profiles.json", [dict(recipe=recipe.to_dict(), revision=1)])
    window = CalibrationWindow(Path(folder))
    window.resize(1268, 748)
    window.show()
    for timer in window.findChildren(QTimer):
        timer.stop()
    page = window.operator_page
    operator = dict(id="OP-01", name="Sample operator", enabled=True, role="operator")
    page._session = page.store.start(operator, mode="both_required")
    page._authenticated_operator = operator
    page.live_run_factory = forbidden
    page.live_readiness = lambda process: ""
    page.serial.setText("WO-S00261-BS1-019")
    result = calibrated(detail="Synthetic layout preview; no charger operated.")
    result["stage_details"] = [
        dict(name="Baseline open voltage", after=56, unit="V", duration_seconds=1, status="PASS"),
        dict(name="Baseline current", after=23, unit="A", duration_seconds=19, status="PASS"),
        dict(name="Charger parameters", before_summary="20 A / 57.3 V", after_summary="Checked; unchanged", duration_seconds=2, status="PASS"),
        dict(name="Current calibration", before=23, after=20.01, unit="A", duration_seconds=22, status="PASS"),
        dict(name="Voltage calibration", before=56, after=57.3, unit="V", duration_seconds=8, status="PASS")]
    attempt = page.store.begin_unit(page.serial.text(), recipe, 1, run_tests=False)
    page.store.finish_unit(attempt, result)
    page._session = page.store.active()
    page._attempt_id = attempt
    page.snapshot = dict(is_running=False, stage="Calibration complete", run_tests=False,
        run_calibration=True, stage_details=result["stage_details"],
        detail="Switch charger AC off. Press Test and follow the restart instruction.",
        unit_result=dict(outcome="CALIBRATED"))
    page._render_run(page.snapshot)
    page.refresh()
    window.tabs.setCurrentWidget(page)
    for key in ("purple", "purple_light"):
        theme.apply_theme(key, app)
        window.mode_badge.setText("LAYOUT PREVIEW · NO HARDWARE")
        app.processEvents()
        app.processEvents()
        path = out / ("session-both-required-" + ("light" if key.endswith("light") else "dark") + ".png")
        assert window.grab().save(str(path))
        print(path)
    dialog = SessionModeDialog(window)
    dialog.mode_select.setCurrentIndex(3)
    dialog.show()
    app.processEvents()
    path = out / "session-start-mode.png"
    assert dialog.grab().save(str(path))
    print(path)
    dialog.mode_select.showPopup()
    app.processEvents()
    path = out / "session-mode-four-options.png"
    assert dialog.mode_select.view().window().grab().save(str(path))
    print(path)
    dialog.mode_select.hidePopup()
    dialog.close()
    page._close_session()
    page._session = page.store.start(operator, mode="independent")
    page._authenticated_operator = operator
    page.serial.clear()
    page._identity_changed()
    page.report_status.setText("Session reports save automatically as chargers finish.")
    page.refresh()
    window.mode_badge.setText("LAYOUT PREVIEW · NO HARDWARE")
    app.processEvents()
    path = out / "session-independent-light.png"
    assert window.grab().save(str(path))
    print(path)
    page._close_session()
    page._session = page.store.start(operator, mode="test_only")
    page._authenticated_operator = operator
    page.serial.setText("WO-S00261-BS1-020")
    page.refresh()
    page.snapshot = dict(is_running=True, phase="test_ocv", stage="Open-circuit voltage test in progress",
        run_calibration=False, run_tests=True, elapsed_seconds=2.3, unit="V", error=-0.02,
        reading=dict(current=0, voltage=57.28), stage_details=[],
        detail="Load OFF · Waiting for settled open-circuit voltage. Full-current test follows at CV 49 V.")
    page._render_run(page.snapshot)
    page._lock_controls()
    window.mode_badge.setText("LAYOUT PREVIEW · NO HARDWARE")
    app.processEvents()
    path = out / "session-test-only-ocv-light.png"
    assert window.grab().save(str(path))
    print(path)
    page.snapshot = None
    page._lock_controls()
    window.close()
    app.processEvents()
