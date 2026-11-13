"""Password-operation UI checks with temporary sample accounts only."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest

from PySide6.QtWidgets import QApplication, QDialog
from leanwatts_can.account_settings import (AccountSettings, ChangeOwnPasswordDialog,
                                            LoginDialog, ResetOperatorPasswordDialog)
from leanwatts_can.session_store import OperatorStore


class PasswordUIChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.accounts = OperatorStore(self.folder.name)
        self.accounts.upsert_operator("ADMIN", "Sample admin", "admin-example", role="admin")
        self.accounts.upsert_operator("LEAD", "Sample lead", "lead-example", role="lead")
        self.accounts.upsert_operator("OP", "Sample operator", "operator-example")

    def tearDown(self):
        self.folder.cleanup()

    def test_lead_is_labelled_and_existing_lead_password_cannot_use_admin_edit(self):
        page = AccountSettings(self.folder.name)
        index = next(n for n, item in enumerate(page._records) if item["id"] == "LEAD")
        page.table.selectRow(index)
        self.assertEqual(page.table.item(index, 2).text(), "Production lead")
        self.assertEqual(page.role.currentData(), "lead")
        self.assertFalse(page.password.isEnabled())
        self.assertTrue(page.reset_button.isEnabled())
        self.assertTrue(page.change_password_button.isEnabled())
        page.close()

    def test_reset_dialog_limits_targets_and_resets_with_fresh_lead_credentials(self):
        dialog = ResetOperatorPasswordDialog(self.accounts, selected_id="OP")
        self.assertEqual([dialog.target.itemData(n) for n in range(dialog.target.count())], ["OP"])
        dialog.actor_id.setText("LEAD")
        dialog.actor_password.setText("lead-example")
        dialog.new_password.setText("operator-updated")
        dialog.confirm_password.setText("operator-updated")
        dialog._save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(self.accounts.authenticate("OP", "operator-updated")["role"], "operator")
        self.assertEqual(dialog.actor_password.text(), "")
        self.assertEqual(dialog.new_password.text(), "")

    def test_own_change_requires_current_password_and_keeps_role(self):
        dialog = ChangeOwnPasswordDialog(self.accounts, selected_id="LEAD")
        dialog.current_password.setText("wrong-current")
        dialog.new_password.setText("lead-updated")
        dialog.confirm_password.setText("lead-updated")
        dialog._save()
        self.assertIsNone(dialog.changed_operator)
        self.assertTrue(dialog.error.text())
        self.assertEqual(self.accounts.authenticate("LEAD", "lead-example")["role"], "lead")
        dialog.current_password.setText("lead-example")
        dialog.new_password.setText("lead-updated")
        dialog.confirm_password.setText("lead-updated")
        dialog._save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(self.accounts.authenticate("LEAD", "lead-updated")["role"], "lead")
        self.assertEqual(dialog.current_password.text(), "")

    def test_cancel_clears_password_inputs(self):
        login = LoginDialog(self.accounts)
        login.password.setText("not-saved")
        login.reject()
        self.assertEqual(login.password.text(), "")
        change = ChangeOwnPasswordDialog(self.accounts)
        for field in (change.current_password, change.new_password, change.confirm_password):
            field.setText("not-saved")
        change.reject()
        self.assertTrue(all(not field.text() for field in (change.current_password, change.new_password, change.confirm_password)))


if __name__ == "__main__":
    unittest.main()
