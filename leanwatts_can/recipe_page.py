"""Variant editor and shared manual CAN controls; no hardware opened on creation."""
from __future__ import annotations

import copy
import json
import math
import os
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget)

from . import theme
from .core import Recipe
from .protocol import encode_all_config
from .recipe_defaults import apply_automatic_settings, automatic_settings_summary


PARAMETER_PAIRS = [
    (("batteryType", "Battery type"), ("maxChargeMinutes", "Maximum charge time / min")),
    (("ratedVoltage", "Rated voltage / V"), ("endVoltage", "End voltage / V")),
    (("startVoltage", "Start voltage / V"), ("ccCurrent", "CC current / A")),
    (("temperatureLimit", "Protection temperature / °C"), ("rechargeVoltage", "Recharge voltage / V")),
    (("derating110", "110 V derating / %"), ("endCurrent", "End current / A")),
    (("derating220", "220 V derating / %"), ("blindCharge", "Blind charge")),
    (("adjustVoltageMax", "Adjustable voltage / V"), ("adjustCurrentMax", "Adjustable current / A")),
    (("adjustVoltageMin", "Voltage lower limit / V"), ("adjustCurrentMin", "Current lower limit / A")),
]
PROGRAMMED = {"ccCurrent": "programmed_cc_current", "endVoltage": "programmed_end_voltage",
              "endCurrent": "programmed_end_current"}
MAIN_FIELDS = (("ccCurrent", "cc_current", "Full current / A"),
               ("endVoltage", "end_voltage", "End voltage / V"),
               ("endCurrent", "end_current", "Cutoff current / A"))
EDITABLE_FIELDS = ("cc_current", "end_voltage", "end_current", "max_voltage", "max_current")
AUTOMATIC_FIELDS = ("current_tolerance", "voltage_tolerance", "end_current_tolerance", "max_power",
                    "current_cal_load_voltage", "voltage_cal_load_current",
                    "cc_test_load_voltage", "voltage_test_load_current")
RAMP_DEFAULTS = dict(start_current=5, minimum_current=.1, step_current=.1,
                     dwell_seconds=3, off_voltage=5, off_current=.05,
                     confirm_seconds=3, max_seconds=600)


class RecipeInputError(ValueError):
    def __init__(self, message, field):
        super().__init__(message)
        self.field = field


def caption(value):
    label = QLabel(value)
    label.setObjectName("Caption")
    label.setWordWrap(True)
    return label


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


class RecipePage(QWidget):
    profiles_changed = Signal(list, int)

    def __init__(self, data_dir, legacy_page, parent=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self.legacy = legacy_page
        self.records = []
        self._load_error = ""
        self._busy = False
        self._showing_recipe = False
        self._invalid_field = None
        self._blank_fields = []
        self.field_labels = {}
        self.parameters, self.live_values, self.fields, self.ramp_fields = {}, {}, {}, {}
        self._build()
        for edit in (self.name, *self.parameters.values(), *(self.fields[key] for key in EDITABLE_FIELDS)):
            edit.textChanged.connect(self._mark_unsaved)
        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self.refresh_live)
        self._timer.start()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        toolbar = QHBoxLayout()
        self.selector = QComboBox()
        self.selector.setMinimumWidth(230)
        self.selector.currentIndexChanged.connect(self.select_record)
        toolbar.addWidget(self.selector, 2)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Variant name")
        toolbar.addWidget(self.name, 2)
        self.new_button = QPushButton("New variant")
        self.new_button.clicked.connect(self.new_variant)
        toolbar.addWidget(self.new_button)
        self.save_button = QPushButton("Save variant")
        self.save_button.setObjectName("Primary")
        self.save_button.clicked.connect(self.save_variant)
        toolbar.addWidget(self.save_button)
        layout.addLayout(toolbar)
        self.status = caption("New variant · Unsaved changes. Enter a name, then save your draft.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setAccessibleName("Variant save status")
        layout.addWidget(self.status)
        self._set_status(self.status.text(), "info")

        can = theme.Card()
        can.body.setContentsMargins(12, 8, 12, 8)
        controls = QHBoxLayout()
        controls.addWidget(caption("CAN device"))
        self.device = QComboBox()
        self.device.addItems(["0", "1", "2", "3"])
        self.device.currentTextChanged.connect(lambda value: self.legacy.device_index.setCurrentText(value))
        controls.addWidget(self.device)
        self.bitrate = QComboBox()
        self.bitrate.addItems(["250kbps", "500kbps", "125kbps", "1000kbps"])
        self.bitrate.currentTextChanged.connect(lambda value: self.legacy.bitrate.setCurrentText(value))
        controls.addWidget(self.bitrate)
        self.can_buttons = {}
        for key, name in (("open_device", "Open device"), ("start_can", "Start CAN"),
                          ("reset_can", "Stop CAN")):
            button = QPushButton(name)
            button.clicked.connect(lambda checked=False, k=key: self.legacy.buttons[k].click())
            controls.addWidget(button)
            self.can_buttons[key] = button
        self.close_can = QPushButton("Close device")
        self.close_can.clicked.connect(self.legacy.close_adapter_button.click)
        controls.addWidget(self.close_can)
        controls.addStretch()
        self.can_status = caption("CAN closed")
        controls.addWidget(self.can_status)
        can.body.addLayout(controls)
        layout.addWidget(can)

        content = QHBoxLayout()
        content.setSpacing(10)
        parameters = theme.Card("Charger parameters", "Set the charger values and the measurements you require.")
        parameters.body.setContentsMargins(12, 12, 12, 12)
        self.parameter_tabs = QTabWidget()
        self.main_tab = QWidget()
        main_layout = QVBoxLayout(self.main_tab)
        main_layout.setContentsMargins(8, 10, 8, 8)
        self.parameter_table = QTableWidget(3, 4)
        self.parameter_table.setHorizontalHeaderLabels(["Parameter", "Programmed", "Required measurement", "Live read"])
        self.parameter_table.verticalHeader().setVisible(False)
        self.parameter_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.parameter_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.parameter_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        theme.style_table(self.parameter_table, row_h=42)
        self.parameter_table.setFixedHeight(166)
        for row, (parameter, field, title) in enumerate(MAIN_FIELDS):
            self.parameter_table.setItem(row, 0, QTableWidgetItem(title))
            programmed = QLineEdit("0")
            required = QLineEdit("0")
            self.parameters[parameter] = programmed
            self.fields[field] = required
            self.field_labels[parameter] = "Programmed " + title
            self.field_labels[field] = "Required " + title
            self.parameter_table.setCellWidget(row, 1, programmed)
            self.parameter_table.setCellWidget(row, 2, required)
            live = QLabel("—")
            live.setAlignment(Qt.AlignmentFlag.AlignCenter)
            live.setToolTip("No charger readback received.")
            self.live_values[parameter] = live
            self.parameter_table.setCellWidget(row, 3, live)
        main_layout.addWidget(self.parameter_table)
        main_layout.addWidget(caption("Programmed values are sent to the charger. Required measurements are the results each charger must achieve; they may be different."))
        main_layout.addStretch()
        self.parameter_tabs.addTab(self.main_tab, "Main parameters")

        self.other_tab = QWidget()
        other_layout = QVBoxLayout(self.other_tab)
        other_layout.setContentsMargins(8, 10, 8, 8)
        other_parameters = [(key, title) for pair in PARAMETER_PAIRS for key, title in pair if key not in PROGRAMMED]
        self.other_parameter_table = QTableWidget((len(other_parameters) + 1) // 2, 6)
        self.other_parameter_table.setHorizontalHeaderLabels(["Parameter", "Set", "Read", "Parameter", "Set", "Read"])
        self.other_parameter_table.verticalHeader().setVisible(False)
        self.other_parameter_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.other_parameter_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.other_parameter_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        theme.style_table(self.other_parameter_table, row_h=34)
        for index, (key, title) in enumerate(other_parameters):
            row, column = index // 2, index % 2 * 3
            self.other_parameter_table.setItem(row, column, QTableWidgetItem(title))
            edit = QLineEdit("0")
            edit.setMinimumWidth(42)
            self.other_parameter_table.setCellWidget(row, column + 1, edit)
            self.parameters[key] = edit
            self.field_labels[key] = title
            value = QLabel("—")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value.setToolTip("No charger readback received.")
            self.other_parameter_table.setCellWidget(row, column + 2, value)
            self.live_values[key] = value
        for column in (1, 2, 4, 5):
            self.other_parameter_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.other_parameter_table.setColumnWidth(column, 62)
        other_layout.addWidget(self.other_parameter_table)
        self.parameter_tabs.addTab(self.other_tab, "Other charger parameters")
        parameters.body.addWidget(self.parameter_tabs, 1)
        actions = QHBoxLayout()
        self.shutdown = QPushButton("Shutdown")
        self.shutdown.clicked.connect(self.legacy.buttons["shutdown"].click)
        actions.addWidget(self.shutdown)
        self.send = QPushButton("Send data now")
        self.send.clicked.connect(self.send_parameters)
        actions.addWidget(self.send)
        self.copy_live = QPushButton("Use live values")
        self.copy_live.clicked.connect(self.copy_live_values)
        actions.addWidget(self.copy_live)
        actions.addStretch()
        parameters.body.addLayout(actions)
        self.command_status = caption("Live reads appear after CAN starts. Stop CAN stops communication, not charger output.")
        parameters.body.addWidget(self.command_status)
        content.addWidget(parameters, 3)

        limits = theme.Card("Bench limits", "Only the voltage and current limits need to be set.")
        limits.setMinimumWidth(305)
        limits.body.setContentsMargins(14, 12, 14, 12)
        form = QFormLayout()
        for key, title in (("max_voltage", "Maximum V"), ("max_current", "Maximum A")):
            self.fields[key] = QLineEdit()
            self.field_labels[key] = title
            form.addRow(title, self.fields[key])
        limits.body.addLayout(form)
        automatic_heading = QLabel("Automatic calibration & tests")
        automatic_heading.setStyleSheet("font-weight: 600;")
        limits.body.addWidget(automatic_heading)
        self.automatic_summary = caption("")
        self.automatic_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.automatic_summary.setAccessibleName("Automatic test settings")
        limits.body.addWidget(self.automatic_summary)
        limits.body.addWidget(caption("Test loads, tolerances and the cutoff ramp are calculated automatically when you save."))
        limits.body.addStretch()
        content.addWidget(limits, 2)
        # Keep the internal field handles used by older callers, without exposing
        # additional setup controls or accepting edits to automatically set values.
        for key in AUTOMATIC_FIELDS:
            edit = QLineEdit(self)
            edit.hide()
            self.fields[key] = edit
        for key, value in RAMP_DEFAULTS.items():
            edit = QLineEdit(str(value), self)
            edit.hide()
            self.ramp_fields[key] = edit
        layout.addLayout(content, 1)

    def _set_status(self, message, state="info"):
        colour = {"info": theme.TEXT2, "draft": theme.WARNING,
                  "ready": theme.SUCCESS, "error": theme.DANGER}[state]
        self.status.setText(message)
        self.status.setStyleSheet(f"color: {colour}; font-weight: 600; padding: 2px 0;")
        self.status.setAccessibleDescription(message)

    def _clear_invalid_field(self):
        if self._invalid_field is not None:
            self._invalid_field.setStyleSheet(self._invalid_field_style)
            self._invalid_field = None

    def _focus_invalid_field(self, edit):
        self._clear_invalid_field()
        self._invalid_field = edit
        self._invalid_field_style = edit.styleSheet()
        edit.setStyleSheet(self._invalid_field_style + f"; border: 2px solid {theme.DANGER};")
        if edit in (self.fields[key] for key in ("cc_current", "end_voltage", "end_current")):
            self.parameter_tabs.setCurrentWidget(self.main_tab)
        elif edit in self.parameters.values():
            key = next(key for key, value in self.parameters.items() if value is edit)
            self.parameter_tabs.setCurrentWidget(self.main_tab if key in PROGRAMMED else self.other_tab)
        edit.setFocus()
        edit.selectAll()

    def _mark_unsaved(self):
        if self._showing_recipe:
            return
        self._clear_invalid_field()
        self._set_status("Unsaved changes. Save variant to keep your edits.", "draft")
        self._update_automatic_summary()

    def _show_automatic_fields(self, recipe):
        for key in AUTOMATIC_FIELDS:
            self.fields[key].setText(f"{getattr(recipe, key):g}")
        for key, edit in self.ramp_fields.items():
            edit.setText(f"{recipe.termination_test[key]:g}")

    def _update_automatic_summary(self):
        try:
            recipe = self._draft_from_inputs(validate_ready=False, require_name=False)
            self._show_automatic_fields(recipe)
            self.automatic_summary.setText(automatic_settings_summary(recipe))
        except (ValueError, TypeError, OverflowError) as exc:
            self.automatic_summary.setText(f"Automatic settings will update after you correct the values. {exc}")

    @staticmethod
    def _readiness_problem(recipe):
        required = (("cc_current", "required full current"), ("end_voltage", "required end voltage"),
                    ("end_current", "required cutoff current"), ("max_voltage", "maximum voltage"),
                    ("max_current", "maximum current"))
        missing = [label for key, label in required if getattr(recipe, key) <= 0]
        if missing:
            return "Complete " + ", ".join(missing) + " with values greater than zero."
        return recipe.validate_production()

    def _record_state(self, record):
        recipe = Recipe.from_dict(record["recipe"])
        problem = self._readiness_problem(recipe)
        ready = not problem and record.get("production_ready") is not False
        return ready, problem or record.get("validation_error", "")

    def _show_record_status(self, record, *, saved=False, blank_fields=False):
        ready, problem = self._record_state(record)
        state = "Ready" if ready else "Draft"
        message = f"{record['recipe']['name']} · r{record['revision']} · {state}"
        message += " saved." if saved else "."
        if problem:
            message += " " + problem
        elif ready:
            message += " Ready for calibration and all three tests."
        else:
            message += " Save after completing the required settings."
        if blank_fields:
            message += " Blank numeric fields were saved as 0."
        self._set_status(message, "ready" if ready else "draft")

    def load(self):
        path = self.data_dir / "profiles.json"
        if path.exists():
            try:
                records = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(records, list):
                    raise ValueError("Recipe file must contain a list.")
                for record in records:
                    recipe = Recipe.from_dict(record["recipe"])
                    if not isinstance(record["recipe"].get("name"), str) or not recipe.name.strip():
                        raise ValueError("A saved variant must have a name.")
                    if not isinstance(recipe.device_settings, dict) or not isinstance(recipe.termination_test, dict):
                        raise ValueError("Saved charger and cutoff settings must be named values.")
                    numbers = [getattr(recipe, key) for key in self.fields
                               if getattr(recipe, key) is not None or key not in
                               ("cc_test_load_voltage", "voltage_test_load_current")]
                    numbers += list(recipe.charger_values().values())
                    numbers += list(recipe.device_settings.values())
                    numbers += [value for key, value in recipe.termination_test.items() if key != "enabled"]
                    if any(type(value) not in (int, float) or not math.isfinite(value) for value in numbers):
                        raise ValueError("Saved numeric settings must be finite numbers.")
                    if type(record.get("revision")) is not int or record["revision"] < 1:
                        raise ValueError("Invalid recipe revision.")
                self.records = records
            except Exception as exc:
                self._load_error = f"Saved recipes could not be read: {exc}. The existing file has been preserved."
        self._populate(0)
        if not self.records:
            self.new_variant()
        if self._load_error:
            self._set_status(self._load_error, "error")

    def _populate(self, index):
        self.selector.blockSignals(True)
        self.selector.clear()
        for record in self.records:
            ready, _ = self._record_state(record)
            suffix = "" if ready else " · Draft"
            self.selector.addItem(f"{record['recipe']['name']} · r{record['revision']}{suffix}")
        self.selector.setCurrentIndex(index)
        self.selector.blockSignals(False)
        if 0 <= index < len(self.records):
            self.select_record(index)
        self.profiles_changed.emit(copy.deepcopy(self.records), max(0, index))

    def select_record(self, index):
        if not 0 <= index < len(self.records):
            return
        recipe = Recipe.from_dict(self.records[index]["recipe"])
        self._show_recipe(recipe)
        self._show_record_status(self.records[index])

    def _show_recipe(self, recipe):
        self._clear_invalid_field()
        self._showing_recipe = True
        try:
            self.name.setText(recipe.name)
            values = dict(recipe.device_settings)
            values.update(recipe.charger_values())
            for key, edit in self.parameters.items():
                edit.setText(f"{values.get(key, 0):g}")
            for key, edit in self.fields.items():
                value = getattr(recipe, key)
                if value is None:
                    value = 49 if key == "cc_test_load_voltage" else 1
                edit.setText(f"{value:g}")
            for key, edit in self.ramp_fields.items():
                edit.setText(str(recipe.termination_test.get(key, RAMP_DEFAULTS[key])))
        finally:
            self._showing_recipe = False
        self._update_automatic_summary()

    def new_variant(self):
        if self._busy:
            return
        self.selector.setCurrentIndex(-1)
        recipe = Recipe(name="")
        self._show_recipe(recipe)
        self._set_status("New variant · Unsaved changes. Enter a name, then save your draft.", "info")
        self.name.setFocus()

    def _number(self, key, edit, *, allow_blank):
        text = edit.text().strip()
        if not text and allow_blank:
            self._blank_fields.append(self.field_labels[key])
            return 0.0
        try:
            value = float(text)
        except ValueError:
            raise RecipeInputError(f"Enter a number for {self.field_labels[key]}.", edit) from None
        if not math.isfinite(value):
            raise RecipeInputError(f"Enter a finite number for {self.field_labels[key]}.", edit)
        return value

    def draft(self, validate_ready=True):
        return self._draft_from_inputs(validate_ready=validate_ready, require_name=True)

    def _draft_from_inputs(self, *, validate_ready, require_name):
        if require_name and not self.name.text().strip():
            raise RecipeInputError("Enter a variant name.", self.name)
        self._blank_fields = []
        values = {key: self._number(key, self.fields[key], allow_blank=not validate_ready)
                  for key in EDITABLE_FIELDS}
        params = {key: self._number(key, edit, allow_blank=not validate_ready)
                  for key, edit in self.parameters.items()}
        index = self.selector.currentIndex()
        base = (Recipe.from_dict(self.records[index]["recipe"]).to_dict()
                if 0 <= index < len(self.records) else Recipe().to_dict())
        base.update(values, name=self.name.text().strip())
        base["device_settings"] = {key: value for key, value in params.items() if key not in PROGRAMMED}
        for key, field in PROGRAMMED.items():
            base[field] = params[key]
        # This release runs the three requested tests; older optional plans are
        # preserved in recipes but are not silently enabled by this editor.
        recipe = apply_automatic_settings(Recipe.from_dict(base))
        if validate_ready:
            problem = self._readiness_problem(recipe)
            if problem:
                raise ValueError(problem)
        return recipe

    def save_variant(self):
        if self._busy:
            return
        try:
            if self._load_error:
                raise ValueError(self._load_error)
            recipe = self.draft(validate_ready=False)
            problem = self._readiness_problem(recipe)
            blank_fields = bool(self._blank_fields)
            records = copy.deepcopy(self.records)
            index = next((i for i, record in enumerate(records)
                          if record["recipe"]["name"].casefold() == recipe.name.casefold()), -1)
            record = dict(revision=records[index]["revision"] + 1 if index >= 0 else 1,
                          recipe=recipe.to_dict(), production_ready=not problem,
                          validation_error=problem)
            if index < 0:
                records.append(record)
                index = len(records) - 1
            else:
                records[index] = record
            atomic_json(self.data_dir / "profiles.json", records)
            self.records = records
            self._populate(index)
            self._show_record_status(record, saved=True, blank_fields=blank_fields)
        except Exception as exc:
            self._set_status(f"Variant not saved. {exc}", "error")
            if isinstance(exc, RecipeInputError):
                self._focus_invalid_field(exc.field)

    def send_parameters(self):
        if self._busy:
            return
        try:
            if self.legacy.live_mode != "manual":
                raise ValueError("Select Live manual control on Manual calibration first.")
            if self.legacy._live_controls_busy() or self.legacy.pending_settings is not None:
                raise ValueError("Wait for the active CAN action to finish.")
            values = {key: float(edit.text()) for key, edit in self.parameters.items()}
            encode_all_config(values)
            self.legacy.set_bench_limits(*(float(self.fields[key].text())
                for key in ("max_voltage", "max_current", "max_power")))
            for key, value in values.items():
                self.legacy.settings[key].setText(f"{value:g}")
            self.legacy._guarded(self.legacy.send_settings)
        except Exception as exc:
            self._set_status(str(exc), "error")

    def copy_live_values(self):
        if self._busy or self.legacy.live_mode == "simulation":
            return
        copied = []
        now = time.monotonic()
        for key, edit in self.parameters.items():
            value = self.legacy._live_observed.get(key)
            stamp = self.legacy._live_observed_at.get(key)
            if (type(value) in (int, float) and math.isfinite(value) and stamp is not None
                    and 0 <= now - stamp <= 10):
                edit.setText(f"{value:g}")
                copied.append(key)
        self._set_status(f"Unsaved changes. Copied {len(copied)} fresh charger values. Save variant to keep these values."
                         if copied else "No fresh charger parameters are available. Start CAN and wait for live readback.",
                         "draft" if copied else "info")

    def set_busy(self, busy):
        self._busy = bool(busy)
        self.setEnabled(not busy)

    def refresh_live(self):
        page = self.legacy
        for key, button in self.can_buttons.items():
            button.setEnabled(not self._busy and page.buttons[key].isEnabled())
        self.close_can.setEnabled(not self._busy and page.close_adapter_button.isEnabled())
        self.shutdown.setEnabled(not self._busy and page.buttons["shutdown"].isEnabled())
        self.send.setEnabled(not self._busy and page.buttons["send_settings"].isEnabled())
        self.copy_live.setEnabled(not self._busy and page.live_mode != "simulation" and page.can_started)
        for field, source in ((self.device, page.device_index), (self.bitrate, page.bitrate)):
            field.blockSignals(True)
            field.setCurrentText(source.currentText())
            field.blockSignals(False)
            field.setEnabled(not page.device_open and not self._busy)
        self.can_status.setText("SIMULATION" if page.live_mode == "simulation" else
                               "CAN running" if page.can_started else "Device open" if page.device_open else "CAN closed")
        for key, label in self.live_values.items():
            value = page._live_observed.get(key)
            stamp = page._live_observed_at.get(key)
            valid = page.live_mode != "simulation" and type(value) in (int, float) and math.isfinite(value) and stamp is not None
            label.setText(f"{value:g}" if valid else "—")
            age = time.monotonic() - stamp if valid else None
            label.setStyleSheet(f"color: {theme.TEXT if valid and age <= 10 else theme.MUTED};")
            label.setToolTip(f"Received {age:.1f} seconds ago." if valid else "No actual charger readback received.")
        if page.event_log:
            self.command_status.setText(page.event_log[-1])

    def dispose(self):
        self._timer.stop()
