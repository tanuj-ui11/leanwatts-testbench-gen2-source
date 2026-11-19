"""Importing a recipe preset file fills an unsaved draft; nothing is saved or sent to hardware."""
import json
import tempfile
import unittest
from pathlib import Path

from leanwatts_can.recipe_preset import PresetError, load_preset
from leanwatts_can.tests.test_recipe_save import RecipeSaveTests as Base, ready_recipe


class PresetFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_json_recipe_and_profile_record_and_single_item_list(self):
        recipe = ready_recipe("Imported").to_dict()
        for text in (json.dumps(recipe), json.dumps({"recipe": recipe, "revision": 3}), json.dumps([recipe])):
            loaded, ignored = load_preset(self.write("p.json", text))
            self.assertEqual((loaded.name, loaded.cc_current, loaded.end_voltage), ("Imported", 15, 58.4))
            self.assertEqual(loaded.programmed_end_current, 1.5)
            self.assertTrue(loaded.termination_test["enabled"])

    def test_two_column_csv_with_comments_and_header(self):
        path = self.write("p.csv", "field,value\n# comment\nname,From sheet\nccCurrent,12.5\nendVoltage,58.4\n"
                                   "endCurrent,1.2\nmax_voltage,65\nmax_current,20\nratedVoltage,48\nstart_current,5\n")
        recipe, ignored = load_preset(path)
        self.assertEqual((recipe.name, recipe.cc_current, recipe.programmed_cc_current), ("From sheet", 12.5, 12.5))
        self.assertEqual(recipe.device_settings, {"ratedVoltage": 48.0})
        self.assertEqual(recipe.termination_test["start_current"], 5.0)
        self.assertEqual(ignored, [])

    def test_bad_files_are_refused_with_plain_messages(self):
        bad = {
            "empty.json": "",
            "text.json": "not json",
            "many.json": json.dumps([{"cc_current": 1}, {"cc_current": 2}]),
            "nan.json": '{"cc_current": NaN}',
            "bool.json": '{"cc_current": true}',
            "str.json": '{"cc_current": "abc"}',
            "none.json": '{"colour": "red"}',
            "dev.json": '{"cc_current": 1, "device_settings": [1]}',
            "dup.csv": "ccCurrent,1\nccCurrent,2\n",
            "one.csv": "ccCurrent\n",
        }
        for name, text in bad.items():
            with self.assertRaises(PresetError, msg=name):
                load_preset(self.write(name, text))
        with self.assertRaises(PresetError):
            load_preset(self.dir / "missing.json")
        with self.assertRaises(PresetError):
            load_preset(self.write("big.json", " " * 300000))

    def test_unknown_keys_are_reported_not_applied(self):
        recipe, ignored = load_preset(self.write("p.json", '{"cc_current": 5, "current_tolerance": 9, "x": 1}'))
        self.assertEqual(ignored, ["current_tolerance", "x"])
        self.assertEqual(recipe.current_tolerance, 0.0)


class PresetImportPageTests(Base):
    def test_import_fills_an_unsaved_new_variant_and_saving_stores_it(self):
        recipe = ready_recipe("Imported").to_dict()
        path = self.data / "preset.json"
        path.write_text(json.dumps(recipe), encoding="utf-8")
        before = self.path.exists()
        self.page.import_preset(path)
        self.assertEqual(self.page.selector.currentIndex(), -1)
        self.assertEqual(self.page.name.text(), "Imported")
        self.assertEqual(self.page.fields["cc_current"].text(), "15")
        self.assertIn("Unsaved", self.page.status.text())
        self.assertEqual(self.path.exists(), before)  # nothing written yet
        self.save()
        self.assertEqual([r["recipe"]["name"] for r in self.stored()], ["Imported"])

    def test_bad_preset_leaves_the_editor_untouched(self):
        self.page.name.setText("Keep me")
        path = self.data / "bad.json"
        path.write_text("nope", encoding="utf-8")
        self.page.import_preset(path)
        self.assertEqual(self.page.name.text(), "Keep me")
        self.assertIn("Preset not imported", self.page.status.text())

    def test_same_name_warns_about_replacement(self):
        self.page.name.setText("Gen2ex")
        self.page.save_button.click()
        path = self.data / "preset.json"
        path.write_text(json.dumps(ready_recipe("gen2ex").to_dict()), encoding="utf-8")
        self.page.import_preset(path)
        self.assertIn("will replace it", self.page.status.text())


del Base
