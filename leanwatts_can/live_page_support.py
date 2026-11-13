"""Explicit live CAN modes for the original-layout page; no automatic startup."""
from __future__ import annotations

import copy
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout)

from .protocol import CONFIG, command, decode, describe_charger_fault, encode_all_config, encode_duty, poll


class LivePageSupport:
    def _init_live(self, factory):
        self._monitor_factory = factory
        self._live_client = None
        self._live_executor = None
        self._live_future = None
        self._live_operation = ""
        self._live_pending_operator = None
        self._live_disposed = False
        self._live_closing = False
        self._live_next_receive = 0.0
        self._live_deadline = 0.0
        self._live_readback_issue = ""
        self._live_next_poll = 0.0
        self._live_received = 0
        self._live_last_rx = None
        self._live_observed = {}
        self._live_observed_at = {}
        self._live_telemetry = {}
        self._live_telemetry_at = {}
        self._live_verified = False
        self._live_verified_values = None
        self._live_written_values = None
        self._live_shutdown_requested = False
        self._live_shutdown_pending = False
        self._live_cancel_send = threading.Event()
        self._live_applied_duties = None
        self._live_pending_duties = None
        self._live_pending_start = None
        self._mode_previous_index = 0
        self.bench_limits = None
        self._bench_limits_dialog = None
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(100)
        self._live_timer.timeout.connect(self._live_tick)

    @property
    def live_mode(self):
        if not hasattr(self, "mode_select"):
            return "simulation"
        return ("simulation", "receive", "manual")[self.mode_select.currentIndex()]

    def _mode_bar(self):
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 0, 4, 0)
        self.mode_select = QComboBox()
        self.mode_select.addItems(["Simulation", "Live CAN · receive only", "Live manual control"])
        self.mode_select.setMaximumWidth(250)
        self.mode_select.setToolTip("Live modes use the real adapter. Close the Chinese app before opening the same adapter here.")
        self.mode_select.currentIndexChanged.connect(self._change_live_mode)
        self.close_adapter_button = QPushButton("Close adapter")
        self.close_adapter_button.setMaximumWidth(140)
        self.close_adapter_button.clicked.connect(self.close_live)
        self.close_adapter_button.setToolTip("Close CAN only. This does not shut down the charger.")
        self.apply_duties_button = QPushButton("Apply duties")
        self.apply_duties_button.setMaximumWidth(130)
        self.apply_duties_button.setToolTip("Send both explicitly entered duty values. Confirm bench readings before saving calibration.")
        self.apply_duties_button.clicked.connect(lambda: self._guarded(lambda: self._live_duty("voltage", 0)))
        self.bench_limits_button = QPushButton("Bench limits")
        self.bench_limits_button.setMaximumWidth(130)
        self.bench_limits_button.clicked.connect(lambda: self._guarded(self._show_bench_limits_dialog))
        bar.addWidget(self.mode_select)
        bar.addStretch()
        bar.addWidget(self.bench_limits_button)
        bar.addWidget(self.apply_duties_button)
        bar.addWidget(self.close_adapter_button)
        return bar

    def _bench_setup_reason(self):
        if self.live_mode != "manual":
            return "Select Live manual control to enter bench limits."
        if self._live_controls_busy():
            return "Wait for the queued or active operator action, or adapter close, before changing bench limits."
        if self.pending_settings is not None:
            return "Wait for the settings readback before changing bench limits."
        return ""

    def _refresh_bench_dialog(self):
        dialog = self._bench_limits_dialog
        if dialog is not None:
            reason = self._bench_setup_reason()
            dialog.apply_button.setEnabled(not reason)
            dialog.apply_button.setToolTip(reason or "Apply these limits locally. No CAN command is sent.")
            if reason:
                dialog.validation_message.setText(reason)
            elif getattr(dialog, "last_busy_reason", "") == dialog.validation_message.text():
                dialog.validation_message.setText("Limits apply locally. No CAN command will be sent.")
            dialog.last_busy_reason = reason

    def _show_bench_limits_dialog(self):
        reason = self._bench_setup_reason()
        if reason:
            raise ValueError(reason)
        if self._bench_limits_dialog is not None:
            self._bench_limits_dialog.raise_()
            self._bench_limits_dialog.activateWindow()
            return self._bench_limits_dialog

        dialog = QDialog(self)
        dialog.setWindowTitle("Bench limits")
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.setMinimumWidth(420)
        dialog.setMaximumWidth(460)
        layout = QVBoxLayout(dialog)
        help_text = QLabel("Enter the approved maximum voltage, current and power for this test setup. Charger settings stay as entered; no saved variant is required.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        form = QFormLayout()
        dialog.limit_fields = {}
        for key, label in (("max_voltage", "Maximum voltage (V)"),
                           ("max_current", "Maximum current (A)"),
                           ("max_power", "Maximum power (W)")):
            edit = QLineEdit()
            edit.setObjectName("bench_" + key)
            edit.setPlaceholderText("Enter approved limit")
            if self.bench_limits is not None:
                edit.setText(f"{self.bench_limits[key]:g}")
            dialog.limit_fields[key] = edit
            form.addRow(label, edit)
        layout.addLayout(form)
        dialog.validation_message = QLabel("Limits apply locally. No CAN command will be sent.")
        dialog.validation_message.setWordWrap(True)
        dialog.validation_message.setMinimumHeight(40)
        layout.addWidget(dialog.validation_message)
        buttons = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        dialog.apply_button = buttons.button(QDialogButtonBox.Apply)
        dialog.apply_button.setText("Apply limits")

        def apply_limits():
            try:
                reason = self._bench_setup_reason()
                if reason:
                    raise ValueError(reason)
                try:
                    values = [float(dialog.limit_fields[key].text())
                              for key in ("max_voltage", "max_current", "max_power")]
                except ValueError as exc:
                    raise ValueError("Enter a numeric value for all three bench limits.") from exc
                self.set_bench_limits(*values)
            except ValueError as exc:
                dialog.validation_message.setText(str(exc))
                return
            dialog.accept()

        dialog.apply_button.clicked.connect(apply_limits)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self._bench_limits_dialog = dialog
        dialog.finished.connect(lambda: setattr(self, "_bench_limits_dialog", None))
        self._refresh_bench_dialog()
        dialog.open()
        return dialog

    def _clear_live_values(self):
        self._live_written_values = None
        self._live_readback_issue = ""
        self._live_telemetry.clear()
        self._live_telemetry_at.clear()
        for edit in list(self.readbacks.values()) + list(self.telemetry.values()):
            edit.setText("—")
            edit.setToolTip("No current received value. This is not a measurement.")
        for edit in (self.version, self.bms_alarm, self.work_voltage, self.work_current):
            edit.setText("—")
        self.fault.setText("No received fault/status data")

    def _change_live_mode(self):
        if self._live_client is not None or self._live_future is not None or self._live_closing:
            self.mode_select.blockSignals(True)
            self.mode_select.setCurrentIndex(self._mode_previous_index)
            self.mode_select.blockSignals(False)
            self._event("Close the real adapter before changing modes.")
            return
        self._mode_previous_index = self.mode_select.currentIndex()
        self.readback_timer.stop()
        self.pending_settings = None
        self.device_open = self.can_started = False
        self.running_mode = "stopped"
        self._live_verified = False
        self._live_verified_values = None
        self._live_shutdown_requested = False
        self._live_shutdown_pending = False
        self._live_applied_duties = None
        self._live_pending_duties = self._live_pending_start = None
        self._live_pending_operator = None
        self._live_observed.clear()
        self._live_observed_at.clear()
        self._live_telemetry.clear()
        self._live_telemetry_at.clear()
        self._live_received = 0
        self._live_last_rx = None
        self._clear_live_values()
        if self.live_mode == "simulation":
            self._live_timer.stop()
            for edit in self.readbacks.values():
                edit.setText("0")
            for edit in self.telemetry.values():
                edit.setText("0")
            self.telemetry["protectionTemperature"].setText("25")
            self.version.setText("0")
            self.bms_alarm.setText("0")
            self.work_voltage.setText("29.6")
            self.work_current.setText("15")
            self.fault.setText("SIMULATION — no hardware telemetry")
            for edit in self.duty.values():
                edit.setText("80.0")
            self.banner.setText("Original charger UI · English | SIMULATION ONLY — commands are NOT SENT; readback is simulated")
        else:
            for edit in self.duty.values():
                edit.setText("80.0" if self.live_mode == "manual" else "")
                edit.setToolTip("Original initial duty value, not a measurement. Edit locally; Cal. start or Apply duties sends both displayed values.")
            self.device_type.setCurrentText("USBCAN1")
            self.channel.setCurrentText("0")
            self.banner.setText("LIVE CAN · Disconnected. Close the Chinese app before opening this adapter.")
            self._live_timer.start()
        self._event("Mode selected: " + self.mode_select.currentText() + ". Manual calibration. No automatic hardware action.")
        self._refresh_controls()
        self.mode_changed.emit(self.live_mode)

    def _client(self):
        if self._live_client is None:
            if self._monitor_factory is None:
                from .can_monitor import MonitorClient
                self._live_client = MonitorClient()
            else:
                self._live_client = self._monitor_factory()
        return self._live_client

    def _live_submit(self, operation, callback):
        if self._live_disposed or self._live_closing or self._live_future is not None:
            raise ValueError("Wait for the pending CAN operation.")
        if self._live_executor is None:
            self._live_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="charger-can")
        # Each worker owns its cancellation token. A subsequent operation must
        # never clear cancellation on the batch it superseded.
        self._live_cancel_send = threading.Event()
        self._live_operation = operation
        self._live_future = self._live_executor.submit(callback)
        self._refresh_controls()

    def _live_controls_busy(self):
        """Background receive/polling never locks operator controls or recipes."""
        foreground = self._live_future is not None and self._live_operation not in ("receive", "readback_poll")
        return bool(foreground or self._live_pending_operator is not None or self._live_closing
                    or self._live_shutdown_pending or self._live_disposed)

    def _cancel_live_operator(self, reason):
        if self._live_pending_operator is not None:
            name, _ = self._live_pending_operator
            self._live_pending_operator = None
            self._event("Queued " + name + " cancelled: " + reason + ". No queued command was sent.")

    def _queue_live_operator(self, name, prepare):
        # prepare runs on the GUI thread, once the preceding SDK call completes.
        # It revalidates the frozen intent and returns transport-only worker work.
        if self._live_pending_operator is not None:
            self._event("An operator action is already queued; no duplicate command queued.")
            return False
        if self._live_controls_busy():
            raise ValueError("Wait for the current operator action or adapter close.")
        self._live_pending_operator = (name, prepare)
        if self._live_future is not None:
            self._event(name + " queued after the current CAN call, ahead of background polling.")
        self._refresh_controls()
        self._live_tick()
        return True

    def _live_open(self):
        if self.device_open:
            raise ValueError("The adapter is already open.")
        if self.device_type.currentText() != "USBCAN1" or self.channel.currentText() not in ("", "0"):
            raise ValueError("This connection supports the single-channel USBCAN-I: choose USBCAN1, channel 0.")
        self._live_written_values = None
        self._live_shutdown_requested = False
        self._live_applied_duties = self._live_pending_duties = self._live_pending_start = None
        client = self._client()
        index = int(self.device_index.currentText())
        self._event("Opening the REAL USB-CAN adapter; no charger command is being sent.")
        self._live_submit("open", lambda: client.open(index=index, channel=0))

    def _live_start(self):
        if not self.device_open:
            raise ValueError("Open the real adapter first.")
        bitrate = {"125kbps": 125000, "250kbps": 250000, "500kbps": 500000, "1000kbps": 1000000}[self.bitrate.currentText()]
        client = self._client()
        listen_only = self.live_mode == "receive"
        self._live_submit("start", lambda: client.start(bitrate=bitrate, listen_only=listen_only))

    def _live_reset(self):
        if not self.device_open:
            raise ValueError("Open the real adapter first.")
        if (self._live_closing or self._live_disposed or self._live_shutdown_pending
                or (self._live_future is not None and self._live_operation not in ("receive", "readback_poll"))):
            raise ValueError("Wait for the current operator action before resetting CAN.")
        if self._live_pending_operator is not None and self._live_pending_operator[0] == "reset":
            self._event("CAN reset is already queued; no duplicate reset queued.")
            return
        self._cancel_live_operator("CAN reset requested")
        self._live_written_values = None
        self.pending_settings = None
        self._live_deadline = 0.0
        self._live_verified = False
        self._live_verified_values = None
        self._live_applied_duties = self._live_pending_duties = None
        self._live_pending_start = None
        self._live_shutdown_requested = False
        self.running_mode = "unknown"

        def prepare():
            if not self.device_open or self._live_client is None:
                raise ValueError("Open the real adapter before resetting CAN.")
            return self._live_client.reset

        self._queue_live_operator("reset", prepare)

    def _require_live_manual(self):
        if self.live_mode != "manual" or not self.can_started:
            raise ValueError("Select Live manual control and open/start the real CAN connection first.")
        if self._live_controls_busy():
            raise ValueError("Wait for the queued or active operator action.")

    def _live_shutdown(self):
        if self.live_mode != "manual" or not self.device_open or not self.can_started:
            raise ValueError("Select Live manual control and open/start the real CAN connection first.")
        if self._live_disposed or self._live_closing:
            raise ValueError("The adapter is closing; no new charger command was queued.")
        if self._live_shutdown_pending:
            self._event("Shutdown is already queued or being sent; no duplicate command queued.")
            return
        if self._live_future is not None and self._live_operation in ("open", "start", "reset", "close"):
            raise ValueError("Wait for the adapter connection operation before requesting Shutdown.")
        self._cancel_live_operator("Shutdown requested")
        self._live_written_values = None
        self._live_shutdown_pending = True
        self._live_cancel_send.set()
        self._live_shutdown_requested = False
        self.pending_settings = None
        self._live_deadline = 0.0
        self._live_readback_issue = ""
        self._live_verified = False
        self._live_verified_values = None
        self._live_applied_duties = None
        self._live_pending_duties = None
        self._live_pending_start = None
        self.running_mode = "shutdown_queued"
        self._event("Shutdown queued with priority. Unsent adjustments/settings are cancelled; a command already in flight cannot be undone. Physical output is unconfirmed.")
        self._refresh_controls()
        self._live_tick()

    def _live_values(self):
        try:
            values = {key: float(edit.text()) for key, edit in self.settings.items()}
        except ValueError as exc:
            raise ValueError("Enter all 16 variant settings as numbers.") from exc
        encode_all_config(values)
        if values["endVoltage"] <= 0 or values["ccCurrent"] <= 0:
            raise ValueError("Enter the variant's positive end voltage and CC current before Send data.")
        if values["endCurrent"] > values["ccCurrent"]:
            raise ValueError("End current must not exceed CC current.")
        self._check_bench_limits(values)
        return values

    def set_bench_limits(self, max_voltage, max_current, max_power):
        values = (max_voltage, max_current, max_power)
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("Bench voltage, current and power limits must be finite positive numbers.")
        if self._live_controls_busy() or self.pending_settings is not None:
            raise ValueError("Wait for the operator action and settings readback before changing bench limits.")
        self.bench_limits = dict(zip(("max_voltage", "max_current", "max_power"), map(float, values)))
        self._live_written_values = None
        self._live_readback_issue = ""
        self._live_verified = False
        self._live_verified_values = None
        self._event("Bench limits applied: " + f"{max_voltage:g} V / {max_current:g} A / {max_power:g} W. Software limits do not replace fixture protection.")
        self._refresh_controls()

    def _require_manual_bench(self):
        keys = ("max_voltage", "max_current", "max_power")
        if (not isinstance(self.bench_limits, dict) or any(
                type(self.bench_limits.get(key)) not in (int, float)
                or not math.isfinite(self.bench_limits[key]) or self.bench_limits[key] <= 0 for key in keys)):
            raise ValueError("Use Bench limits in the top bar to enter approved V / A / W limits before sending data or calibrating.")
        now = time.monotonic()
        received = {key: value for key, value in self._live_telemetry.items()
                    if key in ("outputVoltage", "outputCurrent") and 0 <= now - self._live_telemetry_at.get(key, 0) <= 2}
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in received.values()):
            raise ValueError("Recent received charger output is invalid. Check the physical bench; use Shutdown if needed.")
        if (received.get("outputVoltage", 0) > self.bench_limits["max_voltage"]
                or received.get("outputCurrent", 0) > self.bench_limits["max_current"]
                or received.get("outputVoltage", 0) * received.get("outputCurrent", 0) > self.bench_limits["max_power"]):
            raise ValueError("Recent received charger telemetry exceeds a bench limit. Check the physical bench; use Shutdown if needed.")
        return dict(self.bench_limits)

    def _check_bench_limits(self, values):
        limits = self._require_manual_bench()
        voltage, current = values["endVoltage"], values["ccCurrent"]
        if (voltage > limits["max_voltage"] or current > limits["max_current"]
                or voltage * current > limits["max_power"]):
            raise ValueError("The voltage/current targets exceed the selected bench voltage, current or power limit.")

    def _send_batch(self, frames, drain=False):
        client = self._client()
        cancelled = self._live_cancel_send
        accepted = []
        attempted = None
        deadline = time.monotonic() + 10
        try:
            def drain_receive():
                for _ in range(8):
                    if cancelled.is_set():
                        return
                    if not client.receive().get("frames", []):
                        break
                else:
                    raise ValueError("Receive backlog is too large to establish fresh readback; any already accepted writes are logged.")
            if drain:
                drain_receive()
            for position, frame in enumerate(frames):
                if cancelled.is_set():
                    return {"accepted_frames": accepted, "cancelled_for_shutdown": True,
                            "unsent_count": len(frames) - len(accepted)}
                if drain and position == 16:
                    # Discard replies collected during writes, then make new
                    # explicit read requests. Matching is still not a save ACK.
                    drain_receive()
                if cancelled.is_set():
                    return {"accepted_frames": accepted, "cancelled_for_shutdown": True,
                            "unsent_count": len(frames) - len(accepted)}
                if self._live_disposed or self._live_closing:
                    raise RuntimeError("Pending send sequence cancelled; already accepted/in-flight commands cannot be undone.")
                if time.monotonic() > deadline:
                    raise RuntimeError("CAN send sequence timed out; already accepted commands are not automatically retried.")
                attempted = copy.deepcopy(frame)
                response = client.send(frame)
                if response.get("accepted") != 1:
                    raise RuntimeError("CAN driver did not accept the complete frame.")
                accepted.append(copy.deepcopy(frame))
                attempted = None
                if position + 1 < len(frames):
                    selector = frame["data"][1]
                    if selector in {spec["selector"] for spec in CONFIG.values()} or selector in (0x14, 0x16, 0x0B):
                        cancelled.wait(.1)
            return {"accepted_frames": accepted}
        except Exception as exc:
            try:
                client.close()
            except Exception:
                pass
            return {"accepted_frames": accepted, "unconfirmed_frame": attempted, "error": str(exc)}

    def _live_action(self, name, frames, validate=None, on_dispatch=None, drain=False):
        self._require_live_manual()
        if validate is not None:
            validate()
        frames = copy.deepcopy(frames)

        def prepare():
            self._require_live_manual()
            if validate is not None:
                validate()
            if on_dispatch is not None:
                on_dispatch()
            return lambda: self._send_batch(frames, drain=drain)

        return self._queue_live_operator(name, prepare)

    def _live_send_settings(self):
        self._require_live_manual()
        self._live_written_values = None
        values = self._live_values()
        frames = encode_all_config(values) + [poll(group) for group in range(1, 7)]

        def validate():
            if not self._live_shutdown_requested:
                raise ValueError("Use Shutdown before sending real settings. Physical output must be checked at the bench.")
            if self.pending_settings is not None:
                raise ValueError("Wait for the pending real settings readback.")
            if self._live_values() != values:
                raise ValueError("Variant settings changed after the click; review them and press Send data again.")

        def on_dispatch():
            self.pending_settings = dict(values)
            self._live_deadline = 0.0
            self._live_readback_issue = ""
            self._live_observed.clear()
            self._live_observed_at.clear()
            self._live_verified = False
            self._live_verified_values = None
            for edit in self.readbacks.values():
                edit.setText("—")
                edit.setToolTip("Unconfirmed: waiting for an actual readback of this setting.")

        self._live_action("send_settings", frames, validate, on_dispatch, drain=True)

    def _live_start_charger(self, mode, name):
        self._require_live_manual()
        limits = self._require_manual_bench()
        duties = self._live_duty_values() if name == "calibrationStart" else None
        frames = [command(name)]
        if duties is not None:
            frames += [encode_duty("voltage", duties["voltage"]), encode_duty("current", duties["current"])] * 3

        def validate():
            if not (self._live_shutdown_requested or (name == "calibrationStart" and self.running_mode == "calibration_requested")):
                raise ValueError("Use Shutdown and check physical output before starting the charger.")
            if self._require_manual_bench() != limits:
                raise ValueError("Bench limits changed after the click; review them before starting.")
            if duties is not None and self._live_duty_values() != duties:
                raise ValueError("Duty values changed after the click; review them before Cal. start.")

        def on_dispatch():
            self._live_pending_start = {"name": name, "frames": copy.deepcopy(frames), "duties": copy.deepcopy(duties)}
            self._live_applied_duties = None
            self._live_pending_duties = None
            self._event("Manual start uses stored charger settings; Send data is optional. Displayed configuration inputs are not transmitted.")

        self._live_action(name, frames, validate, on_dispatch)

    def _live_duty_values(self):
        try:
            values = {axis: float(edit.text()) for axis, edit in self.duty.items()}
        except ValueError as exc:
            raise ValueError("Enter valid numbers for BOTH displayed duty values before Cal. start or Apply duties.") from exc
        for axis, value in values.items():
            encode_duty(axis, value)
        return values

    def _live_duty(self, axis, delta):
        self._require_live_manual()
        limits = self._require_manual_bench()
        if self.running_mode != "calibration_requested":
            raise ValueError("Use Cal. start before real duty adjustment.")
        if axis not in ("voltage", "current") or not isinstance(delta, (int, float)) or not math.isfinite(delta):
            raise ValueError("Invalid duty adjustment.")
        original = self._live_duty_values()
        values = dict(original)
        values[axis] = round(values[axis] + delta, 7)
        frames = [encode_duty("voltage", values["voltage"]), encode_duty("current", values["current"])] * 3

        def validate():
            if self.running_mode != "calibration_requested":
                raise ValueError("Use Cal. start before duty adjustment.")
            if self._require_manual_bench() != limits:
                raise ValueError("Bench limits changed after the click; review them before applying duties.")
            if self._live_duty_values() != original:
                raise ValueError("Duty values changed after the click; review and apply them again.")

        def on_dispatch():
            self._live_pending_duties = values
            self.duty[axis].setText(f"{values[axis]:.1f}")

        self._live_action("duty_edit", frames, validate, on_dispatch)

    def _live_save(self, name):
        self._require_live_manual()
        limits = self._require_manual_bench()
        duties = self._live_duty_values()

        def validate():
            if self.running_mode != "calibration_requested":
                raise ValueError("Use Cal. start before a real calibration save.")
            if self._require_manual_bench() != limits:
                raise ValueError("Bench limits changed after the click; review them before saving.")
            if self._live_duty_values() != duties or duties != self._live_applied_duties:
                raise ValueError("Apply the entered duties and verify the bench readings before saving.")

        self._live_action(name, [command(name)], validate)

    def _record_live_frame(self, frame, status, action):
        item = copy.deepcopy(frame)
        item.update(sequence=(self.frame_log[-1]["sequence"] + 1 if self.frame_log else 1), status=status, action=action, recorded=self.recording)
        self.frame_log.append(item)
        self.frame_log[:] = self.frame_log[-10000:]
        self._event(f"{status}  {item['id']:08X}  DLC{item['dlc']}  " + " ".join(f"{byte:02X}" for byte in item["data"]) + "  [" + action + "]")

    def _receive_live(self, result):
        frames = result.get("frames", [])
        if not isinstance(frames, list) or len(frames) > 4096:
            raise RuntimeError("Invalid CAN receive response.")
        for frame in frames:
            if (not isinstance(frame, dict) or type(frame.get("id")) is not int or not 0 <= frame["id"] <= 0x1FFFFFFF
                    or type(frame.get("dlc")) is not int or not 0 <= frame["dlc"] <= 8
                    or not isinstance(frame.get("data"), list) or len(frame["data"]) != frame["dlc"]
                    or any(type(value) is not int or not 0 <= value <= 255 for value in frame["data"])):
                raise RuntimeError("Malformed received CAN frame; connection stopped.")
            self._record_live_frame(frame, "REAL RX", "receive")
            self._live_received += 1
            self._live_last_rx = time.monotonic()
            if frame.get("error"):
                continue
            try:
                decoded = decode(frame)
            except ValueError:
                continue
            values = decoded.get("values", {})
            if decoded["kind"] == "configurationReadback":
                self._live_observed.update(values)
                self._live_observed_at.update({key: time.monotonic() for key in values})
                expected = self._live_written_values or self._live_verified_values
                if expected and any(
                        key in expected and not math.isclose(value, expected[key], rel_tol=0, abs_tol=1e-8)
                        for key, value in values.items()):
                    self._live_verified = False
                    self._event("ACTUAL configuration differs from the requested values. Parameter readback is unverified; manual calibration uses stored charger settings.")
                for key, value in values.items():
                    self.readbacks[key].setText(f"{value:g}")
                    self.readbacks[key].setToolTip("Actual received CAN value; reception time recorded in trace.")
            elif decoded["kind"] == "telemetry":
                self._live_telemetry.update(values)
                self._live_telemetry_at.update({key: time.monotonic() for key in values})
                for key, value in values.items():
                    if key in self.telemetry:
                        self.telemetry[key].setText(f"{value:g}")
                        self.telemetry[key].setToolTip("Actual received CAN value; reception time recorded in trace.")
                    elif key == "faultWord":
                        self.fault.setText(describe_charger_fault(value))
                    elif key == "statusByte":
                        self.bms_alarm.setText(f"0x{value:02X}")
                        self.bms_alarm.setToolTip("Raw received status byte; individual alarm meanings are not inferred.")
        written = self._live_written_values
        if written is not None and not self._live_verified and all(key in self._live_observed for key in written):
            try:
                current = {key: float(edit.text()) for key, edit in self.settings.items()}
            except ValueError:
                current = None
            now = time.monotonic()
            if (current == written
                    and all(0 <= now - self._live_observed_at.get(key, 0) <= 8 for key in written)
                    and all(math.isclose(value, self._live_observed[key], rel_tol=0, abs_tol=1e-8) for key, value in written.items())):
                self._live_verified = True
                self._live_verified_values = dict(written)
                self.pending_settings = None
                self._live_readback_issue = ""
                self._event("ACTUAL CAN readbacks match all 16 requested settings. This does not prove calibration-save persistence.")
        if result.get("dropped_malformed"):
            self._event("Receive helper rejected malformed frames: " + str(result["dropped_malformed"]))

    def _live_failure(self, message):
        self._cancel_live_operator("the CAN operation failed; no automatic replay")
        self.last_error = str(message)
        self.device_open = self.can_started = False
        self.pending_settings = None
        self._live_verified = False
        self._live_shutdown_requested = False
        self._live_shutdown_pending = False
        self._live_applied_duties = None
        self._live_pending_duties = self._live_pending_start = None
        self.running_mode = "unknown"
        self._clear_live_values()
        self._live_telemetry.clear()
        self._live_telemetry_at.clear()
        self._event("REAL CAN ERROR: " + self.last_error + ". No automatic charger stop or retry was attempted; output state is unknown. Check the bench.")
        if self._live_client is not None and self._live_executor is not None:
            client, self._live_client = self._live_client, None
            self._live_executor.submit(client.close)

    def _complete_live(self, operation, result):
        if operation == "send_settings":
            self._live_written_values = None
        if not isinstance(result, dict):
            raise RuntimeError("Invalid CAN helper response.")
        for frame in result.get("accepted_frames", []):
            self._record_live_frame(frame, "API ACCEPTED", operation)
        if result.get("error"):
            if result.get("unconfirmed_frame") is not None:
                self._record_live_frame(result["unconfirmed_frame"], "SEND UNCONFIRMED", operation)
            raise RuntimeError(result["error"])
        if result.get("cancelled_for_shutdown"):
            if operation in ("workStart", "calibrationStart"):
                self._live_pending_start = None
                self._live_applied_duties = self._live_pending_duties = None
                self._live_shutdown_requested = False
                if not self._live_shutdown_pending:
                    self.running_mode = "unknown"
            destination = " The queued Shutdown has priority next." if self._live_shutdown_pending else " No Shutdown remains queued."
            self._event(f"Cancelled {result.get('unsent_count', 0)} unsent frame(s) from {operation}; already accepted frames remain in the trace." + destination)
            return
        if self._live_shutdown_pending and operation not in ("shutdown", "receive"):
            # The previous native call may have completed just as Shutdown was
            # requested. Log its real acceptance above, but never reinstate its
            # start/trim/settings state or verification after the stop request.
            self._event("Previous " + operation + " completed before queued Shutdown; its completion does not re-enable calibration or parameter verification.")
            return
        if operation == "open":
            if result.get("state") != "open":
                raise RuntimeError("Adapter open was not confirmed by the helper.")
            self.device_open = True
            self._event("REAL device opened successfully through the USB-CAN driver.")
        elif operation == "start":
            expected = "listening" if self.live_mode == "receive" else "active"
            if result.get("state") != expected:
                raise RuntimeError("CAN start mode was not confirmed by the helper.")
            self.can_started = True
            self._live_next_poll = time.monotonic() + 2
            self._event("REAL CAN started in " + ("listen-only mode" if self.live_mode == "receive" else "manual-control mode") + ". No automatic charger-start command.")
        elif operation == "receive":
            self._receive_live(result)
        elif operation == "reset":
            self.can_started = False
            self.pending_settings = None
            self._live_verified = False
            self._live_shutdown_requested = False
            self._clear_live_values()
            self._event("REAL CAN channel reset. No charger stop command was sent.")
        elif operation == "shutdown":
            self._live_shutdown_pending = False
            self._live_shutdown_requested = True
            self.running_mode = "stop_requested"
            self._live_next_poll = time.monotonic() + 2
            self._event("Shutdown frame API accepted; physical output is unconfirmed. Verify the load-bank current and voltage before proceeding.")
        elif operation == "send_settings":
            if self.pending_settings is None or self._live_closing or self._live_disposed:
                self._event("Completed settings send no longer has an active request; calibration eligibility was not restored.")
                return
            expected_frames = encode_all_config(self.pending_settings) + [poll(group) for group in range(1, 7)]
            if result.get("accepted_frames") != expected_frames:
                raise RuntimeError("Settings send did not confirm the complete expected 22-frame sequence; calibration remains blocked.")
            self._live_written_values = dict(self.pending_settings)
            self._live_deadline = time.monotonic() + 8
            self._live_next_poll = time.monotonic() + 1
            self._event("Settings accepted by CAN API; waiting for ACTUAL readbacks (8 second timeout).")
        elif operation in ("workStart", "calibrationStart"):
            pending = self._live_pending_start
            self._live_pending_start = None
            if self._live_closing or self._live_disposed or pending is None:
                self._event("Completed start no longer has an active request; calibration state was not restored.")
                return
            if pending["name"] != operation or result.get("accepted_frames") != pending["frames"]:
                raise RuntimeError("Start did not confirm the complete expected command sequence; initial duties are not marked applied.")
            self.running_mode = "calibration_requested" if operation == "calibrationStart" else "work_requested"
            self._live_shutdown_requested = False
            self._live_applied_duties = pending["duties"]
            self._live_pending_duties = None
            self._event("Start sequence accepted by CAN API; physical charger state must be measured. Stored charger settings were used.")
        elif operation == "duty_edit":
            self._live_applied_duties = self._live_pending_duties
            self._live_pending_duties = None
            self._event("Duty frames accepted by CAN API. Use the load bank readings to verify the adjustment.")
        elif operation.startswith("save"):
            self._event("Save frame accepted by CAN API. Charger acknowledgement and persistence are NOT confirmed; verify after power cycle.")
        elif operation == "close":
            self._cancel_live_operator("adapter closed")
            self._live_shutdown_pending = False
            self.device_open = self.can_started = False
            self._live_client = None
            self._live_closing = False
            self.pending_settings = None
            self._live_verified = False
            self._live_shutdown_requested = False
            self._clear_live_values()
            self._event("REAL adapter closed. No charger stop command was sent.")

    def _live_tick(self):
        if self._live_disposed or self.live_mode == "simulation":
            return
        if self._live_future is not None:
            if not self._live_future.done():
                return
            future, operation = self._live_future, self._live_operation
            self._live_future = None
            try:
                self._complete_live(operation, future.result())
            except Exception as exc:
                self._live_failure(exc)
            self._live_next_receive = time.monotonic() + .15
        if self._live_closing and self._live_future is None:
            self._live_closing = False
            if self._live_client is not None:
                self._live_submit("close", self._live_client.close)
                self._live_closing = True
                return
        if self._live_shutdown_pending and self._live_future is None:
            if self.live_mode != "manual" or not self.can_started or self._live_client is None:
                self._live_shutdown_pending = False
                self._event("Queued Shutdown cancelled because the CAN connection is no longer ready. No retry was made; physical output is unknown.")
            else:
                self._live_submit("shutdown", lambda: self._send_batch([command("shutdown")]))
                return
        if self._live_pending_operator is not None and self._live_future is None:
            name, prepare = self._live_pending_operator
            self._live_pending_operator = None
            try:
                callback = prepare()
            except ValueError as exc:
                # Conditions can change while receiving. Rejected intent must
                # neither transmit nor close an otherwise healthy connection.
                self._event("Queued " + name + " not sent: " + str(exc))
                self._refresh_controls()
                return
            self._live_submit(name, callback)
            return
        now = time.monotonic()
        if self.pending_settings and self._live_deadline and now > self._live_deadline:
            missing = [key for key in self.pending_settings if key not in self._live_observed]
            mismatched = [key for key, value in self.pending_settings.items()
                          if key in self._live_observed and not math.isclose(value, self._live_observed[key], rel_tol=0, abs_tol=1e-8)]
            details = (" Missing: " + ", ".join(missing) + ".") if missing else ""
            if any(CONFIG[key]["response"] == 0x18FF56E5 for key in missing):
                details += " Adjustable limits require received frame 18FF56E5."
            if mismatched:
                details += " Mismatched: " + ", ".join(mismatched) + "."
            missing_ids = sorted({CONFIG[key]["response"] for key in missing})
            self._live_readback_issue = f"Last readback failed: {len(missing)} missing / {len(mismatched)} mismatched"
            if missing_ids:
                self._live_readback_issue += " · missing " + ", ".join(f"{identity:08X}" for identity in missing_ids)
            self._live_readback_issue += ". Parameter check only; manual controls use stored settings."
            self.pending_settings = None
            self._live_verified = False
            self._event("Real settings readback timed out or did not match. Full 16-setting verification remains unavailable." + details)
        if self.can_started and self._live_future is None:
            if self.live_mode == "manual" and now >= self._live_next_poll:
                self._live_next_poll = now + (1 if self.pending_settings else 2)
                self._live_submit("readback_poll", lambda: self._send_batch([poll(group) for group in range(1, 7)]))
            elif now >= self._live_next_receive:
                self._live_submit("receive", self._client().receive)
        self._refresh_controls()

    def close_for_automatic_run(self):
        """Queue CAN handoff on the manual worker; never opens automatic CAN.

        Called from Qt's thread only. The automatic worker must await the
        returned future before acquiring the adapter. Background reads may
        finish; foreground commands must finish before starting automation.
        """
        if self._live_disposed or self.live_mode != "manual":
            raise ValueError("Select Live manual control before automatic calibration.")
        if self._live_future is not None and self._live_operation not in ("receive", "readback_poll"):
            raise ValueError("Wait for the current manual command to finish.")
        if self._live_closing:
            raise ValueError("Wait for the manual CAN connection to close.")
        if self._live_pending_operator is not None or self._live_shutdown_pending:
            raise ValueError("Wait for the queued manual command to finish.")
        self._cancel_live_operator("automatic calibration selected")
        self._live_shutdown_pending = False
        self.pending_settings = None
        self._live_written_values = None
        self._live_applied_duties = self._live_pending_duties = self._live_pending_start = None
        self._live_shutdown_requested = False
        self.running_mode = "unknown"
        if self._live_client is None:
            self.device_open = self.can_started = False
            return None
        # One executor owns all manual I/O. Its queued close runs after any
        # in-flight background read/poll; no adapter is shared across workers.
        self._live_closing = True
        self._live_operation = "close"
        self._live_future = self._live_executor.submit(self._live_client.close)
        self._event("Releasing manual CAN for automatic calibration.")
        self._refresh_controls()
        return self._live_future

    def close_live(self):
        if self.live_mode == "simulation" or self._live_disposed:
            return
        self._cancel_live_operator("adapter close requested")
        self._live_written_values = None
        self._live_applied_duties = self._live_pending_duties = self._live_pending_start = None
        self._live_shutdown_requested = False
        self.running_mode = "unknown"
        self.pending_settings = None
        if self._live_shutdown_pending:
            self._event("Queued Shutdown cancelled by adapter close; no additional shutdown frame will be sent. Any command already in flight cannot be undone.")
        self._live_shutdown_pending = False
        self._live_closing = True
        self._refresh_controls()
        self._live_tick()

    def dispose(self):
        if self._live_disposed:
            return
        self._cancel_live_operator("page disposed")
        self._live_written_values = None
        self._live_applied_duties = self._live_pending_duties = self._live_pending_start = None
        self._live_shutdown_requested = False
        self.running_mode = "unknown"
        self._live_disposed = True
        self._live_shutdown_pending = False
        self.bench_limits_button.setEnabled(False)
        self._refresh_bench_dialog()
        self._live_timer.stop()
        self.readback_timer.stop()
        if self._live_executor is not None:
            if self._live_client is not None:
                self._live_executor.submit(self._live_client.close)
            self._live_executor.shutdown(wait=False, cancel_futures=False)

    def _refresh_sim_mode_controls(self):
        self.mode_select.setEnabled(True)
        self.close_adapter_button.setEnabled(False)
        self.apply_duties_button.setEnabled(False)
        self.bench_limits_button.setEnabled(False)
        self.bench_limits_button.setToolTip("Bench limits are available in Live manual control.")
        self._refresh_bench_dialog()
        self.buttons["open_device"].setEnabled(True)
        for name in ("open_device", "start_can", "reset_can", "shutdown", "send_settings", "work_start", "calibration_start", "save_voltage", "save_full_current", "save_half_current"):
            self.buttons[name].setToolTip("Simulation only. No CAN command is sent and no hardware state is confirmed.")
        self.trace.setToolTip("Offline commands are marked NOT SENT. Earlier live-session records retain their explicit per-frame status.")
        for edit in self.settings.values():
            edit.setEnabled(True)
        for edit in (self.device_type, self.device_index, self.channel, self.bitrate, self.filter_mode, self.acc_mask, self.acc_code):
            edit.setEnabled(True)

    def _refresh_live_controls(self):
        busy = self._live_controls_busy()
        manual = self.live_mode == "manual" and self.can_started and not busy
        waiting = self.pending_settings is not None
        common_reason = ("Select Live manual control and open/start CAN." if self.live_mode != "manual" or not self.can_started
                         else "Wait for the queued or active operator action." if busy else "")
        bench_reason = ""
        if not common_reason:
            try:
                self._require_manual_bench()
            except ValueError as exc:
                bench_reason = str(exc)
        start_reason = common_reason or bench_reason
        if not start_reason and not self._live_shutdown_requested:
            start_reason = "Use Shutdown and check physical output before starting."
        cal_reason = (common_reason or bench_reason if self.running_mode == "calibration_requested" else start_reason)
        if not cal_reason:
            try:
                self._live_duty_values()
            except ValueError as exc:
                cal_reason = str(exc)
        send_reason = common_reason or ("Wait for the pending parameter readback before sending more data." if waiting else "")
        if not send_reason:
            try:
                self._live_values()
            except ValueError as exc:
                send_reason = str(exc)
            if not send_reason and not self._live_shutdown_requested:
                send_reason = "Shutdown → check physical output → Send data."
        duty_reason = common_reason or bench_reason or ("Use Cal. start before duty adjustment or save." if self.running_mode != "calibration_requested" else "")
        if not duty_reason:
            try:
                duty_values = self._live_duty_values()
            except ValueError as exc:
                duty_reason = str(exc)
        save_reason = duty_reason
        if not save_reason and duty_values != self._live_applied_duties:
            save_reason = "Apply the entered duties and verify the bench readings before saving."
        self.mode_select.setEnabled(not self.device_open and not busy)
        self.close_adapter_button.setEnabled(self._live_client is not None and not self._live_closing)
        bench_setup_reason = self._bench_setup_reason()
        self.bench_limits_button.setEnabled(not bench_setup_reason)
        self.bench_limits_button.setToolTip(bench_setup_reason or "Enter approved bench V / A / W limits. Keeps your charger settings; no CAN command is sent.")
        self._refresh_bench_dialog()
        self.apply_duties_button.setEnabled(not duty_reason)
        self.apply_duties_button.setToolTip(duty_reason or "Send both entered duties, then verify the load-bank readings before saving.")
        self.buttons["open_device"].setEnabled(not self.device_open and not busy)
        self.buttons["open_device"].setToolTip("Open the real single-channel USB-CAN adapter. Close the Chinese app first.")
        self.buttons["start_can"].setToolTip("Start the real CAN channel in the selected live mode. No automatic charger-start command.")
        self.buttons["reset_can"].setToolTip("Reset the real CAN channel. This does not stop the charger output.")
        self.buttons["shutdown"].setToolTip("Queue one Shutdown ahead of polling and unsent settings/adjustments. A native call already in flight cannot be interrupted; verify physical output independently.")
        self.buttons["send_settings"].setToolTip(send_reason or "Send all 16 real variant settings and wait for actual matching CAN readbacks.")
        self.buttons["work_start"].setToolTip(start_reason or "Send Work start using stored charger settings. Send data is optional; no duty or configuration values are transmitted.")
        self.buttons["calibration_start"].setToolTip(cal_reason or "Use stored charger settings: send Cal. start and three pairs of the displayed voltage/current duties. Send data is optional.")
        for name in ("save_voltage", "save_full_current", "save_half_current"):
            self.buttons[name].setToolTip(save_reason or "Send a real calibration-save command. Driver acceptance does not confirm charger acknowledgement or persistence.")
        self.trace.setToolTip("REAL RX is received hardware traffic. API ACCEPTED is driver acceptance, not charger acknowledgement. SEND UNCONFIRMED identifies a failed send attempt; delivery is not confirmed. NOT SENT marks older simulation events.")
        self.device_type.setToolTip("The supplied real SDK integration is for the single-channel USBCAN-I, device type 3.")
        self.device_index.setToolTip("Real adapter index; usually 0 for one connected adapter.")
        self.channel.setToolTip("Real single-channel USBCAN-I uses channel 0.")
        self.buttons["start_can"].setEnabled(self.device_open and not self.can_started and not busy)
        foreground = self._live_future is not None and self._live_operation not in ("receive", "readback_poll")
        reset_ready = (self.device_open and not foreground and not self._live_closing and not self._live_disposed
                       and not self._live_shutdown_pending
                       and not (self._live_pending_operator is not None and self._live_pending_operator[0] == "reset"))
        self.buttons["reset_can"].setEnabled(reset_ready)
        self.buttons["reset_can"].setToolTip("Reset CAN after the current receive/poll call; cancel any queued operator command. This does not stop charger output."
                                                if reset_ready else "Open the adapter and wait for an active operator command before resetting CAN. This does not stop charger output.")
        self.buttons["shutdown"].setEnabled(
            self.live_mode == "manual" and self.can_started and not self._live_disposed
            and not self._live_closing and not self._live_shutdown_pending
            and not (self._live_future is not None and self._live_operation in ("open", "start", "reset", "close")))
        self.buttons["send_settings"].setEnabled(not send_reason)
        self.buttons["work_start"].setEnabled(not start_reason)
        self.buttons["calibration_start"].setEnabled(not cal_reason)
        for name in ("voltage_plus", "voltage_minus", "current_plus", "current_minus"):
            self.buttons[name].setEnabled(not duty_reason)
            self.buttons[name].setToolTip(duty_reason or "Adjust the entered duty by 0.1 and send both duty values.")
        for name in ("save_voltage", "save_full_current", "save_half_current"):
            self.buttons[name].setEnabled(not save_reason)
        for edit in self.duty.values():
            edit.setEnabled(manual and (self._live_shutdown_requested or self.running_mode == "calibration_requested"))
        for edit in self.settings.values():
            edit.setEnabled(self.live_mode == "manual" and not busy and self.pending_settings is None)
        for edit in (self.device_type, self.device_index, self.channel, self.bitrate):
            edit.setEnabled(not self.device_open and not busy)
        for edit in (self.filter_mode, self.acc_mask, self.acc_code):
            edit.setEnabled(False)
            edit.setToolTip("Live capture accepts all classic frames; these reference filter fields are not applied.")
        self.buttons["record"].setText("Stop recording" if self.recording else "Start recording")
        label = "LIVE MANUAL" if self.live_mode == "manual" else "LIVE RECEIVE-ONLY"
        if self._live_shutdown_pending:
            status = "Sending Shutdown" if self._live_operation == "shutdown" else "Shutdown queued · waiting for current CAN call"
        elif self._live_pending_operator is not None:
            status = self._live_pending_operator[0] + " queued · waiting for current CAN call"
        elif busy:
            status = self._live_operation.replace("_", " ") + "…"
        elif self.running_mode == "stop_requested" and self.can_started:
            status = "Shutdown API accepted · physical output unconfirmed"
        elif self.can_started:
            status = f"CAN active · {self._live_received} received"
            if self._live_last_rx is not None:
                status += f" · last RX {time.monotonic()-self._live_last_rx:.1f}s ago"
            else:
                status += " · no traffic yet (inconclusive)"
        elif self.device_open:
            status = "Adapter open · CAN stopped"
        else:
            status = "Disconnected"
        next_step = ("Uses stored charger settings; Send data optional." if self.live_mode == "manual"
                     else "Receive only; no charger commands.")
        if self.live_mode == "manual" and not busy:
            if common_reason or bench_reason:
                next_step += " " + (common_reason or bench_reason)
            elif self.running_mode == "calibration_requested":
                next_step += " " + (duty_reason or "Adjust duties, verify the bench reading, then save.")
            elif cal_reason:
                next_step += " " + cal_reason
            else:
                next_step += " Cal. start is ready."
            if waiting:
                missing = [key for key in self.pending_settings if key not in self._live_observed]
                next_step += f" Parameter readback pending ({len(missing)} missing)"
                if any(CONFIG[key]["response"] == 0x18FF56E5 for key in missing):
                    next_step += " · adjustable limits: 18FF56E5"
            elif self._live_readback_issue:
                next_step += " " + self._live_readback_issue
        self.banner.setText(label + " · Manual calibration | " + status + " | " + next_step)
        self.banner.setToolTip("Load bank control is manual. A closed CAN connection does not stop the charger. API acceptance is not charger acknowledgement. "
                               + (cal_reason or "No automatic simulation fallback."))
