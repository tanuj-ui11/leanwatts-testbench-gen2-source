r"""Check an extracted portable package without opening hardware or retaining data.

Run the copy inside the extracted package, using its own interpreter:
    runtime\python.exe tools\smoke_portable.py <package-root>
"""
from __future__ import annotations

import argparse
from importlib.machinery import ExtensionFileLoader, SourceFileLoader
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _embedded_qt_modules(root: Path) -> set[int]:
    """Identify Shiboken's in-memory archive, not a module-name allowlist.

    Shiboken 6.12 assigns relative virtual __file__ values to archive members.
    Its bootstrap also has a synthetic SourceFileLoader whose apparent source
    file does not exist. Recognize that bootstrap only through the archive's
    actual loader class, with the native Shiboken extension inside this package.
    Real files and ordinary filesystem/zipimport loaders still undergo checks.
    """
    native = sys.modules.get("shiboken6.Shiboken")
    native_spec = getattr(native, "__spec__", None)
    native_file = getattr(native, "__file__", None)
    if (not isinstance(getattr(native_spec, "loader", None), ExtensionFileLoader)
            or not native_file or not _under(Path(native_file), root)):
        return set()
    embedded = set()
    for module in list(sys.modules.values()):
        spec = getattr(module, "__spec__", None)
        loader = getattr(spec, "loader", None)
        archive = getattr(loader, "zfile", None)
        filename = getattr(module, "__file__", None)
        if (getattr(spec, "has_location", True) or getattr(spec, "origin", None) is not None
                or not isinstance(archive, zipfile.ZipFile)
                or not isinstance(archive.fp, io.BytesIO)
                or not filename or Path(filename).is_absolute()):
            continue
        try:
            archive.getinfo(filename)
        except KeyError:
            continue
        loader_class = type(loader)
        bootstrap = sys.modules.get(loader_class.__module__)
        bootstrap_spec = getattr(bootstrap, "__spec__", None)
        bootstrap_file = getattr(bootstrap, "__file__", None)
        bootstrap_origin = getattr(bootstrap_spec, "origin", None)
        code = getattr(getattr(loader_class, "exec_module", None), "__code__", None)
        if (getattr(bootstrap, loader_class.__name__, None) is not loader_class
                or not isinstance(getattr(bootstrap_spec, "loader", None), SourceFileLoader)
                or not bootstrap_file or Path(bootstrap_file).is_absolute()
                or not bootstrap_origin or Path(bootstrap_origin).exists()
                or Path(bootstrap_origin).name != bootstrap_file
                or getattr(code, "co_filename", None) != bootstrap_file):
            continue
        embedded.update((id(module), id(bootstrap)))
    return embedded


def _check_isolation(root: Path) -> dict:
    problems = []
    executable = Path(sys.executable).resolve()
    if not _under(executable, root):
        problems.append("The interpreter is outside the package: " + str(executable))
    paths = []
    for entry in sys.path:
        path = Path(entry or os.getcwd()).resolve()
        paths.append(str(path))
        if not _under(path, root):
            problems.append("An import search path is outside the package: " + str(path))
        normalized = str(path).replace("\\", "/").lower()
        if "/_internal/app" in normalized:
            problems.append("The original application's import path is present: " + str(path))
    checked = 0
    embedded = _embedded_qt_modules(root)
    embedded_names = []
    for name, module in list(sys.modules.items()):
        if name == "lw_eol" or name.startswith("lw_eol."):
            problems.append("An original-application module was imported: " + name)
        if module is None:
            continue
        spec = getattr(module, "__spec__", None)
        if getattr(spec, "origin", None) in {"built-in", "frozen"}:
            continue
        if id(module) in embedded:
            embedded_names.append(name)
            continue
        filename = getattr(module, "__file__", None)
        if filename:
            checked += 1
            if not _under(Path(filename), root):
                problems.append(f"Module {name} is outside the package: {filename}")
        for location in getattr(module, "__path__", ()):
            if not _under(Path(location), root):
                problems.append(f"Package {name} searches outside the package: {location}")
    if problems:
        raise RuntimeError("; ".join(problems))
    return {"status": "passed", "local_modules_checked": checked,
            "embedded_qt_modules": sorted(embedded_names), "sys_path": paths}


def _exercise_gui(root: Path) -> dict:
    # Force a headless platform before Qt imports, irrespective of the host setup.
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["PYQTGRAPH_QT_LIB"] = "PySide6"
    os.environ["QT_PLUGIN_PATH"] = str(root / "runtime/Lib/site-packages/PySide6/plugins")
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(
        root / "runtime/Lib/site-packages/PySide6/plugins/platforms"
    )

    import PySide6
    import shiboken6
    import numpy
    import colorama
    import pyqtgraph
    from PySide6.QtCore import QTimer, qVersion
    from PySide6.QtWidgets import QApplication
    from leanwatts_can import device_diagnostics

    def hardware_forbidden():
        raise RuntimeError("Hardware diagnostics must not run during portable validation.")

    device_diagnostics.check_adapter = hardware_forbidden
    from leanwatts_can import theme
    from leanwatts_can.gui import CalibrationWindow

    dependencies = {
        name: {"version": str(module.__version__), "path": str(Path(module.__file__).resolve())}
        for name, module in {
            "PySide6": PySide6, "shiboken6": shiboken6, "numpy": numpy,
            "colorama": colorama, "pyqtgraph": pyqtgraph,
        }.items()
    }
    app = QApplication.instance() or QApplication(["portable-smoke-check"])
    if app.platformName() != "offscreen":
        raise RuntimeError("The Qt platform is not offscreen.")
    theme.load_fonts()
    app.setFont(theme.ui_font(14))
    app.setStyleSheet(theme.STYLESHEET)
    pages = []
    errors = []

    with tempfile.TemporaryDirectory(prefix="can-portable-smoke-") as temporary:
        window = CalibrationWindow(Path(temporary) / "data")
        window.timer.stop()
        window.resize(1024, 650)
        window.tabs.setCurrentIndex(0)
        window.show()

        def fail(message):
            errors.append(message)
            window.close()
            app.exit(1)

        def capture_page(expected):
            name = window.tabs.tabText(window.tabs.currentIndex())
            if name != expected:
                raise RuntimeError(f"Expected page {expected!r}, found {name!r}.")
            if window._adapter_future is not None:
                raise RuntimeError("The GUI unexpectedly requested hardware diagnostics.")
            picture = window.grab().toImage()
            if picture.isNull() or picture.width() <= 0 or picture.height() <= 0:
                raise RuntimeError("Qt could not render page " + expected)
            if window.width() > 1024 or window.height() > 650:
                raise RuntimeError("The window exceeds the requested 1024 x 650 size.")
            pages.append({"name": name, "width": window.width(), "height": window.height()})

        def finish():
            try:
                if window.tabs.currentIndex() != 0:
                    raise RuntimeError("The default calibration page was not restored.")
                window.close()
                app.exit(0)
            except Exception as error:
                fail(str(error))

        def inspect_original():
            try:
                capture_page("Original UI · English")
                window.tabs.setCurrentIndex(0)
                QTimer.singleShot(50, finish)
            except Exception as error:
                fail(str(error))

        def inspect_default():
            try:
                capture_page("Operator")
                window.tabs.setCurrentWidget(window.legacy_page)
                QTimer.singleShot(50, inspect_original)
            except Exception as error:
                fail(str(error))

        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(lambda: fail("Qt smoke validation timed out."))
        timeout.start(5000)
        QTimer.singleShot(50, inspect_default)
        try:
            exit_code = app.exec()
        finally:
            timeout.stop()
            window.close()
            app.processEvents()
        if errors or exit_code:
            raise RuntimeError("; ".join(errors) or f"Qt returned exit code {exit_code}.")

    return {
        "status": "passed", "qt_version": qVersion(), "platform": app.platformName(),
        "rendered_pages": pages, "data": "temporary; removed",
        "hardware_check_requested": False, "dependencies": dependencies,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_root", type=Path)
    args = parser.parse_args()
    result = {"status": "failed", "python": sys.version.split()[0], "executable": sys.executable}
    try:
        root = args.package_root.resolve(strict=True)
        result["package_root"] = str(root)
        if not root.is_dir() or not (root / "leanwatts_can/gui.py").is_file():
            raise RuntimeError("The supplied directory is not a CAN Calibration package.")
        if not _under(Path(__file__), root):
            raise RuntimeError("Run the validator copy inside the extracted package's tools directory.")
        sys.path.insert(0, str(root))
        _check_isolation(root)
        result["gui"] = _exercise_gui(root)
        result["isolation"] = _check_isolation(root)
        result["status"] = "passed"
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
