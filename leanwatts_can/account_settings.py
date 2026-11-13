"""Local operator administration. Passwords are handled by OperatorStore only."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from . import theme
from .session_store import OperatorStore

ROLE_LABELS = {"operator": "Operator", "lead": "Production lead", "admin": "Administrator"}


class LoginDialog(QDialog):
    def __init__(self, accounts, parent=None, *, title="Start session", admin_only=False):
        super().__init__(parent)
        self.accounts = accounts
        self.admin_only = admin_only
        self.operator = None
        self.setWindowTitle(title)
        self.setMinimumWidth(380)
        body = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 23px; font-weight: 600;")
        body.addWidget(heading)
        form = QFormLayout()
        self.operator_id = QLineEdit()
        self.operator_id.setPlaceholderText("Operator ID number")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("ID number", self.operator_id)
        form.addRow("Password", self.password)
        body.addLayout(form)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {theme.DANGER};")
        body.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Log in")
        buttons.accepted.connect(self._login)
        buttons.rejected.connect(self.reject)
        body.addWidget(buttons)
        self.password.returnPressed.connect(self._login)

    def _login(self):
        try:
            operator = self.accounts.authenticate(self.operator_id.text().strip(), self.password.text())
            if self.admin_only and operator.get("role") != "admin":
                raise ValueError("An administrator account is required.")
            self.operator = operator
            self.password.clear()
            self.accept()
        except Exception as exc:
            self.error.setText(str(exc))
            self.password.clear()
            self.password.setFocus()

    def done(self, result):
        self.password.clear()
        super().done(result)


def password_field():
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    return field


class ResetOperatorPasswordDialog(QDialog):
    """One reset, authenticated by the store when the button is pressed."""
    def __init__(self, accounts, parent=None, *, selected_id=None):
        super().__init__(parent)
        self.accounts = accounts
        self.changed_operator = None
        self.setWindowTitle("Reset operator password")
        self.setMinimumWidth(480)
        body = QVBoxLayout(self)
        note = QLabel("Production leads and administrators can reset an operator's password.")
        note.setWordWrap(True)
        body.addWidget(note)
        form = QFormLayout()
        self.actor_id = QLineEdit()
        self.actor_password = password_field()
        self.target = QComboBox()
        for account in accounts.list_operators():
            if account.get("role") == "operator":
                self.target.addItem(account["id"] + " · " + account.get("name", ""), account["id"])
        index = self.target.findData(selected_id)
        if index >= 0:
            self.target.setCurrentIndex(index)
        self.new_password = password_field()
        self.confirm_password = password_field()
        form.addRow("Your lead / admin ID", self.actor_id)
        form.addRow("Your password", self.actor_password)
        form.addRow("Operator to reset", self.target)
        form.addRow("New operator password", self.new_password)
        form.addRow("Confirm new password", self.confirm_password)
        body.addLayout(form)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {theme.DANGER};")
        body.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Reset password")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.target.count() > 0)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        body.addWidget(buttons)

    def _clear_passwords(self):
        self.actor_password.clear()
        self.new_password.clear()
        self.confirm_password.clear()

    def _save(self):
        try:
            if self.new_password.text() != self.confirm_password.text():
                raise ValueError("The new passwords do not match.")
            self.changed_operator = self.accounts.reset_operator_password(
                self.actor_id.text().strip(), self.actor_password.text(),
                self.target.currentData(), self.new_password.text())
            self.accept()
        except Exception as exc:
            self.error.setText(str(exc))
            self._clear_passwords()
            self.actor_password.setFocus()

    def done(self, result):
        self._clear_passwords()
        super().done(result)


class ChangeOwnPasswordDialog(QDialog):
    def __init__(self, accounts, parent=None, *, selected_id=None):
        super().__init__(parent)
        self.accounts = accounts
        self.changed_operator = None
        self.setWindowTitle("Change my password")
        self.setMinimumWidth(440)
        body = QVBoxLayout(self)
        note = QLabel("Enter your current password to choose a new password for your own account.")
        note.setWordWrap(True)
        body.addWidget(note)
        form = QFormLayout()
        self.operator_id = QLineEdit(selected_id or "")
        self.current_password = password_field()
        self.new_password = password_field()
        self.confirm_password = password_field()
        form.addRow("Your ID number", self.operator_id)
        form.addRow("Current password", self.current_password)
        form.addRow("New password", self.new_password)
        form.addRow("Confirm new password", self.confirm_password)
        body.addLayout(form)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {theme.DANGER};")
        body.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Change password")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        body.addWidget(buttons)

    def _clear_passwords(self):
        self.current_password.clear()
        self.new_password.clear()
        self.confirm_password.clear()

    def _save(self):
        try:
            if self.new_password.text() != self.confirm_password.text():
                raise ValueError("The new passwords do not match.")
            self.changed_operator = self.accounts.change_own_password(
                self.operator_id.text().strip(), self.current_password.text(), self.new_password.text())
            self.accept()
        except Exception as exc:
            self.error.setText(str(exc))
            self._clear_passwords()
            self.current_password.setFocus()

    def done(self, result):
        self._clear_passwords()
        super().done(result)


class AccountSettings(QWidget):
    accounts_changed = Signal()

    def __init__(self, data_dir, parent=None):
        super().__init__(parent)
        self.accounts = OperatorStore(data_dir)
        self._selected_id = None
        self._records = []
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        card = theme.Card("Operator accounts", "Local to this station")
        self.description = QLabel("Create the first administrator account, then add the production team's operator IDs.")
        self.description.setWordWrap(True)
        card.body.addWidget(self.description)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["ID number", "Name", "Role", "Access"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._select)
        theme.style_table(self.table, row_h=34)
        card.body.addWidget(self.table, 1)
        form = QFormLayout()
        self.operator_id = QLineEdit()
        self.name = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("New password; leave blank to keep an existing password")
        self.role = QComboBox()
        self.role.addItem("Operator", "operator")
        self.role.addItem("Production lead", "lead")
        self.role.addItem("Administrator", "admin")
        self.role.currentIndexChanged.connect(self._password_controls)
        self.enabled = QCheckBox("Account enabled")
        self.enabled.setChecked(True)
        form.addRow("ID number", self.operator_id)
        form.addRow("Name", self.name)
        form.addRow("Password", self.password)
        form.addRow("Role", self.role)
        form.addRow("", self.enabled)
        card.body.addLayout(form)
        actions = QHBoxLayout()
        new = QPushButton("New account")
        new.clicked.connect(self._new)
        self.save_button = QPushButton("Save account")
        self.save_button.setObjectName("Primary")
        self.save_button.clicked.connect(self._save)
        actions.addWidget(new)
        actions.addWidget(self.save_button)
        actions.addStretch()
        self.reset_button = QPushButton("Reset operator password")
        self.reset_button.clicked.connect(self._reset_password)
        self.change_password_button = QPushButton("Change my password")
        self.change_password_button.clicked.connect(self._change_password)
        actions.addWidget(self.reset_button)
        actions.addWidget(self.change_password_button)
        card.body.addLayout(actions)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        card.body.addWidget(self.status)
        body.addWidget(card, 1)
        self.refresh()

    def refresh(self):
        try:
            self._records = self.accounts.list_operators()
            self.table.blockSignals(True)
            self.table.setRowCount(len(self._records))
            for row, operator in enumerate(self._records):
                values = (operator["id"], operator.get("name", ""), ROLE_LABELS.get(operator.get("role"), "Operator"),
                          "Enabled" if operator.get("enabled") else "Disabled")
                for col, value in enumerate(values):
                    self.table.setItem(row, col, QTableWidgetItem(str(value)))
            self.table.blockSignals(False)
            first = not self._records
            self.role.setEnabled(not first)
            if first:
                self.role.setCurrentIndex(self.role.findData("admin"))
            self.description.setText("Create the first administrator account to begin. No default password is supplied."
                                     if first else "Administrators manage accounts. Production leads can reset operator passwords. Everyone changes their own password using their current password.")
            self.reset_button.setEnabled(any(account.get("role") == "operator" for account in self._records))
            self.change_password_button.setEnabled(not first)
            self._password_controls()
        except Exception as exc:
            self.table.blockSignals(False)
            self.status.setText(str(exc))

    def _select(self):
        row = self.table.currentRow()
        if not 0 <= row < len(self._records):
            return
        operator = self._records[row]
        self._selected_id = operator["id"]
        self.operator_id.setText(operator["id"])
        self.operator_id.setReadOnly(True)
        self.name.setText(operator.get("name", ""))
        self.password.clear()
        self.role.setCurrentIndex(self.role.findData(operator.get("role", "operator")))
        self.enabled.setChecked(bool(operator.get("enabled")))
        self._password_controls()

    def _password_controls(self):
        selected = next((account for account in self._records if account["id"] == self._selected_id), None)
        is_existing_lead = selected is not None and selected.get("role") == "lead"
        self.password.setEnabled(not is_existing_lead)
        if is_existing_lead:
            self.password.clear()
            self.password.setPlaceholderText("Production leads use Change my password")
            self.password.setToolTip("The lead must enter their current password to change it.")
        else:
            self.password.setPlaceholderText("New password; leave blank to keep an existing password")
            self.password.setToolTip("")

    def _new(self):
        self.table.clearSelection()
        self._selected_id = None
        self.operator_id.setReadOnly(False)
        self.operator_id.clear()
        self.name.clear()
        self.password.clear()
        self.role.setCurrentIndex(self.role.findData("admin" if not self._records else "operator"))
        self.enabled.setChecked(True)
        self._password_controls()
        self.status.clear()
        self.operator_id.setFocus()

    def _reset_password(self):
        dialog = ResetOperatorPasswordDialog(self.accounts, self, selected_id=self._selected_id)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.password.clear()
            self.refresh()
            self.status.setText("Password reset for operator " + dialog.changed_operator["id"] + ".")
            self.accounts_changed.emit()

    def _change_password(self):
        dialog = ChangeOwnPasswordDialog(self.accounts, self, selected_id=self._selected_id)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.password.clear()
            self.refresh()
            self.status.setText("Password changed for " + dialog.changed_operator["id"] + ".")
            self.accounts_changed.emit()

    def _save(self):
        try:
            # Authentication is per mutation; leaving this page open does not
            # leave an unlocked administrator session behind.
            if self.accounts.list_operators():
                login = LoginDialog(self.accounts, self, title="Administrator approval", admin_only=True)
                if login.exec() != QDialog.DialogCode.Accepted:
                    return
            operator_id = self.operator_id.text().strip()
            password = self.password.text()
            self.accounts.upsert_operator(operator_id, self.name.text().strip(), password,
                                          enabled=self.enabled.isChecked(), role=self.role.currentData())
            self.password.clear()
            self.refresh()
            self.status.setText("Account saved. The operator can now start a session." if self.enabled.isChecked()
                                else "Account disabled. Previous session reports remain available.")
            self.accounts_changed.emit()
        except Exception as exc:
            self.password.clear()
            self.status.setText(str(exc))
