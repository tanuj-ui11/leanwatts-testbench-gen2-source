"""Recipe storage and visible save feedback, using widget stubs without hardware."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QComboBox, QPushButton

from leanwatts_can.core import Recipe
from leanwatts_can.protocol import CONFIG
from leanwatts_can.recipe_page import RecipePage, RAMP_DEFAULTS, AUTOMATIC_FIELDS, EDITABLE_FIELDS
from leanwatts_can.session_page import SessionPage
from leanwatts_can.session_store import SessionStore


def ready_recipe(name="Gen2ex"):
    recipe = Recipe(name=name, cc_current=15, end_voltage=58.4, end_current=1.1,
        current_tolerance=.1, voltage_tolerance=.1, end_current_tolerance=.1,
        max_voltage=65, max_current=20, max_power=1300,
        programmed_cc_current=15, programmed_end_voltage=58.4, programmed_end_current=1.5,
        cc_test_load_voltage=49, voltage_test_load_current=2,
        termination_test={"enabled": True, **RAMP_DEFAULTS})
    recipe.device_settings = {key: 0 for key in CONFIG if key not in recipe.charger_values()}
    return recipe


def legacy_stub():
    return SimpleNamespace(
        device_index=QComboBox(), bitrate=QComboBox(),
        buttons={key: QPushButton(key) for key in
                 ("open_device", "start_can", "reset_can", "shutdown", "send_settings")},
        close_adapter_button=QPushButton(), live_mode="simulation", device_open=False,
        can_started=False, _live_observed={}, _live_observed_at={}, event_log=[])


class RecipeSaveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.pages = []
        self.page = self.make_page()
        self.path = self.data / "profiles.json"

    def make_page(self):
        page = RecipePage(self.data, legacy_stub())
        page.dispose()
        page.resize(1230, 630)
        page.load()
        self.pages.append(page)
        page.show()
        self.app.processEvents()
        return page

    def tearDown(self):
        for page in self.pages:
            page.dispose()
            page.close()
            page.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def save(self):
        self.page.save_button.click()
        self.app.processEvents()

    def stored(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_save_button_keeps_zero_and_blank_settings_as_draft_and_reopens(self):
        self.page.name.setText("Gen2ex")
        self.page.fields["max_current"].clear()
        self.save()
        record = self.stored()[0]
        self.assertFalse(record["production_ready"])
        self.assertEqual(record["revision"], 1)
        self.assertEqual(record["recipe"]["max_power"], 0)
        self.assertEqual(record["recipe"]["programmed_cc_current"], 0)
        self.assertIn("required full current", record["validation_error"])
        self.assertIn("maximum voltage", record["validation_error"])
        self.assertIn("Gen2ex · r1 · Draft saved", self.page.status.text())
        self.assertIn("Blank numeric fields were saved as 0", self.page.status.text())
        self.assertIn("Draft", self.page.selector.currentText())
        reopened = self.make_page()
        self.assertEqual(reopened.records, [record])
        self.assertEqual(reopened.name.text(), "Gen2ex")
        self.assertEqual(reopened.fields["max_power"].text(), "0")
        self.assertIn("Draft", reopened.status.text())

    def test_ready_save_and_later_edits_replace_success_feedback(self):
        self.page._show_recipe(ready_recipe())
        self.save()
        record = self.stored()[0]
        self.assertTrue(record["production_ready"])
        self.assertEqual(record["validation_error"], "")
        self.assertIn("Gen2ex · r1 · Ready saved", self.page.status.text())
        self.page.fields["max_current"].setText("21")
        self.assertIn("Unsaved changes", self.page.status.text())
        self.assertNotIn("Ready saved", self.page.status.text())
        self.page.select_record(0)
        self.assertNotIn("Unsaved", self.page.status.text())
        self.assertIn("Ready", self.page.status.text())
        self.page.new_variant()
        self.assertIn("New variant", self.page.status.text())
        self.assertNotIn("Ready", self.page.status.text())

    def test_completing_draft_marks_same_variant_ready_at_next_revision(self):
        self.page.name.setText("Gen2ex")
        self.save()
        self.page._show_recipe(ready_recipe())
        self.save()
        records = self.stored()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["revision"], 2)
        self.assertTrue(records[0]["production_ready"])
        self.assertNotIn("Draft", self.page.selector.currentText())

    def test_old_voltage_current_is_replaced_automatically_without_changing_targets(self):
        recipe = ready_recipe()
        recipe.voltage_test_load_current = 1
        self.page._show_recipe(recipe)
        self.save()
        record = self.stored()[0]
        self.assertTrue(record["production_ready"])
        self.assertEqual(record["recipe"]["voltage_test_load_current"], 2)
        self.assertEqual(record["recipe"]["end_current"], 1.1)
        self.assertEqual(record["recipe"]["programmed_end_current"], 1.5)
        self.assertEqual(self.page.draft().validate_production(), "")

    def test_unencodable_finite_charger_values_are_drafts(self):
        self.page._show_recipe(ready_recipe())
        self.page.parameters["batteryType"].setText("0.5")
        self.save()
        record = self.stored()[0]
        self.assertFalse(record["production_ready"])
        self.assertEqual(record["recipe"]["device_settings"]["batteryType"], .5)
        self.assertIn("batteryType", record["validation_error"])

    def test_invalid_text_and_nonfinite_values_do_not_overwrite_and_focus_field(self):
        self.page._show_recipe(ready_recipe())
        self.save()
        previous = self.path.read_bytes()
        old_records = copy.deepcopy(self.page.records)
        for text in ("not a number", "NaN", "inf", "-inf"):
            with self.subTest(text=text):
                self.page.fields["max_current"].setText(text)
                self.save()
                self.assertEqual(self.path.read_bytes(), previous)
                self.assertEqual(self.page.records, old_records)
                self.assertIn("Variant not saved", self.page.status.text())
                self.assertIn("Maximum A", self.page.status.text())
                self.assertIs(self.app.focusWidget(), self.page.fields["max_current"])
        self.page.fields["max_current"].setText("20")
        self.assertIn("Unsaved changes", self.page.status.text())
        self.assertIsNone(self.page._invalid_field)

    def test_hidden_automatic_fields_cannot_override_generated_settings(self):
        self.page._show_recipe(ready_recipe())
        self.page.ramp_fields["step_current"].setText("NaN")
        self.page.fields["voltage_test_load_current"].setText("0")
        self.page.fields["max_power"].setText("99999")
        self.save()
        record = self.stored()[0]
        self.assertTrue(record["production_ready"])
        self.assertEqual(record["recipe"]["voltage_test_load_current"], 2)
        self.assertEqual(record["recipe"]["max_power"], 1300)
        self.assertEqual(record["recipe"]["termination_test"]["step_current"], .1)

    def test_invalid_charger_parameter_reveals_the_corresponding_tab(self):
        self.page._show_recipe(ready_recipe())
        self.page.parameters["ratedVoltage"].setText("NaN")
        self.page.parameter_tabs.setCurrentWidget(self.page.main_tab)
        self.save()
        self.assertFalse(self.path.exists())
        self.assertIs(self.page.parameter_tabs.currentWidget(), self.page.other_tab)
        self.assertIs(self.app.focusWidget(), self.page.parameters["ratedVoltage"])
        self.assertIn("Rated voltage", self.page.status.text())
        self.page.parameters["ratedVoltage"].setText("48")
        self.page.fields["cc_current"].setText("NaN")
        self.save()
        self.assertIs(self.page.parameter_tabs.currentWidget(), self.page.main_tab)
        self.assertIs(self.app.focusWidget(), self.page.fields["cc_current"])

    def test_main_view_only_exposes_main_values_and_two_limits_with_live_summary(self):
        self.page._show_recipe(ready_recipe())
        self.app.processEvents()
        self.assertEqual({key for key, edit in self.page.fields.items() if edit.isVisible()}, set(EDITABLE_FIELDS))
        self.assertEqual({key for key, edit in self.page.parameters.items() if edit.isVisible()},
                         {"ccCurrent", "endVoltage", "endCurrent"})
        self.assertTrue(all(not self.page.fields[key].isVisible() for key in AUTOMATIC_FIELDS))
        self.assertTrue(all(not edit.isVisible() for edit in self.page.ramp_fields.values()))
        first_summary = self.page.automatic_summary.text()
        self.page.fields["end_current"].setText("2.1")
        self.assertNotEqual(first_summary, self.page.automatic_summary.text())
        self.assertIn("3 A", self.page.automatic_summary.text())
        self.assertEqual(self.page.fields["voltage_test_load_current"].text(), "3")
        self.assertFalse(self.path.exists())

    def test_name_is_required(self):
        self.save()
        self.assertFalse(self.path.exists())
        self.assertIn("Enter a variant name", self.page.status.text())
        self.assertIs(self.app.focusWidget(), self.page.name)

    def test_failed_disk_replace_keeps_prior_recipe_and_reports_failure(self):
        self.page._show_recipe(ready_recipe())
        self.save()
        previous = self.path.read_bytes()
        old_records = copy.deepcopy(self.page.records)
        self.page.fields["max_current"].setText("21")
        with patch("leanwatts_can.recipe_page.os.replace", side_effect=OSError("Disk unavailable")):
            self.save()
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(self.page.records, old_records)
        self.assertEqual(self.page.fields["max_current"].text(), "21")
        self.assertIn("Variant not saved. Disk unavailable", self.page.status.text())

    def test_corrupted_recipe_file_is_preserved(self):
        incomplete = ready_recipe().to_dict()
        incomplete["cc_current"] = None
        for broken in ("{broken JSON", '[{"recipe": {}, "revision": 1}]',
                       json.dumps([dict(recipe=incomplete, revision=1)])):
            with self.subTest(broken=broken):
                self.path.write_text(broken, encoding="utf-8")
                self.page.load()
                self.page.name.setText("Recovery draft")
                self.save()
                self.assertEqual(self.path.read_text(encoding="utf-8"), broken)
                self.assertEqual(self.page.records, [])
                self.assertIn("Variant not saved", self.page.status.text())
                self.assertIn("existing file has been preserved", self.page.status.text())

    def test_saved_draft_cannot_start_authenticated_live_or_simulation_session(self):
        self.page.name.setText("Gen2ex")
        self.save()
        store = SessionStore(self.data)
        session = SessionPage(self.data, legacy_stub(), store)
        session.timer.stop()
        self.pages.append(session)
        session.accounts.upsert_operator("OP", "Test operator", "password", role="admin")
        operator = session.accounts.authenticate("OP", "password")
        session._authenticated_operator = operator
        session._session = store.start(operator)
        session.set_profiles(self.stored())
        session.serial.setText("TEST-UNIT")
        factory = Mock(side_effect=AssertionError("No instrument factory may be called"))
        session.live_run_factory = factory
        session.live_readiness = lambda process: ""
        for mode in ("manual", "simulation"):
            with self.subTest(mode=mode):
                session.set_mode(mode)
                session.start()
                self.assertFalse(session.busy)
                self.assertIn("Draft", session.detail.text())
                self.assertEqual(store.active()["units"], [])
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
