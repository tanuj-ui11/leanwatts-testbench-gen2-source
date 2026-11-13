"""Compact official Leanwatts branding, loaded from packaged local assets."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget
from . import theme

ASSETS = Path(__file__).resolve().parent / "assets"


def app_icon():
    return QIcon(str(ASSETS / "leanwatts.ico"))


def _set_logo(label, height, light):
    filename = "leanwatts-logo-light.png" if light else "leanwatts-logo.png"
    pixmap = QPixmap(str(ASSETS / filename))
    if not pixmap.isNull():
        display = pixmap.scaledToHeight(height * 2, Qt.TransformationMode.SmoothTransformation)
        display.setDevicePixelRatio(2)
        label.setPixmap(display)
        label.setFixedSize(round(display.width() / 2), height)
    else:
        label.setText("Leanwatts")


def logo_label(height=34, light=None):
    label = QLabel()
    label.setAccessibleName("Leanwatts logo")
    label.setToolTip("Leanwatts")
    label.setProperty("brandLogoHeight", height)
    label.setProperty("brandFixedVariant", light is not None)
    use_light = theme.CURRENT_THEME.endswith("_light") if light is None else light
    _set_logo(label, height, use_light)
    return label


def refresh_branding(app):
    """Use the official dark/white wordmark appropriate to the active surface."""
    light = theme.CURRENT_THEME.endswith("_light")
    for widget in app.allWidgets():
        height = widget.property("brandLogoHeight")
        if height and not widget.property("brandFixedVariant"):
            _set_logo(widget, height, light)


def symbol_label(size=14):
    label = QLabel()
    label.setAccessibleName("Leanwatts mark")
    pixmap = QPixmap(str(ASSETS / "leanwatts-symbol.png"))
    display = pixmap.scaled(size * 2, size * 2, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
    display.setDevicePixelRatio(2)
    label.setPixmap(display)
    label.setFixedSize(size, size)
    return label


def footer_widget():
    footer = QWidget()
    row = QHBoxLayout(footer)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    row.addStretch()
    row.addWidget(symbol_label(12))
    text = QLabel("LEANWATTS  ·  TESTBENCH GEN2")
    text.setStyleSheet(f"color: {theme.MUTED}; font-size: 10px; letter-spacing: 1px;")
    row.addWidget(text)
    footer.setFixedHeight(14)
    return footer
