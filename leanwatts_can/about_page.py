"""Product and station information for the operator-facing application."""
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout, QWidget)

from . import theme, branding
from .version import VERSION


class AboutPage(QWidget):
    updates_requested = Signal()

    def __init__(self, data_dir, *, version=VERSION, parent=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir).resolve()
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        product = theme.Card()
        heading = QHBoxLayout()
        self.logo_label = branding.logo_label(height=44)
        heading.addWidget(self.logo_label)
        heading.addStretch()
        heading.addWidget(theme.pill("VERSION " + version, theme.ACCENT))
        product.body.addLayout(heading)
        title = QLabel("Testbench Gen2")
        title.setStyleSheet("font-size: 24px; font-weight: 600;")
        product.body.addWidget(title)
        subtitle = QLabel("Charger calibration and production verification")
        subtitle.setObjectName("Caption")
        product.body.addWidget(subtitle)
        description = QLabel("One operator session brings together charger parameter verification, current and voltage calibration, and post-calibration tests. Each charger is recorded in the session report.")
        description.setWordWrap(True)
        product.body.addWidget(description)
        body.addWidget(product)

        details = QHBoxLayout()
        release = theme.Card("What's new in " + version)
        for text in ("Four session choices: Parameters + Calibration only, Test only, Both independently, or Both mandatorily.",
                     "Arm Test, then restart charger AC. Tests do not change charger parameters or send charger commands.",
                     "Tests run in order: settled open-circuit voltage, stable peak current at V-set 49 V, then cutoff from I-set 5 A down to 0.5 A in 0.1 A steps.",
                     "The cutoff ramp begins immediately after switching to I-set. The load stays ON between the loaded stages; shutdown is checked for relay restarts.",
                     "Reports include failure reasons, measured values and required ranges for each failed test.",
                     "Test and Firmware Programming have their own variant selection and operator sign-in. No Auto Calibration session is required.",
                     "Only one operation runs at a time. Firmware programming retains chip checks, backup and full flash verification.",
                     "Existing recipes, accounts and reports are retained."):
            label = QLabel(text)
            label.setWordWrap(True)
            release.body.addWidget(label)
        release.body.addStretch()
        update = QPushButton("Software updates")
        update.setObjectName("Primary")
        update.clicked.connect(self.updates_requested.emit)
        release.body.addWidget(update)
        details.addWidget(release, 1)
        station = theme.Card("Station records", "Stored on this laptop")
        label = QLabel("Recipes, operator accounts, connection settings and session reports stay with this station when the application is updated.")
        label.setWordWrap(True)
        station.body.addWidget(label)
        self.data_path = QLineEdit(str(self.data_dir))
        self.data_path.setReadOnly(True)
        self.data_path.setToolTip(str(self.data_dir))
        station.body.addWidget(self.data_path)
        station.body.addStretch()
        open_folder = QPushButton("Open station folder")
        open_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir))))
        station.body.addWidget(open_folder)
        details.addWidget(station, 1)
        body.addLayout(details)
        note = QLabel("Leanwatts India Private Limited · Production systems")
        note.setObjectName("Caption")
        body.addWidget(note)
        body.addStretch()
