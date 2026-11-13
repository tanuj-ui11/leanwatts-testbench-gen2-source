import unittest
from leanwatts_can.core import Recipe
from leanwatts_can.recipe_defaults import apply_automatic_settings, automatic_settings_summary
from leanwatts_can.tests.test_session_ui_v8 import profile


class AutomaticRecipeTests(unittest.TestCase):
    def test_known_variant_derives_48_49_and_cutoff_without_changing_targets(self):
        original = profile()
        before = original.to_dict()
        recipe = apply_automatic_settings(original)
        self.assertEqual(original.to_dict(), before)
        self.assertEqual(recipe.charger_values(), original.charger_values())
        self.assertEqual((recipe.cc_current, recipe.end_voltage, recipe.end_current), (15, 58.4, 1.1))
        self.assertEqual((recipe.current_cal_load_voltage, recipe.test_cv_voltage), (48, 49))
        self.assertEqual(recipe.test_cc_current, 2)
        self.assertEqual(recipe.termination_test['start_current'], 5)
        self.assertEqual(recipe.termination_test['step_current'], .1)
        self.assertEqual(recipe.max_power, 1300)
        self.assertEqual(recipe.validate_production(), '')

    def test_small_charger_has_valid_test_points_below_full_current(self):
        recipe = profile()
        recipe.cc_current = recipe.programmed_cc_current = 1
        recipe.end_voltage = recipe.programmed_end_voltage = 12
        recipe.end_current = recipe.programmed_end_current = .2
        recipe.max_voltage, recipe.max_current = 15, 2
        recipe.device_settings['ratedVoltage'] = 12
        recipe = apply_automatic_settings(recipe)
        self.assertGreater(recipe.test_cc_current, recipe.end_current + recipe.end_current_tolerance)
        self.assertLess(recipe.test_cc_current, recipe.cc_current)
        self.assertEqual(recipe.validate_production(), '')

    def test_limits_not_increased_and_power_capped_by_load(self):
        recipe = profile()
        recipe.max_voltage, recipe.max_current = 500, 120
        self.assertEqual(apply_automatic_settings(recipe).max_power, 3700)
        recipe.max_voltage, recipe.max_current = 50, 10
        derived = apply_automatic_settings(recipe)
        self.assertEqual((derived.max_voltage, derived.max_current, derived.max_power), (50, 10, 500))
        self.assertTrue(derived.validate_production())

    def test_incomplete_and_impossible_targets_stay_invalid(self):
        recipe = apply_automatic_settings(Recipe(name='Draft'))
        self.assertTrue(recipe.validate_production())
        self.assertIn('Enter the main', automatic_settings_summary(recipe))
        recipe = profile()
        recipe.end_current = recipe.cc_current
        self.assertTrue(apply_automatic_settings(recipe).validate_production())

    def test_programmed_cutoff_also_sets_test_floor(self):
        recipe = profile()
        recipe.programmed_end_current = 6
        derived = apply_automatic_settings(recipe)
        self.assertGreater(derived.test_cc_current, 6)
        self.assertGreater(derived.termination_test['start_current'], 6)
        self.assertEqual(derived.validate_production(), '')


if __name__ == '__main__':
    unittest.main()
