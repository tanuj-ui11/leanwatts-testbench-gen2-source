"""Settings > Dashboard: address, station, API key (admin only). The key is never shown back or logged."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from . import theme
from .dashboard_client import DashboardClient

CLOCK_WARNING_SECONDS = 300


class DashboardSettingsPage(QWidget):
    def __init__(self, settings, *, can_configure=lambda: True, on_saved=lambda: None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.can_configure = can_configure
        self.on_saved = on_saved
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dashboard-test")
        self._future = None
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        card = theme.Card("Dashboard", "Sends per-serial EoL reports to the LeanWatts operations dashboard")
        values = settings.settings()
        form = QVBoxLayout()
        self.address = QLineEdit(values["base_url"])
        self.address.setPlaceholderText("https://ops.leanwatts.com")
        self.station = QLineEdit(values["station_name"])
        self.station.setPlaceholderText("LW-EOL-01")
        self.station.setMaxLength(64)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Paste a new key to replace the saved one")
        for label, field in (("Dashboard address (HTTPS)", self.address), ("Station name", self.station), ("API key", self.api_key)):
            caption = QLabel(label)
            caption.setObjectName("Caption")
            form.addWidget(caption)
            form.addWidget(field)
        card.body.addLayout(form)
        self.storage = QLabel()
        self.storage.setWordWrap(True)
        card.body.addWidget(self.storage)
        row = QHBoxLayout()
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("Primary")
        self.save_button.clicked.connect(self._save)
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self._test)
        self.forget_button = QPushButton("Remove saved key")
        self.forget_button.clicked.connect(self._forget)
        for button in (self.save_button, self.test_button, self.forget_button):
            row.addWidget(button)
        row.addStretch()
        card.body.addLayout(row)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        card.body.addWidget(self.status)
        body.addWidget(card)
        body.addStretch()
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        self._refresh_storage()

    def _refresh_storage(self):
        storage = self.settings.key_storage()
        text = {
            "protected": "API key saved with Windows account protection.",
            "memory": "Windows account protection is unavailable. The API key is kept in memory only and must be entered again after restarting the application.",
            "none": "No API key saved.",
        }[storage]
        self.storage.setText(text)

    def _allowed(self):
        if self.can_configure():
            return True
        self.status.setText("Administrator approval is needed to change dashboard settings.")
        return False

    def _save(self):
        if not self._allowed():
            return
        try:
            self.settings.save(base_url=self.address.text(), station_name=self.station.text())
            typed = self.api_key.text()
            if typed.strip():
                mode = self.settings.save_api_key(typed)
                self.api_key.clear()
                self.status.setText("Saved." if mode == "protected" else
                                    "Saved. The key is kept in memory only; Windows protection is unavailable.")
            else:
                self.status.setText("Saved.")
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self._refresh_storage()
        self.on_saved()

    def _forget(self):
        if not self._allowed():
            return
        self.settings.forget_key()
        self._refresh_storage()
        self.status.setText("Saved API key removed.")

    def _test(self):
        if self._future is not None or not self._allowed():
            return
        try:
            client = self.settings.client()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.status.setText("Testing the connection…")
        self.test_button.setEnabled(False)
        self._future = self._executor.submit(self._probe, client)

    @staticmethod
    def _probe(client: DashboardClient):
        ping = client.ping()
        skew = DashboardClient.clock_skew_seconds(ping, datetime.now().astimezone())
        return ping, skew

    def _poll(self):
        if self._future is None or not self._future.done():
            return
        future, self._future = self._future, None
        self.test_button.setEnabled(True)
        try:
            ping, skew = future.result()
        except Exception as exc:
            self.status.setText(getattr(exc, "message", None) or str(exc) or "The connection test failed.")
            return
        message = f"Connected as {ping.get('user', 'unknown user')} ({ping.get('role', 'unknown role')})."
        if abs(skew) > CLOCK_WARNING_SECONDS:
            message += f" This laptop's clock differs from the dashboard by {skew:+.0f} s. Check the Windows time setting."
        else:
            message += f" Clock difference {skew:+.1f} s."
        self.status.setText(message)

    def dispose(self):
        self._timer.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
