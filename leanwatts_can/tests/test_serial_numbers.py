"""Serial syntax validation is pure and never changes scanned identifiers."""
import unittest

from leanwatts_can.serial_numbers import validate_serial


class SerialNumberTests(unittest.TestCase):
    def test_both_standard_formats_and_outer_spaces(self):
        for serial in ("WO-S00430-1-001", "WO-S00261-BS1-019", " WO-S00261-BS12-999 ",
                       "WO-S00000-123-000"):
            with self.subTest(serial=serial):
                self.assertEqual(validate_serial(serial), "")

    def test_standard_is_exact_uppercase_ascii_structure(self):
        for serial in ("wo-s00430-1-001", "WO-S0430-1-001", "WO-S000430-1-001", "WO-S00430-1-01",
                       "WO-S00430-1-0001", "WO-S00430-BS-001", "WO-S00430-X1-001", "WO-S00430- 1-001",
                       "WO-S００４３０-1-001", "WO-S00430-Bs1-001", "PREFIX-WO-S00430-1-001",
                       "WO-S00430-1-001-SUFFIX", "UNIT-1"):
            with self.subTest(serial=serial):
                self.assertIn("Other number", validate_serial(serial))

    def test_custom_mode_is_explicit_and_does_not_rewrite_number(self):
        serial = "Factory charger 007"
        self.assertTrue(validate_serial(serial))
        self.assertEqual(validate_serial(serial, allow_custom=True), "")
        self.assertEqual(serial, "Factory charger 007")
        self.assertTrue(validate_serial(serial, allow_custom=False))

    def test_empty_and_length_limit_apply_to_both_modes(self):
        for custom in (False, True):
            for serial in ("", "   ", "A" * 161):
                with self.subTest(custom=custom, serial=serial):
                    self.assertTrue(validate_serial(serial, allow_custom=custom))
        self.assertEqual(validate_serial("A" * 160, allow_custom=True), "")

    def test_ascii_controls_rejected_even_at_edges_in_custom_mode(self):
        for control in [chr(n) for n in range(32)] + [chr(127)]:
            for serial in (control + "WO-S00430-1-001", "WO-S00430-1-001" + control,
                           "CUSTOM" + control + "NUMBER"):
                with self.subTest(control=ord(control), serial=serial):
                    self.assertTrue(validate_serial(serial))
                    self.assertTrue(validate_serial(serial, allow_custom=True))

    def test_nontext_and_implicit_override_are_not_accepted(self):
        for value in (None, 123, [], True):
            self.assertTrue(validate_serial(value, allow_custom=True))
        for override in (1, "yes", None):
            self.assertTrue(validate_serial("CUSTOM", allow_custom=override))


if __name__ == "__main__":
    unittest.main()
