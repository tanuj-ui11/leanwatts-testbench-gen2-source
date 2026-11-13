"""Render actual Gen2 widgets using isolated, explicitly labelled layout data.

No device access is allowed. Screenshots are not calibration/test evidence.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from leanwatts_can import theme
from leanwatts_can.core import Recipe
from leanwatts_can.gui import CalibrationWindow
from leanwatts_can.recipe_page import atomic_json, RAMP_DEFAULTS
from leanwatts_can.session_store import OperatorStore, SessionStore
from leanwatts_can.account_settings import LoginDialog
from leanwatts_can.can_monitor import MonitorClient
from leanwatts_can.maynuo import MaynuoLoad
from leanwatts_can.version import VERSION


def forbidden(*args, **kwargs):
    raise AssertionError("Hardware access is forbidden during layout previews.")


MaynuoLoad.open = forbidden
MonitorClient.open = forbidden
out = ROOT / "artifacts" / ("gen2-" + VERSION + "-screens")
out.mkdir(parents=True, exist_ok=True)
app = QApplication([])
theme.load_fonts()
app.setFont(theme.ui_font(14))
app.setStyleSheet(theme.STYLESHEET)
with tempfile.TemporaryDirectory(prefix="can-layout-v8-") as temp:
    data_dir = Path(temp)
    recipe = Recipe(name="PREVIEW · 58.4 V / 15 A", cc_current=15, end_voltage=58.4,
        end_current=1.1, current_tolerance=.1, voltage_tolerance=.1,
        end_current_tolerance=.1, current_cal_load_voltage=48,
        voltage_cal_load_current=1, max_voltage=65, max_current=20, max_power=1300,
        programmed_cc_current=15, programmed_end_voltage=58.4, programmed_end_current=1.5,
        cc_test_load_voltage=49, voltage_test_load_current=2,
        termination_test={"enabled": True, **RAMP_DEFAULTS},
        device_settings=dict(batteryType=0, ratedVoltage=48, startVoltage=40,
            temperatureLimit=70, maxChargeMinutes=480, derating110=50, derating220=100,
            blindCharge=0, rechargeVoltage=52, adjustCurrentMin=0, adjustCurrentMax=0,
            adjustVoltageMin=0, adjustVoltageMax=0))
    assert not recipe.validate_production(), recipe.validate_production()
    atomic_json(data_dir / "profiles.json", [dict(recipe=recipe.to_dict(), revision=1, simulation_only=True)])
    accounts = OperatorStore(data_dir)
    accounts.upsert_operator("PREVIEW-01", "Sample operator", "layout-only-password", role="admin")
    sessions = SessionStore(data_dir)
    sessions.start(accounts.authenticate("PREVIEW-01", "layout-only-password"))
    attempt = sessions.begin_unit("SAMPLE-CHARGER-001", recipe.to_dict(), 1)
    sessions.finish_unit(attempt, dict(outcome="SIMULATION", calibration_result="SIMULATION",
        test_result="SIMULATION", detail="Layout sample only; no physical charger was operated.",
        simulation=True, stage_details=[], calibration={}, test={}))
    sessions.close()
    window = CalibrationWindow(data_dir)
    window.resize(1366, 768)
    window.show()
    window.legacy_page.mode_select.setCurrentIndex(2)
    window.operator_page.timer.stop()
    window.recipes._timer.stop()
    window.legacy_page._live_timer.stop()

    def capture(name):
        window.mode_badge.setText("LAYOUT PREVIEW")
        app.processEvents()
        app.processEvents()
        target = out / (name + ".png")
        if not window.grab().save(str(target)):
            raise RuntimeError("Screenshot could not be saved.")
        print(target)

    for index, name in enumerate(("01-session", "02-variant-recipes", "03-history-reports",
                                  "04-load-setup", "05-manual-calibration", "06-operator-settings")):
        window.tabs.setCurrentIndex(index)
        if index == 2 and window.history_page.table.rowCount():
            window.history_page.table.selectRow(0)
        if index == 5:
            window.settings_page.setCurrentIndex(0)
        capture(name)
    window.settings_page.setCurrentIndex(1)
    capture("07-software-updates")
    window.settings_page.setCurrentWidget(window.appearance_page)
    capture("09-appearance")
    window.settings_page.setCurrentWidget(window.about_page)
    capture("10-about")
    window.tabs.setCurrentIndex(0)
    window.appearance_page.buttons["blue"].setChecked(True)
    capture("11-session-ocean")
    window.appearance_page.buttons["teal"].setChecked(True)
    capture("12-session-teal")
    window.appearance_page.buttons["purple"].setChecked(True)
    window.appearance_page.mode_buttons["light"].setChecked(True)
    capture("13-session-light")
    window.tabs.setCurrentWidget(window.recipes)
    capture("14-recipes-light")
    window.tabs.setCurrentWidget(window.settings_page)
    window.settings_page.setCurrentWidget(window.appearance_page)
    capture("15-appearance-light")
    window.settings_page.setCurrentWidget(window.about_page)
    capture("16-about-light")
    window.appearance_page.mode_buttons["dark"].setChecked(True)
    window.tabs.setCurrentIndex(0)
    login = LoginDialog(accounts, window)
    login.operator_id.setText("PREVIEW-01")
    login.show()
    app.processEvents()
    login.grab().save(str(out / "08-session-login.png"))
    print(out / "08-session-login.png")
    login.close()
    window.close()
    app.processEvents()
