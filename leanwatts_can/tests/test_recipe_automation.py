"""Required/programmed separation and approved DC-load test profiles."""
import json
import unittest
from leanwatts_can.core import Recipe
from leanwatts_can.tests.test_automation import recipe


def ramp_recipe():
    profile = recipe()
    profile.end_current = 1.1
    profile.end_current_tolerance = .1
    profile.programmed_end_current = 1.5
    profile.cc_test_load_voltage = 49
    profile.voltage_test_load_current = 5
    profile.termination_test = dict(enabled=True, start_current=1.5, minimum_current=.8,
                                    step_current=.05, dwell_seconds=1,
                                    off_voltage=5, off_current=.1,
                                    confirm_seconds=.5, max_seconds=30)
    return profile


class RecipeAutomationTests(unittest.TestCase):
    def test_old_profiles_load_without_enabling_new_tests(self):
        profile = Recipe.from_dict(recipe().to_dict())
        self.assertEqual(profile.validate(), "")
        self.assertEqual(profile.charger_values(), {"ccCurrent":15,"endVoltage":58.4,"endCurrent":.5})
        self.assertEqual(profile.test_cv_voltage, 48)
        self.assertEqual(profile.test_cc_current, 1)
        self.assertFalse(profile.termination_test["enabled"])
        self.assertEqual(profile.test_plan, {})

    def test_programmed_compensation_does_not_change_required_target(self):
        profile = ramp_recipe()
        self.assertEqual(profile.validate(), "")
        self.assertEqual(profile.end_current, 1.1)
        self.assertEqual(profile.charger_values()["endCurrent"], 1.5)
        self.assertEqual(profile.current_cal_load_voltage, 48)
        self.assertEqual(profile.test_cv_voltage, 49)
        copied = Recipe.from_dict(json.loads(json.dumps(profile.to_dict())))
        self.assertEqual(copied.to_dict(), profile.to_dict())
        copied.termination_test["start_current"] = 5
        self.assertEqual(profile.termination_test["start_current"], 1.5)

    def test_invalid_programmed_values_are_rejected(self):
        for key,value in (("programmed_end_current", 1.15), ("programmed_cc_current", 22),
                          ("programmed_end_voltage", 70), ("programmed_end_current", 15),
                          ("programmed_cc_current", float("nan"))):
            profile = ramp_recipe()
            setattr(profile,key,value)
            self.assertTrue(profile.validate(),key)

    def test_cutoff_procedure_requires_complete_bounded_values(self):
        for key,value in (("start_current",1), ("minimum_current",1.05), ("step_current",5),
                          ("dwell_seconds",0), ("off_current",1), ("off_voltage",60),
                          ("confirm_seconds",float("nan")), ("max_seconds",1), ("enabled","yes")):
            profile = ramp_recipe()
            profile.termination_test[key]=value
            self.assertTrue(profile.validate(),key)
        profile = ramp_recipe()
        del profile.termination_test["off_voltage"]
        self.assertTrue(profile.validate())

    def test_optional_points_and_soak_validate_without_enabling_by_default(self):
        profile = ramp_recipe()
        profile.test_plan = dict(voltage_regulation_currents=[3,5,10], current_regulation_voltages=[49,50],
                                 soak_seconds=20, soak_current=5)
        self.assertEqual(profile.validate(), "")
        for plan in (dict(voltage_regulation_currents=[0]), dict(current_regulation_voltages=[60]),
                     dict(soak_seconds=4000,soak_current=5), dict(soak_seconds=10),
                     dict(soak_seconds=10,soak_current=20), dict(ripple=True)):
            profile.test_plan=plan
            self.assertTrue(profile.validate(),plan)


if __name__ == "__main__":
    unittest.main()
