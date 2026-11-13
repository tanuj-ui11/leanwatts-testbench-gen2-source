"""Original-layout English charger UI with explicit simulation and live modes.

The light style and geometry are scoped to this page. Live controls use a
separate worker/helper; simulation never opens hardware or invents live data.
"""
from __future__ import annotations

import copy
import csv
import math
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QPointF, Signal
from PySide6.QtGui import QFontDatabase, QPainter, QPolygonF, QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .protocol import command, encode_all_config, encode_duty
from .live_page_support import LivePageSupport


class _ReferenceCombo(QComboBox):
    """Keep a visible classic drop arrow under both native and offscreen Qt."""
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#777777" if self.isEnabled() else "#b8b8b8"))
        x, y = self.width()-8, self.height()/2
        painter.drawPolygon(QPolygonF([QPointF(x-3, y-1), QPointF(x+3, y-1), QPointF(x, y+2)]))


class _ReplicaCanvas(QWidget):
    """Scale the screenshot's coordinate system without adding page scrolling."""

    BASE_W, BASE_H = 1040, 550

    def __init__(self, parent=None):
        super().__init__(parent)
        # Offscreen Qt does not discover Windows fonts automatically. Load the
        # installed font in place (never copy or redistribute system fonts).
        if "Segoe UI" not in QFontDatabase.families():
            font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
            if font_path.is_file():
                QFontDatabase.addApplicationFont(str(font_path))
        self.setObjectName("LegacyCanvas")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.placements = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._apply_style(11)

    def place(self, widget, x, y, w, h):
        widget.setParent(self)
        self.placements.append((widget, (x, y, w, h)))
        return widget

    def _apply_style(self, px):
        self.setStyleSheet(f"""
            QWidget#LegacyCanvas {{ background: #f0f0f0; }}
            QWidget#LegacyCanvas QWidget {{ font-family: 'Segoe UI', 'Outfit';
                font-size: {px}px; color: #242424; border-radius: 0px; min-height: 0px; min-width: 0px; }}
            QWidget#LegacyCanvas QLabel {{ background: transparent; border: none; padding: 0px; }}
            QWidget#LegacyCanvas QLabel[ink='blue'] {{ color: #338fee; }}
            QWidget#LegacyCanvas QLabel[ink='red'] {{ color: #ff2c2c; }}
            QWidget#LegacyCanvas QFrame[panel='true'] {{ border: 1px solid #989898; background: #f0f0f0; }}
            QWidget#LegacyCanvas QLineEdit {{ background: white; color: #333; border: 1px solid #e2e2e2;
                border-bottom-color: #aaa; border-radius: 0px; padding: 0px 3px; selection-background-color: #aed8ff; }}
            QWidget#LegacyCanvas QLineEdit[readOnly='true'] {{ background: #f0f0f0; }}
            QWidget#LegacyCanvas QComboBox {{ background: white; color: #242424; border: 1px solid #b6b6b6;
                border-radius: 1px; padding: 0px 4px; }}
            QWidget#LegacyCanvas QComboBox::drop-down {{ width: 14px; border: none; }}
            QWidget#LegacyCanvas QComboBox::down-arrow {{ width: 0px; height: 0px; image: none; }}
            QWidget#LegacyCanvas QComboBox QAbstractItemView {{ background: white; color: #242424; }}
            QWidget#LegacyCanvas QPushButton {{ background: #fcfcfc; color: #338fee;
                border: 1px solid #cecece; border-radius: 3px; padding: 0px; font-weight: 400; }}
            QWidget#LegacyCanvas QPushButton:hover {{ background: #e6f2ff; }}
            QWidget#LegacyCanvas QPushButton:pressed {{ background: #cfe4f8; }}
            QWidget#LegacyCanvas QPushButton:disabled {{ background: #fafafa; color: #b1b1b1; border-color: #ececec; }}
            QWidget#LegacyCanvas QPushButton[ink='red'] {{ color: #ff2c2c; }}
            QWidget#LegacyCanvas QPushButton[tab='true'] {{ border-radius: 0px; color: #202020; border-color: #e3e3e3; }}
            QWidget#LegacyCanvas QPushButton[tab='true']:disabled {{ color: #505050; background: #f4f4f4; }}
            QWidget#LegacyCanvas QCheckBox {{ background: transparent; color: #242424; padding: 0px; spacing: 2px; }}
            QWidget#LegacyCanvas QCheckBox::indicator {{ width: 12px; height: 12px; background: #f8f8f8;
                border: 1px solid #a8a8a8; border-radius: 2px; }}
            QWidget#LegacyCanvas QPlainTextEdit {{ color: #214767; background: #b8d1e9; border: 1px solid #989898;
                border-radius: 0px; padding: 3px; font-family: 'Consolas', 'Microsoft YaHei UI'; font-size: {max(9,px-1)}px; }}
        """)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        scale = min(self.width()/self.BASE_W, self.height()/self.BASE_H)
        dx = max(0, (self.width()-self.BASE_W*scale)/2)
        dy = max(0, (self.height()-self.BASE_H*scale)/2)
        for child, (x, y, w, h) in self.placements:
            child.setGeometry(round(dx+x*scale), round(dy+y*scale), max(1,round(w*scale)), max(1,round(h*scale)))
        self._apply_style(max(9, round(11*scale)))


class LegacyChargerPage(LivePageSupport, QWidget):
    """Original-style controls with distinct simulated and live CAN workflows.

    Preview preconditions (CAN open, stopped before settings, calibration mode
    before saves) are local demonstration guards, not verified firmware rules.
    """

    mode_changed = Signal(str)

    def __init__(self, parent=None, monitor_factory=None):
        super().__init__(parent)
        self.settings: dict[str, QLineEdit] = {}
        self.readbacks: dict[str, QLineEdit] = {}
        self.buttons: dict[str, QPushButton] = {}
        self.duty: dict[str, QLineEdit] = {}
        self.frame_log: list[dict] = []
        self.event_log: list[str] = []
        self.device_open = False
        self.can_started = False
        self.running_mode = "stopped"
        self.recording = False
        self.last_error = ""
        self.pending_settings: dict[str, float] | None = None
        self.data_dialog = None
        self._init_live(monitor_factory)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.banner = QLabel("Original charger UI · English  |  SIMULATION ONLY — commands are NOT SENT; readback is simulated")
        self.banner.setStyleSheet("color:#f5b656; font-size:12px; padding:2px 6px;")
        self.banner.setWordWrap(True)
        self.banner.setToolTip("Original Chinese layout. The preview does not access the charger, USB-CAN adapter or load bank.")
        layout.addLayout(self._mode_bar())
        layout.addWidget(self.banner)
        self.canvas = _ReplicaCanvas()
        layout.addWidget(self.canvas, 1)
        self.readback_timer = QTimer(self)
        self.readback_timer.setSingleShot(True)
        self.readback_timer.timeout.connect(self.complete_readback)
        self._build()
        self._refresh_controls()

    def _label(self, text, x, y, w, h=22, ink=None, tooltip=""):
        item = QLabel(text)
        if ink:
            item.setProperty("ink", ink)
        item.setToolTip(tooltip)
        return self.canvas.place(item, x, y, w, h)

    def _field(self, text, x, y, w, h=20, readonly=False, tooltip=""):
        item = QLineEdit(text)
        item.setReadOnly(readonly)
        item.setToolTip(tooltip)
        return self.canvas.place(item, x, y, w, h)

    def _button(self, key, text, x, y, w, h, action, tooltip, ink=None):
        item = QPushButton(text)
        item.setCursor(Qt.CursorShape.PointingHandCursor)
        item.setToolTip(tooltip)
        if ink:
            item.setProperty("ink", ink)
        if action:
            item.clicked.connect(lambda checked=False: self._guarded(action))
        self.buttons[key] = item
        return self.canvas.place(item, x, y, w, h)

    def _combo(self, values, x, y, w, h=22, tooltip=""):
        item = _ReferenceCombo()
        item.addItems(values)
        item.setToolTip(tooltip)
        return self.canvas.place(item, x, y, w, h)

    def _panel(self, x, y, w, h):
        item = QFrame()
        item.setProperty("panel", True)
        return self.canvas.place(item, x, y, w, h)

    def _build(self):
        # Coordinates reproduce the supplied image below its native title bar.
        self._panel(18, 47, 365, 365)
        for n, (text, width) in enumerate((("CAN setup", 62), ("JK", 40), ("IDN", 42), ("PYLON", 49), ("JBD", 40))):
            x = 12 + sum((62, 40, 42, 49, 40)[:n])
            tab = self._button("tab_"+str(n), text, x, 19, width, 21, None,
                               "CAN startup panel" if n == 0 else "Reference tab only; this protocol is not implemented in the preview.")
            tab.setProperty("tab", True)
            tab.setEnabled(n == 0)
        left_labels = [("Device type:", 59, "Device type"), ("CAN index:", 99, "CAN device index"),
                       ("Channel:", 139, "CAN channel"), ("Bitrate:", 179, "CAN bitrate"),
                       ("Filter:", 219, "Filter mode"), ("Mask: 0X", 258, "Acceptance mask"),
                       ("Code: 0X", 300, "Acceptance code")]
        for text, y, tip in left_labels:
            self._label(text, 50, y, 65, ink="blue", tooltip=tip)
        self.device_type = self._combo(["USBCAN1", "USBCAN2"], 117, 59, 82, tooltip="Visual adapter selection only; no driver is opened.")
        self.device_index = self._combo(["0", "1", "2", "3"], 117, 99, 82, tooltip="Simulated device index")
        self.channel = self._combo(["", "0", "1"], 117, 139, 82, tooltip="Simulated channel selection")
        self.bitrate = self._combo(["250kbps", "500kbps", "125kbps", "1000kbps"], 117, 179, 82, tooltip="Screenshot default; actual bus rate still requires verification.")
        self.filter_mode = self._combo(["Standard", "Extended"], 117, 219, 82, tooltip="Reference receive-filter selection. Recovered calibration commands use extended frames regardless of this display.")
        self.acc_mask = self._field("FFFFFFFF", 118, 258, 79, tooltip="Reference acceptance mask; no device filter is configured.")
        self.acc_code = self._field("18FF50E5", 118, 300, 79, tooltip="Reference acceptance code; no device filter is configured.")
        self._button("open_device", "Open device", 230, 59, 81, 31, self.open_device, "Open device — simulation only")
        self._button("start_can", "Start CAN", 230, 101, 81, 31, self.start_can, "Start CAN — simulation only")
        self._button("reset_can", "Reset CAN", 230, 143, 81, 31, self.reset_can, "Reset simulated CAN and cancel pending readback")
        self._label("SW version:", 253, 242, 83, ink="blue", tooltip="Software version; no hardware has been queried")
        self.version = self._field("0", 232, 276, 100, readonly=True)
        self._button("export_data", "Export data", 27, 350, 96, 27, self.export_data, "Export the offline frame/event log as CSV")
        self._button("view_data", "View data", 140, 350, 104, 27, self.show_data, "Inspect the offline command and event log")
        record = self._button("record", "Start recording", 270, 350, 107, 27, self.toggle_recording, "Toggle recording flags on subsequent offline frames")
        record.setStyleSheet("background:#86cee5;color:#338fee;")
        blank = self._field("", 27, 385, 350, 23, readonly=True)
        blank.setStyleSheet("background:white;border:1px solid #e4e4e4;")
        self.trace = QPlainTextEdit()
        self.trace.setReadOnly(True)
        self.trace.setMaximumBlockCount(500)
        self.trace.setToolTip("Offline trace only. NOT SENT means no command reached a device.")
        self.canvas.place(self.trace, 18, 414, 365, 123)

        self._panel(398, 18, 614, 208)
        columns = [
            (408, 501, 557, [("batteryType", "Battery type:", "Battery type code"),
                ("ratedVoltage", "Rated V (V):", "Rated voltage / V"),
                ("startVoltage", "Start V (V):", "Start-charge voltage / V"),
                ("temperatureLimit", "Temp limit (°C):", "Temperature protection point / C"),
                ("derating110", "Derate 110 (%):", "110 V derating / percent"),
                ("derating220", "Derate 220 (%):", "220 V derating / percent")]),
            (608, 693, 744, [("maxChargeMinutes", "Max time (min)", "Maximum charge duration / minutes"),
                ("endVoltage", "End V (V):", "Full-charge/end voltage / V"),
                ("ccCurrent", "Charge I (A):", "Charge/CC current / A"),
                ("rechargeVoltage", "Recharge V (V):", "Recharge voltage / V"),
                ("endCurrent", "End I (A):", "Full-charge/end current / A"),
                ("blindCharge", "Blind charge:", "Blind-charge numeric code; mode meanings remain unverified")]),
        ]
        for label_x, input_x, read_x, entries in columns:
            for row, (key, text, tip) in enumerate(entries):
                y = 30 + row*29
                self._label(text, label_x, y, input_x-label_x-2, 20, "red", tip)
                self.settings[key] = self._field("0", input_x, y, 49, tooltip=tip+" — editable input")
                self.readbacks[key] = self._field("0", read_x, y, 49 if read_x < 600 else 55, readonly=True,
                                                 tooltip=tip+" — SIMULATED readback after Send data")
        for row, (key, text, tip) in enumerate([
            ("adjustVoltageMax", "Adj. V (V)", "Adjustable voltage / V"),
            ("adjustVoltageMin", "Adj. V lower", "Adjustable voltage lower limit / V"),
            ("adjustCurrentMax", "Adj. I (A):", "Adjustable current / A"),
            ("adjustCurrentMin", "Adj. I lower", "Adjustable current lower limit / A"),
        ]):
            y = 54 + row*24
            self._label(text, 820, y, 76, 20, "red", tip)
            self.settings[key] = self._field("0", 895, y, 47, tooltip=tip+" — editable input")
            self.readbacks[key] = self._field("0", 954, y, 47, readonly=True, tooltip="SIMULATED readback: "+tip)
        self._label("Settings", 825, 173, 76, 26, tooltip="Parameter settings area: input on the left, simulated readback on the right")
        self._button("send_settings", "Send data", 915, 175, 72, 25, self.send_settings, "Send data — generate 16 offline frames, then simulate matching readback")

        self._panel(388, 232, 624, 52)
        self._button("work_start", "Work start", 397, 243, 73, 28, self.work_start, "Work start: offline command 00 14 00 00", "red")
        self._label("Charge V (V):", 475, 245, 82, tooltip="Reference command-voltage display; not connected to live BMS traffic")
        self.work_voltage = self._field("29.6", 556, 246, 54, readonly=True,
            tooltip="Screenshot reference value only. Periodic BMS setpoint traffic is not implemented.")
        self._label("Charge I (A):", 616, 245, 82, tooltip="Reference command-current display")
        self.work_current = self._field("15", 698, 246, 52, readonly=True,
            tooltip="Screenshot reference value only; not a recommended charger target.")
        self.heater = QCheckBox("Heat")
        self.heater.setEnabled(False)
        self.heater.setToolTip("Heating control is not implemented or verified in this preview.")
        self.canvas.place(self.heater, 765, 246, 52, 22)
        self._label("CAN protocol", 826, 238, 88, tooltip="CAN protocol selection — reference only")
        self.protocol_select = self._combo(["Qixiong 001", "BMS 002"], 820, 260, 82, tooltip="Alternate BMS protocol behaviour is not implemented in this preview.")
        self.protocol_select.setEnabled(False)
        self._label("BMS alarm", 948, 238, 62, tooltip="BMS alarm — no hardware reading")
        self.bms_alarm = self._field("0", 947, 261, 45, 18, readonly=True)
        self.bms_alarm.setStyleSheet("background:#00e7ed;border:none;color:#555;")
        self._button("shutdown", "Shutdown", 779, 289, 73, 27, self.shutdown, "Shutdown: offline command 00 14 00 80", "red")
        self._button("calibration_start", "Cal. start", 938, 291, 73, 27, self.calibration_start,
                     "Calibration start: offline command 00 14 00 01")

        self._panel(388, 296, 379, 250)
        telemetry = [("inputVoltage", "Input V (V)", 399, 488, 315, "0"),
                     ("outputVoltage", "Output V (V)", 399, 488, 354, "0"),
                     ("outputCurrent", "Output I (A)", 399, 488, 393, "0"),
                     ("ambientTemperature", "Ambient (°C)", 399, 488, 433, "0"),
                     ("pfcVoltage", "PFC V (V):", 582, 679, 320, "0"),
                     ("maxPower", "Max power (W):", 582, 679, 359, "0"),
                     ("workMinutes", "Run time (min):", 582, 679, 401, "0"),
                     ("protectionTemperature", "Protect temp (°C):", 582, 679, 442, "25")]
        self.telemetry = {}
        for key, text, label_x, field_x, y, value in telemetry:
            self._label(text, label_x, y, field_x-label_x, 22, tooltip="Screenshot reference: no measured hardware telemetry")
            self.telemetry[key] = self._field(value, field_x, y, 77, readonly=True,
                                             tooltip="Reference placeholder, not a physical measurement")
        self._label("Fault:", 399, 478, 80, tooltip="Fault/status display — simulated only")
        self.fault = self._field("SIMULATION — no hardware telemetry", 405, 494, 352, readonly=True)

        self._panel(775, 331, 265, 215)
        self._label("Voltage duty:", 785, 351, 88, tooltip="Charger voltage calibration duty, not load voltage")
        self.duty["voltage"] = self._field("80.0", 870, 348, 79, tooltip="Displayed voltage duty; raw CAN value is ×10. Safe hardware range is unverified.")
        self._button("voltage_plus", "+", 780, 387, 60, 27, lambda: self.adjust_duty("voltage", .1), "Increase displayed voltage duty by 0.1")
        self._button("voltage_minus", "-", 862, 387, 49, 27, lambda: self.adjust_duty("voltage", -.1), "Decrease displayed voltage duty by 0.1")
        self._button("save_voltage", "Save voltage", 935, 385, 99, 29, self.save_voltage, "Voltage calibration save: offline 00 17; no save acknowledgement")
        self._label("Current duty:", 790, 467, 87, tooltip="Charger current calibration duty, not load current")
        self.duty["current"] = self._field("80.0", 875, 464, 46, tooltip="Displayed current duty; raw CAN value is ×10. Safe hardware range is unverified.")
        self._button("save_half_current", "Save half current", 935, 460, 99, 28, self.save_half_current, "Half-current save: offline 00 19; not required by the supplied full-current process")
        self._button("current_plus", "+", 785, 503, 60, 27, lambda: self.adjust_duty("current", .1), "Increase displayed current duty by 0.1")
        self._button("current_minus", "-", 868, 503, 49, 27, lambda: self.adjust_duty("current", -.1), "Decrease displayed current duty by 0.1")
        self._button("save_full_current", "Save full current", 935, 502, 99, 28, self.save_full_current, "Full-current save: offline 00 18; no save acknowledgement")
        for axis, edit in self.duty.items():
            edit.editingFinished.connect(lambda a=axis: self._guarded(lambda: self.adjust_duty(a, 0)) if self.live_mode == "simulation" else None)

    def _guarded(self, action):
        try:
            self.last_error = ""
            result = action()
            self._refresh_controls()
            return result
        except (ValueError, OSError, RuntimeError) as exc:
            self.last_error = str(exc)
            self._event("REJECTED: " + self.last_error)
            self._refresh_controls()
            return False

    def _event(self, text):
        entry = datetime.now().strftime("%H:%M:%S") + "  " + text
        self.event_log.append(entry)
        self.event_log[:] = self.event_log[-5000:]
        self.trace.appendPlainText(entry)

    def _append_frames(self, label, frames):
        if self.live_mode != "simulation":
            raise ValueError("Offline command generation is unavailable in live mode.")
        for frame in frames:
            item = copy.deepcopy(frame)
            item.update(sequence=len(self.frame_log)+1, action=label, status="NOT SENT", recorded=self.recording)
            self.frame_log.append(item)
            data = " ".join(f"{b:02X}" for b in item["data"])
            self._event(f"NOT SENT  {item['id']:08X}  DLC{item['dlc']}  {data}  [{label}]")

    def _require_can(self):
        if not self.can_started:
            raise ValueError("Open device and Start CAN first (simulation only).")

    def _require_calibration(self):
        self._require_can()
        if self.running_mode != "calibration":
            raise ValueError("Use Calibration start before duty adjustment or save in this preview.")

    def open_device(self):
        if self.live_mode != "simulation":
            return self._live_open()
        self.device_open = True
        self._event("Device opened in simulation. No USB device or driver was accessed.")
        self._refresh_controls()

    def start_can(self):
        if self.live_mode != "simulation":
            return self._live_start()
        if not self.device_open:
            raise ValueError("Use Open device first.")
        self.can_started = True
        self._event("CAN started in simulation. No bus was opened.")
        self._refresh_controls()

    def reset_can(self):
        if self.live_mode != "simulation":
            return self._live_reset()
        self.readback_timer.stop()
        self.pending_settings = None
        self.can_started = False
        self.running_mode = "stopped"
        self._event("Simulated CAN reset; output state stopped and pending readback cancelled.")
        self._refresh_controls()

    def shutdown(self):
        if self.live_mode != "simulation":
            return self._live_shutdown()
        self._require_can()
        self._append_frames("shutdown", [command("shutdown")])
        self.running_mode = "stopped"
        self._event("Simulated shutdown only; physical output state is unknown.")
        self._refresh_controls()

    def send_settings(self):
        if self.live_mode != "simulation":
            return self._live_send_settings()
        self._require_can()
        if self.running_mode != "stopped":
            raise ValueError("Use Shutdown before sending settings in this preview.")
        if self.pending_settings is not None:
            raise ValueError("Wait for the pending simulated readback.")
        values = {}
        for key, edit in self.settings.items():
            try:
                values[key] = float(edit.text().strip())
            except ValueError as exc:
                raise ValueError(f"Enter a numeric value for {key}.") from exc
        frames = encode_all_config(values)  # Validate the entire set before changing state.
        self._append_frames("send_settings", frames)
        self.pending_settings = dict(values)
        self._event("Waiting 350 ms for SIMULATED readback; no real acknowledgement is expected.")
        self.readback_timer.start(350)
        self._refresh_controls()

    def complete_readback(self):
        if self.live_mode != "simulation":
            return
        self.readback_timer.stop()
        if self.pending_settings is None or not self.can_started:
            return
        values = self.pending_settings
        for key, value in values.items():
            self.readbacks[key].setText(f"{value:g}")
        self.pending_settings = None
        self._event("SIMULATED readback copied all 16 inputs. This is not device verification.")
        self._refresh_controls()

    def _start(self, mode, name):
        if self.live_mode != "simulation":
            return self._live_start_charger(mode, name)
        self._require_can()
        if self.pending_settings is not None:
            raise ValueError("Wait for the pending simulated settings readback.")
        self._append_frames(name, [command(name)])
        self.running_mode = mode
        self._event(f"{mode.capitalize()} state entered in simulation. Hardware telemetry remains unmeasured.")
        self._refresh_controls()

    def work_start(self):
        self._start("work", "workStart")

    def calibration_start(self):
        self._start("calibration", "calibrationStart")

    def adjust_duty(self, axis, delta):
        if self.live_mode != "simulation":
            return self._live_duty(axis, delta)
        self._require_calibration()
        if axis not in ("current", "voltage") or not isinstance(delta, (int, float)) or not math.isfinite(delta):
            raise ValueError("Choose a known duty axis and a finite adjustment.")
        try:
            values = {name: float(edit.text()) for name, edit in self.duty.items()}
        except ValueError as exc:
            raise ValueError("Both duty fields must contain numbers.") from exc
        # First validate the typed values (do not silently round unsupported precision).
        encode_duty("voltage", values["voltage"])
        encode_duty("current", values["current"])
        values[axis] = round(values[axis] + delta, 7)
        pair = [encode_duty("voltage", values["voltage"]), encode_duty("current", values["current"])]
        # Original timer emits both axes three times after an edit; no timing claim is made here.
        self._append_frames("duty_edit", pair*3)
        self.duty[axis].setText(f"{values[axis]:.1f}")
        self._event("Offline duty plan: three pairs, preserving the other axis. Load settings are unchanged.")

    def _save(self, name):
        if self.live_mode != "simulation":
            return self._live_save(name)
        self._require_calibration()
        for axis, edit in self.duty.items():
            try:
                value = float(edit.text())
            except ValueError as exc:
                raise ValueError("Both duty fields must contain numbers before planning a save.") from exc
            encode_duty(axis, value)
        self._append_frames(name, [command(name)])
        self._event("Save command planned only. No EEPROM write, save acknowledgement or calibration result is claimed.")

    def save_voltage(self):
        self._save("saveVoltage")

    def save_full_current(self):
        self._save("saveFullCurrent")

    def save_half_current(self):
        self._save("saveHalfCurrent")

    def stop_preview(self):
        if self.live_mode != "simulation":
            return self.close_live()
        self.readback_timer.stop()
        self.pending_settings = None
        self.running_mode = "stopped"
        self.can_started = False
        self.recording = False
        self._event("Preview stopped; no physical shutdown was attempted or confirmed.")
        self._refresh_controls()

    def toggle_recording(self):
        self.recording = not self.recording
        self._event(("Offline" if self.live_mode == "simulation" else "Live") + " recording " + ("started" if self.recording else "stopped") + "; export includes recorded flags.")
        self._refresh_controls()

    def save_frame_log(self, path):
        with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Validity", "See per-frame status: NOT SENT=simulation; REAL RX=received; API ACCEPTED=send accepted, charger acknowledgement/persistence unverified. Logs retain at most 10000 frames and 5000 events."])
            writer.writerow(["Sequence", "Action", "ID hex", "Extended", "DLC", "Data hex", "Status", "Recording active"])
            for item in self.frame_log:
                writer.writerow([item["sequence"], item["action"], f"{item['id']:08X}", item["extended"], item["dlc"],
                                 " ".join(f"{b:02X}" for b in item["data"]), item["status"], item["recorded"]])
            writer.writerow([])
            writer.writerow(["Events"])
            for entry in self.event_log:
                writer.writerow([entry])
        self._event("Trace exported to " + Path(path).name)

    def export_data(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export original UI trace", "CAN-UI-trace.csv", "CSV (*.csv)")
        if path:
            self.save_frame_log(path)

    def show_data(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Original UI — CAN trace")
        dialog.resize(900, 480)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit("NOT SENT=simulation. REAL RX=received. API ACCEPTED does not prove charger acknowledgement or calibration persistence.\n\n" + "\n".join(self.event_log))
        text.setReadOnly(True)
        layout.addWidget(text)
        self.data_dialog = dialog
        dialog.show()

    def _refresh_controls(self):
        if not self.buttons:
            return
        if self.live_mode != "simulation":
            return self._refresh_live_controls()
        self._refresh_sim_mode_controls()
        self.buttons["start_can"].setEnabled(self.device_open and not self.can_started)
        self.buttons["reset_can"].setEnabled(self.device_open)
        for name in ("work_start", "calibration_start", "shutdown"):
            self.buttons[name].setEnabled(self.can_started and self.pending_settings is None)
        self.buttons["send_settings"].setEnabled(self.can_started and self.running_mode == "stopped" and self.pending_settings is None)
        for name in ("voltage_plus", "voltage_minus", "current_plus", "current_minus", "save_voltage", "save_full_current", "save_half_current"):
            self.buttons[name].setEnabled(self.can_started and self.running_mode == "calibration")
        for edit in self.duty.values():
            edit.setEnabled(self.can_started and self.running_mode == "calibration")
        self.buttons["record"].setText("Stop recording" if self.recording else "Start recording")

