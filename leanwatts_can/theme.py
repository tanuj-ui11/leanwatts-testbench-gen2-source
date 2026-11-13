"""LeanWatts light/dark design system with three accent palettes.

Every status keeps a symbol and a word next to its colour (EoL PRD 4.1:
colour-blind-safe status colours plus icons/text).
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import re

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontDatabase, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

# ------------------------------------------------------------------ tokens
BG_WINDOW = "#110E18"
BG_TITLE = "#0C0A12"
BG_CARD = "#1C1828"
BG_INPUT = "#252034"
BG_ROW_ALT = "#201B2E"
BORDER_CARD = "#2A2440"
BORDER_INPUT = "#3A3253"
TEXT = "#F3F0FA"
TEXT2 = "#ABA5C0"
MUTED = "#8E88A6"
DATA = "#D6D0E6"
ACCENT = "#A78BFA"
ACCENT_SOFT = "#D9CCFF"
INFO = "#B9D1FF"
INFO_BASE = "#6CA0FF"
SUCCESS = "#6EE7A8"
DANGER = "#FF8A8A"
WARNING = "#F5B656"
HIGHLIGHT = "#F2A7CF"
ON_ACCENT = "#17131F"

UI_FONT = "Outfit"
MONO_FONT = "JetBrains Mono"

# Legacy names used by other modules.
GREEN, VERMILLION, ORANGE, BLUE, PURPLE, GREY, INK, PANEL = (
    SUCCESS,
    DANGER,
    WARNING,
    ACCENT,
    HIGHLIGHT,
    MUTED,
    TEXT,
    BG_INPUT,
)

STATUS: dict[str, tuple[str, str, str]] = {
    # key: (symbol, label, colour) - symbol + label always shown with the colour
    "PASS": ("✔", "PASS", SUCCESS),
    "OK": ("✔", "OK", SUCCESS),
    "FAIL": ("✖", "FAIL", DANGER),
    "ERROR": ("⚠", "ERROR", HIGHLIGHT),
    "ABORTED": ("■", "ABORTED", WARNING),
    "BLOCKED": ("⛔", "BLOCKED", WARNING),
    "SKIPPED": ("↷", "SKIP", MUTED),
    "NOT_RUN": ("—", "", MUTED),
    "RUNNING": ("▶", "RUNNING", ACCENT_SOFT),
    "INFO": ("•", "INFO", TEXT2),
    "IN PROGRESS": ("▶", "IN PROGRESS", ACCENT_SOFT),
    # recipe workflow and feasibility
    "approved": ("✔", "approved", SUCCESS),
    "draft": ("✎", "draft", WARNING),
    "retired": ("·", "retired", MUTED),
    "EXECUTABLE": ("✔", "EXECUTABLE", SUCCESS),
    "REDUCED": ("◐", "REDUCED", WARNING),
    "NOT_EXECUTABLE": ("✖", "NOT_EXECUTABLE", DANGER),
}

STATUS_ROLE = Qt.ItemDataRole.UserRole + 7

# Colours retain their semantic meaning in both display modes. The translated
# original manual panel supplies its own independent stylesheet.
_PALETTE_TOKENS = ("BG_WINDOW", "BG_TITLE", "BG_CARD", "BG_INPUT", "BG_ROW_ALT",
                   "BORDER_CARD", "BORDER_INPUT", "TEXT", "TEXT2", "MUTED", "DATA",
                   "ACCENT", "ACCENT_SOFT", "ON_ACCENT", "INFO", "INFO_BASE",
                   "SUCCESS", "DANGER", "WARNING", "HIGHLIGHT")
ACCENT_KEYS = ("purple", "blue", "teal")
_DARK_STATUS = {key: globals()[key] for key in ("INFO", "INFO_BASE", "SUCCESS", "DANGER", "WARNING", "HIGHLIGHT")}
THEMES = {
    "purple": {"name": "Leanwatts Purple", "description": "The original dark theme.",
               **{key: globals()[key] for key in _PALETTE_TOKENS}},
    "blue": {"name": "Ocean Blue", "description": "Cool blue with slate surfaces.", **_DARK_STATUS,
             "BG_WINDOW": "#0E141E", "BG_TITLE": "#0A1019", "BG_CARD": "#172130",
             "BG_INPUT": "#202E41", "BG_ROW_ALT": "#1B2738", "BORDER_CARD": "#25344A",
             "BORDER_INPUT": "#354B66", "TEXT": "#F0F5FB", "TEXT2": "#AAB9CE",
             "MUTED": "#879BB6", "DATA": "#CDDCEC", "ACCENT": "#83B6FA",
             "ACCENT_SOFT": "#C7E0FF", "ON_ACCENT": "#101C2C"},
    "teal": {"name": "Quiet Teal", "description": "Soft teal with deep green surfaces.", **_DARK_STATUS,
             "BG_WINDOW": "#0E181A", "BG_TITLE": "#0A1214", "BG_CARD": "#162629",
             "BG_INPUT": "#203337", "BG_ROW_ALT": "#1B2D30", "BORDER_CARD": "#283E42",
             "BORDER_INPUT": "#38565B", "TEXT": "#EFF8F7", "TEXT2": "#ABC3C4",
             "MUTED": "#88A8AA", "DATA": "#CDE2E2", "ACCENT": "#75CFC6",
             "ACCENT_SOFT": "#B9EAE5", "ON_ACCENT": "#102624"},
}
_LIGHT_STATUS = {"INFO": "#275785", "INFO_BASE": "#2764A5", "SUCCESS": "#247749",
                 "DANGER": "#B53B43", "WARNING": "#8A5D08", "HIGHLIGHT": "#9E3975"}
THEMES.update({
    "purple_light": {"name": "Leanwatts Purple", "description": "A soft purple accent.", **_LIGHT_STATUS,
        "BG_WINDOW": "#F3F0F7", "BG_TITLE": "#EBE6F2", "BG_CARD": "#FEFEFF",
        "BG_INPUT": "#F5F3FA", "BG_ROW_ALT": "#F6F3FA", "BORDER_CARD": "#DED8EA",
        "BORDER_INPUT": "#BDB3CF", "TEXT": "#241B35", "TEXT2": "#584F68",
        "MUTED": "#70647F", "DATA": "#3D3150", "ACCENT": "#7046B7",
        "ACCENT_SOFT": "#5D369F", "ON_ACCENT": "#FFFFFF"},
    "blue_light": {"name": "Ocean Blue", "description": "A clear blue accent.", **_LIGHT_STATUS,
        "BG_WINDOW": "#F0F4F8", "BG_TITLE": "#E5ECF3", "BG_CARD": "#FEFEFF",
        "BG_INPUT": "#F2F6FA", "BG_ROW_ALT": "#F2F5F9", "BORDER_CARD": "#D5DFEA",
        "BORDER_INPUT": "#A9BBD0", "TEXT": "#1E2A3B", "TEXT2": "#4C5D73",
        "MUTED": "#62738A", "DATA": "#32455E", "ACCENT": "#1F65B0",
        "ACCENT_SOFT": "#20548C", "ON_ACCENT": "#FFFFFF"},
    "teal_light": {"name": "Quiet Teal", "description": "A balanced teal accent.", **_LIGHT_STATUS,
        "BG_WINDOW": "#EFF5F4", "BG_TITLE": "#E3EEEC", "BG_CARD": "#FEFEFF",
        "BG_INPUT": "#F1F7F6", "BG_ROW_ALT": "#F1F6F5", "BORDER_CARD": "#D1E1DE",
        "BORDER_INPUT": "#A5BFBA", "TEXT": "#1D302F", "TEXT2": "#4A625F",
        "MUTED": "#607B76", "DATA": "#304F4A", "ACCENT": "#147369",
        "ACCENT_SOFT": "#175F57", "ON_ACCENT": "#FFFFFF"},
})
CURRENT_THEME = "purple"


def is_dark(key=None) -> bool:
    return not (key or CURRENT_THEME).endswith("_light")


def theme_key(accent, dark=True) -> str:
    if accent not in ACCENT_KEYS:
        raise ValueError("Choose one of the available accent colours.")
    return accent if dark else accent + "_light"


def _remap_inline_styles(app, previous, selected):
    # Inline progress bars and badges were built from the palette before the
    # choice changed. Translate their tokens, preserving the status meanings.
    replacements = {}
    for key in _PALETTE_TOKENS:
        old, new = previous[key], selected[key]
        if old == new:
            continue
        replacements[old.lower()] = new
        for alpha in (.14, .16, .18, .22, .25, .40, .45):
            replacements[rgba(old, alpha).lower()] = rgba(new, alpha)
    if not replacements:
        return
    pattern = re.compile("|".join(re.escape(s) for s in sorted(replacements, key=len, reverse=True)), re.I)
    for widget in app.allWidgets():
        if widget.property("keepPreviewColors"):
            continue
        css = widget.styleSheet()
        if css:
            updated = pattern.sub(lambda match: replacements[match.group().lower()], css)
            if updated != css:
                widget.setStyleSheet(updated)
        if isinstance(widget, QTableWidget):
            for row in range(widget.rowCount()):
                for column in range(widget.columnCount()):
                    item = widget.item(row, column)
                    if item is None:
                        continue
                    for role in (Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.BackgroundRole):
                        brush = item.data(role)
                        if isinstance(brush, QBrush) and brush.style() != Qt.BrushStyle.NoBrush:
                            replacement = replacements.get(brush.color().name().lower())
                            if replacement:
                                item.setData(role, QBrush(QColor(replacement)))


def _apply_palette(app):
    palette = QPalette()
    values = {"Window": BG_WINDOW, "WindowText": TEXT, "Base": BG_INPUT,
              "AlternateBase": BG_ROW_ALT, "Text": TEXT, "Button": BG_CARD,
              "ButtonText": TEXT, "Highlight": ACCENT, "HighlightedText": ON_ACCENT,
              "ToolTipBase": BG_INPUT, "ToolTipText": TEXT, "PlaceholderText": MUTED}
    for role, value in values.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(value))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(MUTED))
    app.setPalette(palette)


def apply_theme(key: str, app=None) -> str:
    """Apply a known palette; updates existing widgets without rebuilding them."""
    global CURRENT_THEME, STYLESHEET, GREEN, VERMILLION, ORANGE, BLUE, PURPLE, GREY, INK, PANEL
    if key not in THEMES:
        raise ValueError("Choose one of the available colour themes.")
    previous = {name: globals()[name] for name in _PALETTE_TOKENS}
    selected = THEMES[key]
    globals().update({name: selected[name] for name in _PALETTE_TOKENS})
    CURRENT_THEME = key
    GREEN, VERMILLION, ORANGE, BLUE, PURPLE, GREY, INK, PANEL = (
        SUCCESS, DANGER, WARNING, ACCENT, HIGHLIGHT, MUTED, TEXT, BG_INPUT)
    for status in ("SKIPPED", "NOT_RUN", "retired"):
        symbol, label, _ = STATUS[status]
        STATUS[status] = (symbol, label, MUTED)
    for status in ("RUNNING", "IN PROGRESS"):
        symbol, label, _ = STATUS[status]
        STATUS[status] = (symbol, label, ACCENT_SOFT)
    symbol, label, _ = STATUS["INFO"]
    STATUS["INFO"] = (symbol, label, TEXT2)
    for statuses, colour in ((("PASS", "OK", "approved", "EXECUTABLE"), SUCCESS),
                             (("FAIL", "NOT_EXECUTABLE"), DANGER),
                             (("ERROR",), HIGHLIGHT),
                             (("ABORTED", "BLOCKED", "draft", "REDUCED"), WARNING)):
        for status in statuses:
            symbol, label, _ = STATUS[status]
            STATUS[status] = (symbol, label, colour)
    STYLESHEET = _build_stylesheet()
    app = app or QApplication.instance()
    if app is not None:
        _remap_inline_styles(app, previous, selected)
        _apply_palette(app)
        app.setStyleSheet(STYLESHEET)
        from . import branding
        if hasattr(branding, "refresh_branding"):
            branding.refresh_branding(app)
    return key


def load_saved_theme(data_dir: Path) -> str:
    """Read this station's display preference; damaged/missing preferences use purple."""
    try:
        value = json.loads((Path(data_dir) / "appearance.json").read_text(encoding="utf-8"))
        key = value.get("theme", "purple")
    except (OSError, ValueError, TypeError, AttributeError):
        key = "purple"
    return apply_theme(key if isinstance(key, str) and key in THEMES else "purple")


def save_theme(data_dir: Path, key: str) -> None:
    if key not in THEMES:
        raise ValueError("Choose one of the available colour themes.")
    path = Path(data_dir) / "appearance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"schema": 1, "theme": key}, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def status_text(outcome: str, flagged: bool = False) -> str:
    sym, label, _ = STATUS.get(outcome, ("?", outcome, TEXT))
    return f"{sym} {label}{'*' if flagged else ''}".strip()


def status_color(outcome: str) -> QColor:
    return QColor(STATUS.get(outcome, ("", "", TEXT))[2])


def rgba(color: str, alpha: float) -> str:
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha:.2f})"


def pill(text: str, color: str, size: int = 13) -> QLabel:
    """A status pill label that keeps its natural size."""
    lbl = QLabel(text)
    lbl.setStyleSheet(pill_css(color, 0.14, size))
    lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return lbl


def pill_css(color: str, alpha: float = 0.14, size: int = 12) -> str:
    return (
        f"background: {rgba(color, alpha)}; color: {color}; border-radius: 11px; "
        f"padding: 3px 12px; font-size: {size}px; font-weight: 600;"
    )


# ------------------------------------------------------------------ fonts
FONT_DIR = Path(__file__).resolve().parent / "fonts"


def load_fonts() -> None:
    for f in ("Outfit.ttf", "JetBrainsMono.ttf"):
        p = FONT_DIR / f
        if p.exists():
            QFontDatabase.addApplicationFont(str(p))


def ui_font(px: int = 14, weight: QFont.Weight = QFont.Weight.Normal, tabular: bool = True) -> QFont:
    f = QFont(UI_FONT)
    f.setPixelSize(px)
    f.setWeight(weight)
    if tabular:
        try:
            f.setFeature(QFont.Tag("tnum"), 1)
        except (AttributeError, TypeError):  # older Qt: proportional numerals
            pass
    return f


def mono(widget: QWidget) -> QWidget:
    """Mark a widget for JetBrains Mono (IDs, hex, timestamps, logs)."""
    widget.setObjectName("Mono")
    widget.setFont(mono_font(12))
    return widget


def mono_font(px: int = 13) -> QFont:
    f = QFont(MONO_FONT)
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPixelSize(px)
    return f


# ------------------------------------------------------------------ widgets
class Card(QFrame):
    """Rounded panel with an optional title row (title left, note right)."""

    def __init__(self, title: str = "", note: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(22, 18, 22, 20)
        self.body.setSpacing(12)
        self.title_lbl: QLabel | None = None
        self.note_lbl: QLabel | None = None
        if title:
            row = QHBoxLayout()
            self.title_lbl = QLabel(title)
            self.title_lbl.setObjectName("CardTitle")
            row.addWidget(self.title_lbl)
            row.addStretch(1)
            self.note_lbl = QLabel(note)
            self.note_lbl.setObjectName("CardNote")
            row.addWidget(self.note_lbl)
            self.body.addLayout(row)


class Tile(QFrame):
    """Stat tile: caption, big tabular value and unit (live charger values)."""

    def __init__(self, caption: str, unit: str = "", big: bool = False) -> None:
        super().__init__()
        self.setObjectName("Tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        cap = QLabel(caption)
        cap.setObjectName("TileCaption")
        lay.addWidget(cap)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.value = QLabel("—")
        self.value.setObjectName("TileValueBig" if big else "TileValue")
        self.value.setFont(ui_font(36 if big else 24, QFont.Weight.Normal))
        row.addWidget(self.value)
        self.unit = QLabel(unit)
        self.unit.setObjectName("TileUnit")
        row.addWidget(self.unit, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)
        lay.addLayout(row)

    def set(self, text: str) -> None:
        self.value.setText(text)


class PillDelegate(QStyledItemDelegate):
    """Paints status cells as tinted pills: symbol + word + colour."""

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        key = index.data(STATUS_ROLE) or _key_from_text(str(index.data() or ""))
        if key is None:
            super().paint(painter, option, index)
            return
        # Row background / selection first (alternating shade), without the text.
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""  # type: ignore[attr-defined]
        widget = opt.widget  # type: ignore[attr-defined]
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        text = str(index.data() or "")
        color = status_color(key)
        f = ui_font(12, QFont.Weight.DemiBold)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(f)
        w = painter.fontMetrics().horizontalAdvance(text) + 22
        r = option.rect  # type: ignore[attr-defined]
        h = 22.0
        rect = QRectF(r.x() + 4, r.y() + (r.height() - h) / 2, min(w, r.width() - 8), h)
        bg = QColor(color)
        bg.setAlphaF(0.14)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, h / 2, h / 2)
        painter.setPen(color)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


def _key_from_text(text: str) -> str | None:
    t = text.replace("*", "").strip()
    for key, (sym, label, _) in STATUS.items():
        if label and t in (f"{sym} {label}", label, key):
            return key
    return None


def style_table(t: QTableWidget, row_h: int = 44, pill_columns: tuple[int, ...] = ()) -> None:
    """Mockup table style: uppercase muted headers, tall rows, alternating shade, no grid."""
    for c in range(t.columnCount()):
        it = t.horizontalHeaderItem(c)
        if it is not None:
            it.setText(it.text().upper())
    t.setShowGrid(False)
    t.setAlternatingRowColors(True)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(row_h)
    t.horizontalHeader().setHighlightSections(False)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setFrameShape(QFrame.Shape.NoFrame)
    for c in pill_columns:
        t.setItemDelegateForColumn(c, PillDelegate(t))
    if t.horizontalHeader().sectionResizeMode(0) == QHeaderView.ResizeMode.Interactive:
        t.horizontalHeader().setStretchLastSection(True)


def style_plot(pw: object) -> None:
    """Dark pyqtgraph plot matching the cards."""
    import pyqtgraph as pg

    assert isinstance(pw, pg.PlotWidget)
    pw.setBackground(BG_CARD)
    pi = pw.getPlotItem()
    for ax in ("left", "bottom", "right"):
        a = pi.getAxis(ax)
        a.setPen(pg.mkPen(BORDER_INPUT))
        a.setTextPen(pg.mkPen(MUTED))
        a.setStyle(tickFont=ui_font(11))
    pi.showGrid(x=True, y=True, alpha=0.08)
    pi.titleLabel.item.setFont(ui_font(13, QFont.Weight.DemiBold))
    pi.titleLabel.setText(pi.titleLabel.text, color=TEXT2)


def _build_stylesheet():
    return f"""
* {{ font-family: "{UI_FONT}"; }}
QWidget {{ color: {TEXT}; font-size: 14px; }}
QMainWindow, QDialog, QWidget#Root {{ background: {BG_WINDOW}; }}
QToolTip {{ background: {BG_INPUT}; color: {TEXT}; border: 1px solid {BORDER_INPUT}; padding: 6px; }}

#TitleBar {{ background: {BG_TITLE}; border-bottom: 1px solid {BORDER_CARD}; }}
#TitleBar QLabel {{ color: {TEXT}; font-weight: 600; }}
QLabel#AppIcon {{ background: {ACCENT}; color: {ON_ACCENT}; border-radius: 6px; font-weight: 700; }}
QLabel#TitleMeta {{ color: {TEXT2}; font-weight: 400; }}

QFrame#Card {{ background: {BG_CARD}; border: 1px solid {BORDER_CARD}; border-radius: 18px; }}
QFrame#Card QLabel {{ background: transparent; }}
QLabel#CardTitle {{ font-size: 16px; font-weight: 600; }}
QLabel#CardNote, QLabel#Caption {{ color: {TEXT2}; font-size: 13px; }}
QLabel#Muted {{ color: {MUTED}; }}
QFrame#Tile {{ background: {BG_INPUT}; border: none; border-radius: 14px; }}
QLabel#TileCaption {{ color: {TEXT2}; font-size: 13px; }}
QLabel#TileValue {{ font-size: 24px; }}
QLabel#TileValueBig {{ font-size: 36px; }}
QLabel#TileUnit {{ color: {TEXT2}; font-size: 15px; padding-bottom: 4px; }}

QFrame#Segmented {{ background: {BG_CARD}; border: 1px solid {BORDER_CARD}; border-radius: 16px; }}
QPushButton#SegTab {{ background: transparent; border: none; border-radius: 12px; color: {TEXT2};
    padding: 9px 22px; font-size: 15px; }}
QPushButton#SegTab:hover {{ color: {TEXT}; }}
QPushButton#SegTab:checked {{ background: {BG_INPUT}; color: {TEXT}; font-weight: 600; }}
QPushButton#SegTab:disabled {{ color: {BORDER_INPUT}; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit, QTextEdit {{
    background: {BG_INPUT}; border: 1px solid {BORDER_INPUT}; border-radius: 10px; padding: 7px 10px;
    color: {TEXT}; selection-background-color: {rgba(ACCENT, 0.45)}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {{
    border: 1px solid {ACCENT}; }}
QLineEdit:disabled {{ color: {MUTED}; }}
QLineEdit#SerialEdit {{ font-size: 22px; padding: 9px 14px; border-radius: 14px; }}
QPlainTextEdit {{ font-family: "{MONO_FONT}"; font-size: 13px; color: {DATA}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{ background: {BG_INPUT}; border: 1px solid {BORDER_INPUT};
    selection-background-color: {rgba(ACCENT, 0.25)}; outline: none; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 0; border: none; }}

QPushButton {{ background: {BG_CARD}; border: 1px solid {BORDER_INPUT}; border-radius: 12px;
    padding: 9px 18px; color: {TEXT}; font-weight: 500; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {MUTED}; border-color: {BORDER_CARD}; }}
QPushButton#Primary, QPushButton#StartButton {{ background: {ACCENT}; color: {ON_ACCENT}; border: none;
    font-weight: 600; }}
QPushButton#Primary:disabled, QPushButton#StartButton:disabled {{ background: {rgba(ACCENT, 0.22)};
    color: {rgba(TEXT, 0.45)}; }}
QPushButton#StartButton, QPushButton#AbortButton {{ font-size: 20px; min-height: 52px; min-width: 190px;
    border-radius: 14px; }}
QPushButton#AbortButton {{ background: {DANGER}; color: {ON_ACCENT}; border: none; font-weight: 600; }}
QPushButton#AbortButton:disabled {{ background: {rgba(DANGER, 0.18)}; color: {rgba(TEXT, 0.40)}; }}
QPushButton#Ghost {{ background: transparent; }}

QCheckBox, QRadioButton {{ spacing: 10px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 18px; height: 18px; border: 1px solid {BORDER_INPUT};
    background: {BG_INPUT}; }}
QCheckBox::indicator {{ border-radius: 5px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {ACCENT};
    border-color: {ACCENT}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {MUTED}; }}

QGroupBox {{ background: {BG_CARD}; border: 1px solid {BORDER_CARD}; border-radius: 18px;
    margin-top: 0px; padding: 46px 14px 14px 14px; font-size: 16px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: padding; subcontrol-position: top left; left: 20px; top: 16px;
    color: {TEXT}; }}
QGroupBox QLabel, QGroupBox QCheckBox, QGroupBox QRadioButton {{ font-size: 14px; font-weight: 400; }}

QTableWidget, QTableView, QListWidget {{ background: {BG_CARD}; alternate-background-color: {BG_ROW_ALT};
    border: none; color: {TEXT}; selection-background-color: {rgba(ACCENT, 0.16)}; selection-color: {TEXT};
    outline: none; }}
QTableWidget::item, QListWidget::item {{ padding: 2px 6px; border: none; }}
QListWidget::item {{ min-height: 24px; }}
QHeaderView {{ background: {BG_CARD}; }}
QHeaderView::section {{ background: {BG_CARD}; color: {MUTED}; font-size: 12px; font-weight: 500;
    padding: 6px 8px; border: none; border-bottom: 1px solid {BORDER_CARD}; }}
QTableCornerButton::section {{ background: {BG_CARD}; border: none; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_INPUT}; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_INPUT}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget#qt_scrollarea_viewport {{ background: {BG_WINDOW}; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {TEXT2}; padding: 8px 18px; border-radius: 10px; }}
QTabBar::tab:selected {{ background: {BG_INPUT}; color: {TEXT}; font-weight: 600; }}
QSplitter::handle {{ background: transparent; }}

#Mono, QLabel#Mono, QListWidget#Mono, QTableWidget#Mono {{ font-family: "{MONO_FONT}"; font-size: 12px;
    color: {DATA}; }}
QLabel#Banner {{ font-size: 44px; font-weight: 600; border-radius: 18px; padding: 10px; }}
QLabel#ManualBanner {{ {pill_css(WARNING, 0.16, 16)} border-radius: 14px; padding: 10px 16px; }}
QLabel#StatusText {{ font-size: 18px; font-weight: 600; color: {TEXT2}; }}
QMessageBox QLabel {{ color: {TEXT}; }}
"""


STYLESHEET = _build_stylesheet()
