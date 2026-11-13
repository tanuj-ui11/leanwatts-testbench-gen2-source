"""Independent operator and saved-variant selection for bench workspaces."""
from __future__ import annotations

import copy
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QWidget
from .account_settings import LoginDialog
from .core import Recipe


class WorkspaceIdentity(QWidget):
    changed = Signal()
    sign_in_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records = []
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        variant_label = QLabel("Variant")
        variant_label.setObjectName("Caption")
        row.addWidget(variant_label)
        self.variant = QComboBox()
        self.variant.setMinimumContentsLength(17)
        self.variant.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.variant.setToolTip("Saved variant for this tab. Other tabs keep their own selection.")
        self.variant.currentIndexChanged.connect(lambda *_: self.changed.emit())
        row.addWidget(self.variant, 1)
        self.operator = QLabel("No operator")
        self.operator.setObjectName("Caption")
        row.addWidget(self.operator)
        self.login = QPushButton("Sign in")
        self.login.clicked.connect(self.sign_in_requested.emit)
        row.addWidget(self.login)
        self.set_profiles([])

    def set_profiles(self, records, selected=0):
        old = self.selected_record
        name = old["recipe"]["name"] if old else None
        self.records = copy.deepcopy(records)
        self.variant.blockSignals(True)
        self.variant.clear()
        chosen = 0
        for index, record in enumerate(self.records):
            title = record["recipe"]["name"]
            self.variant.addItem(title + (" · Draft" if record.get("production_ready") is False else ""))
            if title == name:
                chosen = index
        if not self.records:
            self.variant.addItem("Save a variant in Variant Recipes")
        self.variant.setCurrentIndex(chosen)
        self.variant.blockSignals(False)
        self.changed.emit()

    @property
    def selected_record(self):
        index = self.variant.currentIndex()
        return copy.deepcopy(self.records[index]) if 0 <= index < len(self.records) else None

    def set_operator(self, operator):
        self.operator.setText(str((operator or {}).get("name") or (operator or {}).get("id") or "No operator"))
        self.operator.setToolTip("Operator ID: " + str(operator.get("id", "")) if operator else "Sign in here; no Auto Calibration session is needed.")
        self.login.setText("Change operator" if operator else "Sign in")

    def set_locked(self, locked):
        self.variant.setEnabled(not locked)
        self.login.setEnabled(not locked)


def resolve_operator(window, preferred=None):
    candidates = [preferred] if preferred else [getattr(window.operator_page, "_authenticated_operator", None)]
    if not preferred:
        candidates += [getattr(getattr(window, name, None), "_operator", None)
                       for name in ("test_controller", "firmware_controller")]
    operator = next((item for item in candidates if item), None)
    if not operator:
        raise ValueError("Sign in with your operator ID on this tab.")
    current = next((item for item in window.accounts_page.accounts.list_operators()
                    if item.get("id") == operator.get("id") and item.get("enabled")), None)
    if not current:
        raise ValueError("This operator account is disabled. Sign in with an enabled account.")
    return copy.deepcopy(current)


def workspace_context(window, page, preferred=None):
    operator = resolve_operator(window, preferred)
    record = page.identity.selected_record
    if record is None:
        raise ValueError("Choose a saved variant here. Create one in Variant Recipes if the list is empty.")
    return Recipe.from_dict(record["recipe"]), record, operator


def sign_in(window, page):
    dialog = LoginDialog(window.accounts_page.accounts, page, title="Operator sign in")
    return copy.deepcopy(dialog.operator) if dialog.exec() == QDialog.DialogCode.Accepted else None
