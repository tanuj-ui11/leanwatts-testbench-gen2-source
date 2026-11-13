"""Verify the assembled release and open its actual Qt widgets without devices."""
from pathlib import Path
import json
import os
import subprocess
import sys
import zipfile
import argparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from build_gen2_delta import read_release

parser = argparse.ArgumentParser()
parser.add_argument("--version", default="1.1")
args = parser.parse_args()
package = ROOT / f"dist/Leanwatts-Testbench-Gen2-{args.version}-win64.zip"
files, manifest, _, version = read_release(package)
work = ROOT / f"artifacts/gen2-{args.version}-packaged-smoke"
if work.exists():
    raise SystemExit("Preserving the existing package verification folder.")
work.mkdir()
with zipfile.ZipFile(package) as archive:
    # read_release has already rejected unsafe, unlisted and duplicate paths.
    archive.extractall(work)
app_root = work / "CAN-Calibration"
smoke = work / "check_widgets.py"
smoke.write_text('''from pathlib import Path
import json, os, sys, subprocess
os.environ["QT_QPA_PLATFORM"] = "offscreen"
root = Path(__file__).resolve().parent / "CAN-Calibration"
sys.path.insert(0, str(root))
from leanwatts_can import updater, theme
from leanwatts_can.can_monitor import MonitorClient
from leanwatts_can.maynuo import MaynuoLoad
forbidden_calls = []
def forbidden(*args, **kwargs):
    forbidden_calls.append(True)
    raise AssertionError("Device access is forbidden in the package check.")
MonitorClient.open = forbidden
MaynuoLoad.open = forbidden
version_tuple = tuple(map(int, sys.argv[2].split(".")))
has_programs = version_tuple >= (1, 1, 6)
has_firmware = version_tuple >= (1, 1, 7)
has_cli = version_tuple >= (1, 1, 8)
if has_cli:
    subprocess.Popen = forbidden
    subprocess.run = forbidden
if has_firmware:
    from leanwatts_can import gdlink, firmware_controller
    gdlink.discover_programmers = forbidden
    gdlink.ProgrammingJob.run = forbidden
    firmware_controller.discover_programmers = forbidden
if has_cli:
    from leanwatts_can import gdlink_cli
    gdlink_cli.discover_programmers = forbidden
    gdlink_cli._windows_usb = forbidden
    gdlink_cli._run_console = forbidden
    gdlink_cli.ProgrammingJob.run = forbidden
from PySide6.QtWidgets import QApplication
from leanwatts_can.gui import CalibrationWindow
app = QApplication([])
theme.load_fonts()
app.setFont(theme.ui_font(14))
app.setStyleSheet(theme.STYLESHEET)
window = CalibrationWindow(root.parent / "isolated-station-data")
window.legacy_page.mode_select.setCurrentIndex(2)
window.resize(1366, 768)
window.show()
app.processEvents()
labels = [window.tabs.tabText(i) for i in range(window.tabs.count())]
assert len(labels) == (8 if has_firmware else 7 if has_programs else 6), labels
assert window.update_page.local_button.isEnabled()
assert not window.update_page.install_button.isEnabled()
assert window.windowTitle() == "Leanwatts Testbench Gen2 " + sys.argv[2]
assert not window.windowIcon().isNull()
if version_tuple >= (1, 1, 10):
    from leanwatts_can.session_page import SessionModeDialog
    page = window.operator_page
    assert " ".join(page.calibrate.text().split()) == "Parameters + Calibration"
    assert page.test_only.text() == "Test"
    assert not hasattr(page, "calibrate_and_test")
    modes = SessionModeDialog(window)
    expected_modes = ["calibration_only", "test_only", "independent", "both_required"] if version_tuple >= (1, 1, 11) else ["independent", "both_required"]
    assert [modes.mode_select.itemData(i) for i in range(modes.mode_select.count())] == expected_modes
    assert modes.mode == "independent"
    modes.mode_select.setCurrentIndex(modes.mode_select.findData("both_required"))
    assert modes.mode == "both_required"
    modes.close()
    if version_tuple >= (1, 1, 12):
        assert len(page.STAGES) == 7
        assert page.STAGES[-2:] == ("Open-circuit voltage test", "Full-current test")
    elif version_tuple >= (1, 1, 11):
        assert page.STAGES[-3:] == ("Open-circuit voltage test", "Full-current test", "End-current cutoff test")
if hasattr(window, "appearance_page"):
    assert window.settings_page.indexOf(window.appearance_page) >= 0
    assert window.settings_page.indexOf(window.about_page) >= 0
    for key in ("blue", "teal", "purple_light", "blue_light", "teal_light", "purple"):
        theme.apply_theme(key)
        app.processEvents()
    theme.save_theme(window.data_dir, "blue_light")
    assert theme.load_saved_theme(window.data_dir) == "blue_light"
    theme.apply_theme("purple")
assert window.grab().save(str(root.parent / "packaged-session.png"))
program_files_checked = False
if has_programs:
    from leanwatts_can.test_programs import DEFAULT_FILENAME, load_program
    assert "Test" in labels
    assert window.tabs.indexOf(window.test_page) == labels.index("Test")
    bundled = root / "leanwatts_can" / "test_programs"
    installed = window.test_page.programs_dir
    assert (bundled / DEFAULT_FILENAME).is_file()
    assert (bundled / "README.txt").is_file()
    assert (installed / DEFAULT_FILENAME).read_bytes() == (bundled / DEFAULT_FILENAME).read_bytes()
    program = load_program(installed / DEFAULT_FILENAME)
    assert program["program"]["workflow"] == "full_cycle"
    expected_method = "external_restart" if version_tuple >= (1, 1, 10) else "normal" if version_tuple >= (1, 1, 9) else "calibration_start_only"
    assert program["program"]["testing"]["start_method"] == expected_method
    if version_tuple >= (1, 1, 9):
        expected_program = "05-ocv-and-full-current.json" if version_tuple >= (1, 1, 12) else "04-ocv-current-fixed-cutoff.json" if version_tuple >= (1, 1, 11) else "03-full-cycle-external-restart.json" if version_tuple >= (1, 1, 10) else "02-full-cycle-normal-start.json"
        assert DEFAULT_FILENAME == expected_program
        assert not (bundled / "01-full-cycle-calibration-mode.json").exists()
        assert program["program"]["baseline"]["minimum_ramp_seconds"] >= 18
        if version_tuple < (1, 1, 11):
            assert program["program"]["testing"]["cutoff_observation_seconds"] >= 10
    if version_tuple >= (1, 1, 10):
        assert not (bundled / "02-full-cycle-normal-start.json").exists()
    if version_tuple >= (1, 1, 11):
        assert not (bundled / "03-full-cycle-external-restart.json").exists()
        if version_tuple >= (1, 1, 12):
            assert not (bundled / "04-ocv-current-fixed-cutoff.json").exists()
            assert len(window.test_page.STAGES) == 7
            assert window.test_page.STAGES[-2:] == ("Open-circuit voltage", "Full current")
        else:
            assert window.test_page.STAGES[-3:] == ("Open-circuit voltage", "Full current", "End-current cutoff")
        assert program["program"]["testing"]["procedure"] == ("ocv_full_current_v1" if version_tuple >= (1, 1, 12) else "ocv_current_fixed_cutoff_v1")
        assert set(program["program"]["testing"]) == {"start_method", "procedure", "restart_timeout_seconds"}
    assert window.test_page.programs.currentText() == DEFAULT_FILENAME
    assert not window.test_page.start_button.isEnabled()
    assert not window.test_page.copy_button.isEnabled()
    window.tabs.setCurrentWidget(window.test_page)
    app.processEvents()
    assert window.grab().save(str(root.parent / "packaged-test.png"))
    program_files_checked = True
firmware_page_checked = False
if has_firmware:
    assert "Firmware Programming" in labels
    assert window.tabs.indexOf(window.firmware_page) == labels.index("Firmware Programming")
    window.tabs.setCurrentWidget(window.firmware_page)
    app.processEvents()
    assert window.grab().save(str(root.parent / "packaged-firmware.png"))
    firmware_page_checked = True
release = updater.local_update(Path(sys.argv[1]))
assert release["version"] == sys.argv[2] and release["source"] == "local"
window.close()
app.processEvents()
assert not forbidden_calls, "Application attempted hardware discovery or a process launch during the UI check."
print(json.dumps(dict(packaged_ui_opened=True, tabs=labels,
    real_installer_local_validation=True, hardware_access=False,
    test_program_files_verified=program_files_checked,
    firmware_page_no_device_discovery=firmware_page_checked,
    official_cli_not_executed=has_cli)))
''', encoding="utf-8")
env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
result = subprocess.run([str(app_root / "runtime/python.exe"), str(smoke),
    str(ROOT / f"dist/Leanwatts-Testbench-Gen2-Setup-{args.version}.exe"), args.version],
    env=env, cwd=work, text=True, capture_output=True, timeout=60, check=True)
report = dict(version=version, manifest_files=len(manifest), full_archive_verified=True,
    installer_executed=False, **json.loads(result.stdout))
(work / "validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
