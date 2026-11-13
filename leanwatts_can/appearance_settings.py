"""Station appearance choices. A preference change never touches instruments."""
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel,
                               QRadioButton, QVBoxLayout, QWidget)

from . import theme, branding


class AppearanceSettings(QWidget):
    theme_changed = Signal(str)

    def __init__(self, data_dir, parent=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        card = theme.Card("Appearance", "Saved for this station")
        note = QLabel("Choose a display mode and accent colour. Changes apply immediately.")
        note.setWordWrap(True)
        card.body.addWidget(note)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Display mode"))
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for index, (mode, label) in enumerate((("dark", "Dark"), ("light", "Light"))):
            button = QRadioButton(label)
            button.setChecked(theme.is_dark() == (mode == "dark"))
            button.toggled.connect(lambda checked, chosen=mode: self._select_mode(chosen) if checked else None)
            self.mode_group.addButton(button, index)
            self.mode_buttons[mode] = button
            mode_row.addWidget(button)
        mode_row.addStretch()
        card.body.addLayout(mode_row)
        accent_caption = QLabel("Accent colour")
        accent_caption.setObjectName("Caption")
        card.body.addWidget(accent_caption)
        choices = QHBoxLayout()
        choices.setSpacing(16)
        self.group = QButtonGroup(self)
        self.buttons = {}
        self.previews = {}
        for index, accent in enumerate(theme.ACCENT_KEYS):
            column = QVBoxLayout()
            column.setSpacing(12)
            preview = QFrame()
            preview.setFixedHeight(165)
            preview.setProperty("keepPreviewColors", True)
            sample = QVBoxLayout(preview)
            sample.setContentsMargins(17, 16, 17, 16)
            wordmark = branding.logo_label(height=23)
            wordmark.setStyleSheet("background: transparent; border: none;")
            wordmark.setProperty("keepPreviewColors", True)
            sample.addWidget(wordmark)
            bar = QFrame()
            bar.setFixedHeight(35)
            bar.setProperty("keepPreviewColors", True)
            sample.addWidget(bar)
            button = QLabel("START CALIBRATION + TEST")
            button.setProperty("keepPreviewColors", True)
            sample.addWidget(button)
            column.addWidget(preview)
            radio = QRadioButton(theme.THEMES[accent]["name"])
            radio.setChecked(accent == theme.CURRENT_THEME.removesuffix("_light"))
            radio.toggled.connect(lambda checked, chosen=accent: self._select_accent(chosen) if checked else None)
            self.group.addButton(radio, index)
            self.buttons[accent] = radio
            column.addWidget(radio)
            choices.addLayout(column, 1)
            self.previews[accent] = (preview, bar, button)
        card.body.addLayout(choices)
        self.status = QLabel()
        self.status.setObjectName("Caption")
        self.status.setWordWrap(True)
        card.body.addWidget(self.status)
        note = QLabel("PASS, FAIL and warning colours keep the same meaning in both modes. The original Manual Calibration panel keeps its familiar layout.")
        note.setObjectName("Caption")
        note.setWordWrap(True)
        card.body.addWidget(note)
        body.addWidget(card)
        body.addStretch()
        self._sync_controls()

    def _sync_controls(self, *, saved=False):
        accent = theme.CURRENT_THEME.removesuffix("_light")
        dark = theme.is_dark()
        for name, button in self.buttons.items():
            button.blockSignals(True)
            button.setChecked(name == accent)
            button.blockSignals(False)
        for mode, button in self.mode_buttons.items():
            button.blockSignals(True)
            button.setChecked(dark == (mode == "dark"))
            button.blockSignals(False)
        for name, (preview, bar, button) in self.previews.items():
            palette = theme.THEMES[theme.theme_key(name, dark)]
            preview.setStyleSheet(f"QFrame {{ background: {palette['BG_WINDOW']}; border: 1px solid {palette['BORDER_INPUT']}; border-radius: 12px; }}")
            bar.setStyleSheet(f"background: {palette['BG_CARD']}; border: 1px solid {palette['BORDER_CARD']}; border-radius: 7px;")
            button.setStyleSheet(f"background: {palette['ACCENT']}; color: {palette['ON_ACCENT']}; border: none; border-radius: 7px; padding: 9px; font-size: 11px; font-weight: 600;")
        self.status.setText(("Dark" if dark else "Light") + " mode · " + theme.THEMES[accent]["name"] + (" · Saved" if saved else ""))

    def _select_accent(self, accent):
        self._select(theme.theme_key(accent, theme.is_dark()))

    def _select_mode(self, mode):
        self._select(theme.theme_key(theme.CURRENT_THEME.removesuffix("_light"), mode == "dark"))

    def _select(self, key):
        try:
            theme.save_theme(self.data_dir, key)
            theme.apply_theme(key)
            self._sync_controls(saved=True)
            self.theme_changed.emit(key)
        except (OSError, ValueError) as exc:
            self._sync_controls()
            self.status.setText("Could not save the theme: " + str(exc))
