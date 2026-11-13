"""Production session shell. Construction never opens instruments."""
from __future__ import annotations
import argparse
import copy
import sys
from pathlib import Path
from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QTabWidget, QVBoxLayout, QWidget)
from . import theme
from .automation import AutomationSettings
from .bench_session import BenchSession
from .core import Recipe
from .legacy_page import LegacyChargerPage
from .load_setup import LoadSetupPage
from .maynuo import MaynuoLoad, SerialSettings
from .recipe_page import RecipePage
from .session_store import SessionStore
from .session_page import SessionPage
from .test_program_page import TestProgramPage
from .test_program_controller import TestProgramController
from .firmware_page import FirmwareProgrammingPage
from .firmware_controller import FirmwareController
from .production_flow import validate_test_recipe
from .session_history import SessionHistoryPage
from .account_settings import AccountSettings, LoginDialog
from .update_settings import UpdateSettingsPage
from .appearance_settings import AppearanceSettings
from .about_page import AboutPage
from .branding import app_icon, logo_label, footer_widget
from .version import VERSION, PRODUCT


class CalibrationWindow(QMainWindow):
    def __init__(self, data_dir: Path):
        super().__init__()
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        theme.load_saved_theme(self.data_dir)
        self._closing = False
        self.setWindowTitle(f"{PRODUCT} {VERSION}")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(1040, 680)
        self.resize(1400, 900)
        self.session_store = SessionStore(self.data_dir)
        self._build()
        self.recipes.load()
        self.history_page.refresh()
        QShortcut(QKeySequence("F5"), self, activated=self._start_shortcut)
        QShortcut(QKeySequence("Escape"), self, activated=self._stop_shortcut)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._scan_shortcut)
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(200)
        self._close_timer.timeout.connect(self._finish_pending_close)

    def _build(self):
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 12, 16, 10)
        layout.setSpacing(8)
        header = QHBoxLayout()
        header.addWidget(logo_label(height=34))
        header.addSpacing(10)
        subtitle = QLabel("TESTBENCH GEN2")
        subtitle.setObjectName("Caption")
        header.addWidget(subtitle)
        header.addStretch()
        self.mode_badge = theme.pill("LIVE BENCH", theme.ACCENT)
        header.addWidget(self.mode_badge)
        header.addWidget(theme.pill(VERSION, theme.MUTED))
        layout.addLayout(header)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        layout.addWidget(footer_widget())
        self.legacy_page = LegacyChargerPage()
        self.load_setup = LoadSetupPage(self.data_dir)
        self.recipes = RecipePage(self.data_dir, self.legacy_page)
        self.recipe_page = self.recipes
        self.operator_page = SessionPage(self.data_dir, self.legacy_page, self.session_store)
        self.test_page = TestProgramPage(self.data_dir / "test_programs")
        self.firmware_page = FirmwareProgrammingPage()
        self.history_page = SessionHistoryPage(self.data_dir, self.session_store)
        self.accounts_page = AccountSettings(self.data_dir)
        self.update_page = UpdateSettingsPage(self.data_dir, busy_check=self._update_busy,
            current_version=VERSION, can_configure=self._authorize_update_config)
        self.settings_page = QTabWidget()
        self.settings_page.addTab(self.accounts_page, "Operator accounts")
        self.settings_page.addTab(self.update_page, "Software updates")
        self.appearance_page = AppearanceSettings(self.data_dir)
        self.about_page = AboutPage(self.data_dir, version=VERSION)
        self.settings_page.addTab(self.appearance_page, "Appearance")
        self.settings_page.addTab(self.about_page, "About")
        self.about_page.updates_requested.connect(lambda: self.settings_page.setCurrentWidget(self.update_page))
        for name, page in (("Auto Calibration Session", self.operator_page),
                          ("Test", self.test_page),
                          ("Firmware Programming", self.firmware_page),
                          ("Variant Recipes", self.recipes), ("History && Reports", self.history_page),
                          ("Load Setup", self.load_setup), ("Manual Calibration", self.legacy_page),
                          ("Settings", self.settings_page)):
            self.tabs.addTab(page, name)
        self.recipes.profiles_changed.connect(self.operator_page.set_profiles)
        self.recipes.profiles_changed.connect(self.test_page.set_profiles)
        self.recipes.profiles_changed.connect(self.firmware_page.set_profiles)
        self.operator_page.live_readiness = self._operator_live_reason
        self.operator_page.live_run_factory = self._operator_live_factory
        self.test_controller = TestProgramController(self, self.test_page)
        self.test_controller.active_changed.connect(self._test_active)
        self.firmware_controller = FirmwareController(self, self.firmware_page)
        self.firmware_controller.active_changed.connect(self._firmware_active)
        self.operator_page.start_guard = lambda: ("Wait for firmware programming to finish." if self.firmware_controller.busy else
            "Wait for the test program to finish." if self.test_controller.busy else "")
        self.operator_page.run_active_changed.connect(self._operator_active)
        self.operator_page.report_saved.connect(self.history_page.refresh)
        self.operator_page.connections_requested.connect(lambda: self.tabs.setCurrentWidget(self.load_setup))
        self.operator_page.recipes_requested.connect(lambda: self.tabs.setCurrentWidget(self.recipes))
        self.operator_page.settings_requested.connect(lambda: self.tabs.setCurrentWidget(self.settings_page))
        self.accounts_page.accounts_changed.connect(self.operator_page.refresh)
        self.legacy_page.mode_changed.connect(self._mode_changed)
        self.load_setup.configured_changed.connect(lambda _: self.operator_page.refresh())
        self.update_page.install_started.connect(self.close)
        self.tabs.currentChanged.connect(self._tab_changed)

    def _tab_changed(self, index):
        if self.tabs.widget(index) is self.history_page:
            self.history_page.refresh()

    def _update_busy(self):
        return bool(self.operator_page.busy or (getattr(self, "test_controller", None) and self.test_controller.busy) or self.session_store.active() or self.load_setup.busy
                    or (getattr(self, "firmware_controller", None) and self.firmware_controller.busy)
                    or self.legacy_page.device_open or self.legacy_page._live_future is not None)

    def _authorize_update_config(self):
        if not self.accounts_page.accounts.has_accounts():
            QMessageBox.information(self, "Create administrator", "Create the first administrator account before changing the update source.")
            return False
        dialog = LoginDialog(self.accounts_page.accounts, self,
                             title="Administrator approval", admin_only=True)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _operator_active(self, active):
        firmware_busy = bool(getattr(self, "firmware_controller", None) and self.firmware_controller.busy)
        active = active or self.test_controller.busy or firmware_busy
        for page in (self.recipes, self.load_setup, self.legacy_page, self.settings_page):
            self.tabs.setTabEnabled(self.tabs.indexOf(page), not active)
        self.recipes.set_busy(active)
        self.mode_badge.setText("RUNNING" if active else
            "SIMULATION" if self.legacy_page.live_mode == "simulation" else "LIVE BENCH")
        self.tabs.setTabEnabled(self.tabs.indexOf(self.test_page), not self.operator_page.busy and not firmware_busy)
        self.tabs.setTabEnabled(self.tabs.indexOf(self.firmware_page), not self.operator_page.busy and not self.test_controller.busy)
        self.tabs.setTabEnabled(self.tabs.indexOf(self.operator_page), not self.test_controller.busy and not firmware_busy)

    def _test_active(self, active):
        self._operator_active(active)

    def _firmware_active(self, active):
        self._operator_active(active)

    def _mode_changed(self, mode):
        self.operator_page.set_mode(mode)
        self.mode_badge.setText("SIMULATION" if mode == "simulation" else "LIVE BENCH")

    def _workspace_busy_reason(self, owner):
        """One instrument owner across independent workspaces and connection tools."""
        if self.operator_page.busy:
            return "Wait for Auto Calibration to finish."
        if owner != "test" and getattr(self, "test_controller", None) and self.test_controller.busy:
            return "Wait for the test program to finish."
        if owner != "firmware" and getattr(self, "firmware_controller", None) and self.firmware_controller.busy:
            return "Wait for firmware programming to finish."
        if self.load_setup.busy:
            return "Wait for the load connection operation to finish."
        page = self.legacy_page
        if page._live_closing or page._live_pending_operator is not None or page._live_shutdown_pending:
            return "Wait for the active Manual Calibration operation to finish."
        if owner == "firmware" and (page.device_open or page._live_future is not None):
            return "Close the CAN device on Manual Calibration before firmware programming."
        if page._live_future is not None and page._live_operation not in ("receive", "readback_poll"):
            return "Wait for the current CAN command to finish."
        return ""

    def _operator_live_reason(self, process, *, record=None):
        if getattr(self, "firmware_controller", None) and self.firmware_controller.busy:
            return "Wait for firmware programming to finish."
        page = self.legacy_page
        if page.live_mode != "manual":
            return "Select Live manual control on Manual Calibration to run the charger."
        if page._live_closing:
            return "Waiting for the manual CAN connection to close."
        if page._live_pending_operator is not None or page._live_shutdown_pending:
            return "Wait for the queued CAN command to finish."
        if page._live_future is not None and page._live_operation not in ("receive", "readback_poll"):
            return "Wait for the current CAN command to finish."
        if self.load_setup.busy:
            return "Wait for the load connection check to finish."
        try:
            config = (self.load_setup.calibration_profile(for_calibration=False) if process == "test_only"
                      else self.load_setup.calibration_profile())
            error = AutomationSettings(**config["automation_settings"]).validate(calibration=process != "test_only")
            if error:
                return error
            if record is None:
                index = self.operator_page.variant.currentIndex()
                records = self.operator_page.records
                record = records[index] if 0 <= index < len(records) else None
            if record is not None:
                if record.get("simulation_only"):
                    return "Save an actual variant in Variant Recipes before running chargers."
                recipe = Recipe.from_dict(record["recipe"])
                error = (validate_test_recipe(recipe) if process == "test_only" else
                         recipe.validate_parameter_calibration() if process == "calibration" else recipe.validate_production())
                if error:
                    return "Variant recipe: " + error
                if recipe.max_voltage > 500 or recipe.max_current > 120 or recipe.max_power > 3700:
                    return "Variant limits exceed this M8437B's 500 V / 120 A / 3700 W ratings."
        except (ValueError, TypeError, KeyError) as exc:
            return str(exc)
        return ""

    def _operator_live_factory(self, recipe, process, cancel, *, test_start_method="external_restart", trace_sink=None):
        # Called on Qt's thread. Workers only use the frozen values below.
        config = copy.deepcopy(self.load_setup.calibration_profile(for_calibration=False) if process == "test_only"
                               else self.load_setup.calibration_profile())
        page = self.legacy_page
        can_index = int(page.device_index.currentText())
        bitrate = int(page.bitrate.currentText().lower().replace("kbps", "").strip()) * 1000
        settings = AutomationSettings(**config["automation_settings"])
        serial_settings = SerialSettings(config["port"], config["baudrate"], config["address"], config["parity"])
        monitor_factory = page._monitor_factory
        is_test = process in ("test", "post_test", "test_only")
        handoff = None if process in ("test", "post_test") else page.close_for_automatic_run()
        parameters = dict(recipe.device_settings)
        parameters.update(recipe.charger_values())

        def prepare():
            if handoff is not None:
                closed = handoff.result(timeout=15)
                if not isinstance(closed, dict) or closed.get("error") or closed.get("state") != "closed":
                    raise RuntimeError("Manual CAN did not confirm closing; the run was not started.")
            if cancel.is_set():
                raise RuntimeError("Start cancelled before opening the instruments.")
            bench = BenchSession(
                lambda: MaynuoLoad(serial_settings, recipe.max_voltage, recipe.max_current, recipe.max_power),
                monitor_factory=monitor_factory, can_index=can_index, bitrate=bitrate, settings=settings,
                protocol_approved=config["protocol_approved"], cc_mode_code=config["cc_mode_code"],
                cv_mode_code=config["cv_mode_code"], exclusive_confirmed=True,
                termination_ramp_approved=True, post_test_can_start=False, standalone_ready=is_test,
                test_start_method=test_start_method, trace_sink=trace_sink,
                charger_parameters=None if is_test else copy.deepcopy(parameters), cancellation_event=cancel)
            probe = bench.probe()
            if probe["identity"] != {"model_code": config["model_code"], "firmware_code": config["firmware_code"]}:
                raise RuntimeError("The load does not match this M8437B station profile.")
            return bench, settings
        return prepare

    def _start_shortcut(self):
        if self.tabs.currentWidget() is self.operator_page:
            self.operator_page.start("calibration")

    def _scan_shortcut(self):
        self.tabs.setCurrentWidget(self.operator_page)
        self.operator_page.serial.setFocus()
        self.operator_page.serial.selectAll()

    def _stop_shortcut(self):
        if self.firmware_controller.busy:
            self.firmware_controller.stop()
        elif self.test_controller.busy:
            self.test_controller.stop()
        else:
            self.operator_page.stop()

    def _finish_pending_close(self):
        if not self.operator_page.busy and not self.test_controller.busy and not self.firmware_controller.busy:
            self._close_timer.stop()
            self.close()

    def closeEvent(self, event):
        if self._closing:
            event.accept()
            return
        if self.operator_page.busy or self.test_controller.busy or self.firmware_controller.busy:
            self.operator_page.stop()
            self.test_controller.stop()
            self.firmware_controller.stop()
            self._close_timer.start()
            event.ignore()
            return
        self._closing = True
        self.operator_page.dispose()
        self.test_controller.dispose()
        self.firmware_controller.dispose()
        self.recipes.dispose()
        self.load_setup.dispose()
        self.update_page.dispose()
        self.legacy_page.dispose()
        event.accept()


def main(argv=None):
    parser = argparse.ArgumentParser()
    root = Path(__file__).resolve().parent.parent
    parser.add_argument("--data", type=Path, default=root / "calibration_data")
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--original-ui", action="store_true")
    parser.add_argument("--simulation", action="store_true")
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(f"{PRODUCT} {VERSION}")
    app.setWindowIcon(app_icon())
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Leanwatts.Testbench.Gen2")
    theme.load_fonts()
    app.setFont(theme.ui_font(14))
    app.setStyleSheet(theme.STYLESHEET)
    args.data.mkdir(parents=True, exist_ok=True)
    locks = []
    for path in (root / ".app-running.lock", args.data / ".station-running.lock"):
        lock = QLockFile(str(path))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            QMessageBox.information(None, "Already open", "This application or its station data is already open. Use the existing window.")
            return 1
        locks.append(lock)
    try:
        window = CalibrationWindow(args.data)
    except Exception as exc:
        QMessageBox.critical(None, "Could not open station", str(exc))
        return 1
    if not args.simulation:
        window.legacy_page.mode_select.setCurrentIndex(2)
    else:
        window.operator_page.set_mode("simulation")
    if args.original_ui:
        window.tabs.setCurrentWidget(window.legacy_page)
    size = app.primaryScreen().availableGeometry().size()
    window.resize(min(1400, size.width() - 16), min(900, size.height() - 36))
    window.show()
    if args.preview:
        def capture():
            args.preview.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(args.preview))
            window.close()
            app.quit()
        QTimer.singleShot(600, capture)
    result = app.exec()
    for lock in reversed(locks):
        lock.unlock()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
