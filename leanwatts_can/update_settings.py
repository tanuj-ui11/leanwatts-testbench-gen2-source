"""Simple GitHub sign-in and explicit software updates; no automatic downloads."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import time

from PySide6.QtCore import QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from . import theme, updater, github_auth


class UpdateSettingsPage(QWidget):
    install_started = Signal()

    def __init__(self, data_dir, *, busy_check=lambda: False,
                 current_version=updater.CURRENT_VERSION, can_configure=lambda: True):
        super().__init__()
        self.data_dir = Path(data_dir)
        self.busy_check = busy_check
        self.can_configure = can_configure
        self.current_version = current_version
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="software-updates")
        self._future = None
        self._action = ""
        self._release = None
        self._downloaded = None
        self._disposed = False
        self._auth_flow = None
        self._auth_cancel = Event()
        self._auth_next_poll = 0.0
        self._auth_deadline = 0.0
        self._connection = {"status": "signed_out", "login": "", "message": ""}
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        card = theme.Card("Software updates", "Installed version " + current_version)
        self.introduction = QLabel("Check for a new Leanwatts release, or choose a local update file.")
        self.introduction.setWordWrap(True)
        card.body.addWidget(self.introduction)
        account_row = QHBoxLayout()
        self.account_status = QLabel()
        self.account_status.setWordWrap(True)
        account_row.addWidget(self.account_status, 1)
        self.connect_button = QPushButton("Connect GitHub")
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self._disconnect)
        account_row.addWidget(self.connect_button)
        account_row.addWidget(self.disconnect_button)
        card.body.addLayout(account_row)

        self.auth_panel = QFrame()
        self.auth_panel.setObjectName("Tile")
        auth_layout = QHBoxLayout(self.auth_panel)
        auth_layout.setContentsMargins(18, 12, 18, 12)
        code_column = QVBoxLayout()
        label = QLabel("YOUR SIGN-IN CODE")
        label.setObjectName("Caption")
        code_column.addWidget(label)
        self.code = QLabel()
        self.code.setTextFormat(Qt.TextFormat.PlainText)
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code.setFont(theme.mono_font(30))
        self.code.setStyleSheet("font-size: 30px; font-weight: 600;")
        self.code.setMinimumWidth(210)
        code_column.addWidget(self.code)
        self.copy_button = QPushButton("Copy code")
        self.copy_button.clicked.connect(self._copy_code)
        code_column.addWidget(self.copy_button)
        auth_layout.addLayout(code_column)
        instructions = QVBoxLayout()
        description = QLabel("Open GitHub, sign in with the factory account, and enter this code. Return here when GitHub confirms the connection.")
        description.setWordWrap(True)
        instructions.addWidget(description)
        actions = QHBoxLayout()
        self.open_github_button = QPushButton("Open GitHub")
        self.open_github_button.setObjectName("Primary")
        self.open_github_button.clicked.connect(self._open_github)
        self.cancel_button = QPushButton("Cancel sign-in")
        self.cancel_button.clicked.connect(self._cancel_auth)
        actions.addWidget(self.open_github_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        instructions.addLayout(actions)
        self.auth_wait = QLabel("Waiting for GitHub…")
        self.auth_wait.setObjectName("Caption")
        instructions.addWidget(self.auth_wait)
        auth_layout.addLayout(instructions, 1)
        self.auth_panel.hide()
        card.body.addWidget(self.auth_panel)

        row = QHBoxLayout()
        self.check_button = QPushButton("Check for updates")
        self.check_button.clicked.connect(self._check)
        self.download_button = QPushButton("Download update")
        self.download_button.clicked.connect(self._download)
        self.local_button = QPushButton("Install local test update…")
        self.local_button.clicked.connect(self._choose_local)
        self.install_button = QPushButton("Install and restart")
        self.install_button.setObjectName("Primary")
        self.install_button.clicked.connect(self._install)
        for button in (self.check_button, self.download_button, self.local_button, self.install_button):
            row.addWidget(button)
        card.body.addLayout(row)
        self.status = QLabel("Choose Check for updates or Install local test update to begin.")
        self.status.setWordWrap(True)
        card.body.addWidget(self.status)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setPlaceholderText("Release notes appear here after checking.")
        self.notes.setMaximumHeight(105)
        card.body.addWidget(self.notes)
        body.addWidget(card)
        body.addStretch()
        self._refresh_connection()
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        self._controls()

    def _refresh_connection(self):
        try:
            self._connection = github_auth.connection_status(self.data_dir)
        except Exception:
            self._connection = {"status": "error", "login": "", "message": "GitHub connection could not be read. Connect again."}
        state = self._connection.get("status")
        login = self._connection.get("login", "")
        public = state == "public"
        self.connect_button.setVisible(not public)
        self.disconnect_button.setVisible(not public)
        self.introduction.setText("Check for a new Leanwatts release, or choose a local update file." if public else
                                  "Connect GitHub once to get Leanwatts updates, or choose a local update file.")
        if public:
            self.account_status.setText("Online updates use the Leanwatts release channel. No GitHub sign-in is required.")
            self._auth_cancel.set()
            self._auth_flow = None
            self.code.clear()
            self.auth_panel.hide()
        elif state == "connected":
            self.account_status.setText("Connected to GitHub" + (" as " + login if login else "") + ".")
        elif state == "not_configured":
            self.account_status.setText("Online updates are not available in this build yet. Local update files are ready to use.")
        elif state == "expired":
            self.account_status.setText("GitHub sign-in has expired. Reconnect to check for updates.")
        elif state == "error":
            self.account_status.setText("GitHub connection needs attention. Connect again to continue.")
        else:
            self.account_status.setText("GitHub is not connected on this laptop.")
        self.connect_button.setText("Reconnect GitHub" if state in ("expired", "error") else "Connect GitHub")

    def _controls(self):
        pending = self._future is not None
        busy = bool(self.busy_check())
        signing_in = not self._auth_cancel.is_set() and (self._auth_flow is not None or (pending and self._action == "auth_begin"))
        available = not pending and not busy and not signing_in
        connected = self._connection.get("status") in ("connected", "public")
        configured = github_auth.configured()
        self.connect_button.setEnabled(available and configured and not connected)
        self.disconnect_button.setEnabled(available and self._connection.get("status") in ("connected", "expired", "error"))
        self.local_button.setEnabled(available)
        self.check_button.setEnabled(available and connected and configured)
        self.download_button.setEnabled(available and bool(self._release and self._release["available"]
                                                          and self._release.get("source") != "local"))
        self.install_button.setEnabled(available and bool(self._downloaded))
        self.install_button.setToolTip("Close the operator session before installing." if busy else "Install the verified update and restart the application.")
        self.cancel_button.setEnabled(signing_in)
        self.copy_button.setEnabled(self._auth_flow is not None)
        self.open_github_button.setEnabled(self._auth_flow is not None)
        self.notes.setVisible(not signing_in)

    def _connect(self):
        if self.busy_check() or self._future is not None or self._auth_flow is not None or self._connection.get("status") == "public" or not github_auth.configured():
            return
        if not self.can_configure():
            return
        self._release = self._downloaded = None
        self._auth_cancel = Event()
        self._action = "auth_begin"
        self._future = self._executor.submit(github_auth.begin_sign_in)
        self.status.setText("Preparing GitHub sign-in…")
        self.code.setText("…")
        self.auth_wait.setText("Preparing your sign-in code…")
        self.auth_panel.show()
        self._controls()

    def _disconnect(self):
        if self.busy_check() or self._future is not None or self._auth_flow is not None or self._connection.get("status") not in ("connected", "expired", "error") or not self.can_configure():
            return
        try:
            github_auth.sign_out(self.data_dir)
            self._release = self._downloaded = None
            self.status.setText("GitHub disconnected from this laptop. Local updates remain available.")
        except Exception:
            self.status.setText("The GitHub connection could not be removed. Try again.")
        self._refresh_connection()
        self._controls()

    def _copy_code(self):
        if self._auth_flow is not None:
            QApplication.clipboard().setText(str(self._auth_flow["user_code"]))
            self.status.setText("Code copied. Paste it into the GitHub sign-in page.")

    def _open_github(self):
        if self._auth_flow is None:
            return
        # Open only GitHub's fixed device sign-in page, never a URL from an update manifest.
        uri = self._auth_flow.get("verification_uri")
        if uri not in ("https://github.com/login/device", "https://github.com/login/device/"):
            self.status.setText("The GitHub sign-in page could not be opened. Cancel and connect again.")
            return
        if not QDesktopServices.openUrl(QUrl(uri)):
            self.status.setText("Open github.com/login/device in your browser, then enter the code above.")

    def _cancel_auth(self):
        self._auth_cancel.set()
        self._auth_flow = None
        self.code.clear()
        self.auth_panel.hide()
        self.status.setText("GitHub sign-in cancelled. Local updates remain available.")
        self._refresh_connection()
        self._controls()

    def _check(self):
        if self.busy_check() or self._future is not None or self._auth_flow is not None or self._connection.get("status") not in ("connected", "public"):
            return
        self._release = self._downloaded = None
        self._action = "check"
        self._future = self._executor.submit(updater.check_configured, self.data_dir, self.current_version)
        self.status.setText("Checking for a new Leanwatts release…")
        self._controls()

    def _download(self):
        if self.busy_check() or self._future is not None or self._auth_flow is not None or not self._release or not self._release["available"]:
            return
        self._action = "download"
        self._future = self._executor.submit(updater.download_configured, self.data_dir, self._release)
        self.status.setText("Downloading and checking the update…")
        self._controls()

    def _install(self):
        if self.busy_check() or self._future is not None or self._auth_flow is not None or not self._release or not self._downloaded:
            return
        if not self.can_configure():
            return
        message = "Close this application and install version " + self._release["version"] + "? Saved recipes and session reports are retained."
        if self._release.get("source") == "local" and not self._release["available"]:
            message += "\n\nThis is a local test/reinstall of the current version."
        if QMessageBox.question(self, "Install update", message) != QMessageBox.StandardButton.Yes:
            return
        try:
            updater.launch_installer(self._downloaded, self._release)
            self.install_started.emit()
        except Exception as exc:
            self.status.setText("Could not start the installer: " + str(exc))

    def _choose_local(self):
        if self.busy_check() or self._future is not None or self._auth_flow is not None:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Choose a local test update installer", "", "Windows installer (*.exe)")
        if not filename:
            return
        manifest = Path(filename).with_suffix(".update.json")
        if not manifest.is_file():
            selected, _ = QFileDialog.getOpenFileName(self, "Choose this installer's matching .update.json manifest", str(Path(filename).parent), "Update manifest (*.json)")
            if not selected:
                return
            manifest = Path(selected)
        self._release = self._downloaded = None
        self._action = "local"
        self._future = self._executor.submit(updater.local_update, filename, manifest, self.current_version)
        self.status.setText("Checking the local update file…")
        self._controls()

    def _auth_result(self, action, result):
        if self._auth_cancel.is_set():
            self._refresh_connection()
            return
        if action == "auth_begin":
            self._auth_flow = result
            now = time.monotonic()
            self._auth_deadline = now + min(900.0, float(result.get("expires_in", 900)))
            self._auth_next_poll = now + max(1.0, float(result.get("interval", 5)))
            self.code.setText(str(result["user_code"]))
            self.auth_panel.show()
            self.status.setText("Finish sign-in on GitHub. This screen will continue automatically.")
            self._open_github()
            return
        state = result.get("status")
        if state in ("pending", "slow_down"):
            self._auth_next_poll = time.monotonic() + max(1.0, float(result.get("interval", self._auth_flow.get("interval", 5))))
            return
        self._auth_flow = None
        self.code.clear()
        self.auth_panel.hide()
        self._refresh_connection()
        if state == "complete":
            self.status.setText("GitHub connected. Click Check for updates.")
        elif state == "expired":
            self.status.setText("The sign-in code expired. Click Connect GitHub to get a new code.")
        elif state == "cancelled":
            self.status.setText("GitHub sign-in cancelled.")
        else:
            self.status.setText(result.get("message") or "GitHub sign-in could not finish. Connect again to retry.")

    def _poll(self):
        if self._disposed:
            return
        if self.busy_check() and not self._auth_cancel.is_set() and (self._auth_flow is not None or (self._future is not None and self._action in ("auth_begin", "auth_poll"))):
            self._cancel_auth()
        if self._future is not None and self._future.done():
            future, self._future = self._future, None
            action = self._action
            try:
                result = future.result()
                if action in ("auth_begin", "auth_poll"):
                    self._auth_result(action, result)
                elif action in ("check", "local"):
                    self._release = result
                    self.notes.setPlainText(result["notes"])
                    kind = "small update" if result.get("package_type") == "delta" else "full installer"
                    if action == "local":
                        self._downloaded = result["local_path"]
                        self.status.setText("Local " + kind + " " + result["version"] + " verified. Click Install and restart to apply it.")
                    else:
                        self.status.setText("Version " + result["version"] + " is available (" + kind + ")." if result["available"]
                                            else "This installation is up to date with the published release.")
                else:
                    self._downloaded = result
                    self.status.setText("Update verified. Click Install and restart when the operator session is closed.")
            except Exception as exc:
                if action in ("auth_begin", "auth_poll"):
                    self._auth_flow = None
                    self.code.clear()
                    self.auth_panel.hide()
                    if not self._auth_cancel.is_set():
                        self.status.setText("GitHub sign-in could not finish. Check the connection and try again.")
                else:
                    self.status.setText("Update could not finish: " + str(exc))
                self._refresh_connection()
        if self._auth_flow is not None and self._future is None:
            now = time.monotonic()
            if now >= self._auth_deadline:
                self._cancel_auth()
                self.status.setText("The sign-in code expired. Click Connect GitHub to get a new code.")
            else:
                remaining = max(0, int(self._auth_deadline - now))
                self.auth_wait.setText(f"Waiting for GitHub · Code expires in {remaining // 60}:{remaining % 60:02d}")
                if now >= self._auth_next_poll:
                    self._action = "auth_poll"
                    self._future = self._executor.submit(github_auth.poll_sign_in, self.data_dir, self._auth_flow, self._auth_cancel)
        self._controls()

    def dispose(self):
        self._disposed = True
        self._auth_cancel.set()
        self._auth_flow = None
        self._timer.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
