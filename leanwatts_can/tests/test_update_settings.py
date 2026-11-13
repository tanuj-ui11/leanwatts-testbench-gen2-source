"""Offline UI transitions; no GitHub requests, browser launches or installation."""
from concurrent.futures import Future
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from leanwatts_can.update_settings import UpdateSettingsPage


class UpdateSettingsChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.configured = patch("leanwatts_can.github_auth.configured", return_value=True)
        self.connection = patch("leanwatts_can.github_auth.connection_status", return_value={"status": "signed_out", "login": "", "message": ""})
        self.browser = patch("leanwatts_can.update_settings.QDesktopServices.openUrl", return_value=True)
        self.network = patch("leanwatts_can.github_auth._request_json", side_effect=AssertionError("Network forbidden"))
        self.configured.start(); self.connection.start(); self.browser_mock = self.browser.start(); self.network.start()
        self.busy = False
        self.page = UpdateSettingsPage(self.folder.name, busy_check=lambda: self.busy, can_configure=lambda: True)
        self.page._timer.stop()

    def tearDown(self):
        self.page.dispose()
        self.page.close()
        self.network.stop(); self.browser.stop(); self.connection.stop(); self.configured.stop()
        self.folder.cleanup()

    def start_result(self):
        future = Future()
        future.set_result(dict(user_code="TEST-CODE", device_code="fake-internal-code",
                               verification_uri="https://github.com/login/device", expires_in=900, interval=5))
        self.page._future, self.page._action = future, "auth_begin"
        self.page._poll()

    def test_signed_out_still_supports_local_updates_without_manual_fields(self):
        self.assertTrue(self.page.local_button.isEnabled())
        self.assertTrue(self.page.connect_button.isEnabled())
        self.assertFalse(self.page.check_button.isEnabled())
        for name in ("owner", "repository", "token", "feed"):
            self.assertFalse(hasattr(self.page, name))

    def test_public_release_channel_checks_without_login_or_account_controls(self):
        with patch("leanwatts_can.github_auth.connection_status", return_value={"status": "public", "login": "", "message": ""}):
            self.page._refresh_connection()
            self.page._controls()
        self.assertTrue(self.page.connect_button.isHidden())
        self.assertTrue(self.page.disconnect_button.isHidden())
        self.assertTrue(self.page.auth_panel.isHidden())
        self.assertTrue(self.page.check_button.isEnabled())
        self.assertTrue(self.page.local_button.isEnabled())
        self.assertIn("No GitHub sign-in", self.page.account_status.text())

    def test_code_flow_opens_only_github_and_cancel_clears_code(self):
        self.start_result()
        self.assertEqual(self.page.code.text(), "TEST-CODE")
        self.assertNotIn("fake-internal-code", self.page.status.text())
        self.assertEqual(self.browser_mock.call_args.args[0].toString(), "https://github.com/login/device")
        self.assertTrue(self.page.copy_button.isEnabled())
        self.page._cancel_auth()
        self.assertTrue(self.page._auth_cancel.is_set())
        self.assertIsNone(self.page._auth_flow)
        self.assertEqual(self.page.code.text(), "")

    def test_late_begin_after_cancel_cannot_reopen_browser_or_flow(self):
        self.page._cancel_auth()
        self.start_result()
        self.browser_mock.assert_not_called()
        self.assertIsNone(self.page._auth_flow)
        self.assertEqual(self.page.code.text(), "")

    def test_active_session_disables_update_actions_and_cancels_auth(self):
        self.start_result()
        self.busy = True
        self.page._poll()
        self.assertTrue(self.page._auth_cancel.is_set())
        for button in (self.page.connect_button, self.page.local_button, self.page.check_button,
                       self.page.download_button, self.page.install_button):
            self.assertFalse(button.isEnabled())

    def test_dispose_cancels_in_flight_authorisation(self):
        self.start_result()
        self.page.dispose()
        self.assertTrue(self.page._auth_cancel.is_set())
        self.assertIsNone(self.page._auth_flow)


if __name__ == "__main__":
    unittest.main()
