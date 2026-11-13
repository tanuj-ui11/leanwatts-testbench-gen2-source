"""Operator-supplied programs stay data-only and freeze the selected run input."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from leanwatts_can.test_programs import (DEFAULT_FILENAME, MAX_PROGRAM_BYTES, ensure_programs,
                                       list_programs, load_program, validate_program)


class TestProgramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = ensure_programs(self.temp.name)
        self.path = self.folder / DEFAULT_FILENAME
        self.value = json.loads(self.path.read_text(encoding="utf-8"))

    def test_default_is_external_restart_full_cycle_without_test_writes(self):
        entry = load_program(self.path)
        self.assertEqual(entry["program"]["workflow"], "full_cycle")
        self.assertEqual(entry["program"]["testing"]["start_method"], "external_restart")
        self.assertIsNone(entry["program"]["baseline"]["engage_voltage"])
        self.assertIn("no charger commands", entry["program"]["description"].lower())
        self.assertEqual(entry["sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_edit_and_add_file_without_overwriting_station_files(self):
        self.value["testing"]["restart_timeout_seconds"] = 130
        self.path.write_text(json.dumps(self.value), encoding="utf-8")
        custom = self.folder / "04-operator-program.json"
        self.value["workflow"] = "test_only"
        custom.write_text(json.dumps(self.value), encoding="utf-8")
        ensure_programs(self.folder)
        entries = list_programs(self.folder)
        self.assertEqual([item["name"] for item in entries], [DEFAULT_FILENAME, custom.name])
        self.assertEqual(entries[0]["program"]["testing"]["restart_timeout_seconds"], 130)
        self.assertEqual(entries[1]["program"]["workflow"], "test_only")

    def test_reject_executable_or_unknown_fields_at_every_level(self):
        for section in (None, "testing", "baseline"):
            value = copy.deepcopy(self.value)
            (value if section is None else value[section])["code"] = "import os; run()"
            with self.subTest(section=section), self.assertRaisesRegex(ValueError, "Unknown"):
                validate_program(value)

    def test_reject_nonfinite_booleans_and_unbounded_values(self):
        for field, invalids in (("control_hold_seconds", [False, 2, 121, float("nan"), "30"]),
                               ("cutoff_step_a", [True, 0, .51, float("inf")]),
                               ("cutoff_dwell_seconds", [0, 31, None])):
            for invalid in invalids:
                value = copy.deepcopy(self.value)
                value["testing"][field] = invalid
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    validate_program(value)
        for field, invalid in (("schema", True), ("workflow", [])):
            value = copy.deepcopy(self.value)
            value[field] = invalid
            with self.assertRaises(ValueError):
                validate_program(value)
        for field, invalid in (("ramp_timeout_seconds", 14), ("ramp_timeout_seconds", 181),
                               ("settle_seconds", .5), ("settle_seconds", 11)):
            value = copy.deepcopy(self.value)
            value["baseline"][field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                validate_program(value)

    def test_bad_program_remains_visible_and_does_not_hide_good_program(self):
        broken = self.folder / "bad.json"
        broken.write_text('{"schema": 1, "schema": 1}', encoding="utf-8")
        entries = list_programs(self.folder)
        self.assertEqual(len(entries), 2)
        self.assertFalse(entries[0]["error"])
        self.assertIn("Duplicate", entries[1]["error"])
        self.assertIsNone(entries[1]["program"])

    def test_size_limit_and_utf8_and_nonfinite_json(self):
        for raw in (b" " * (MAX_PROGRAM_BYTES + 1), b"\xff", b'{"schema": NaN}'):
            self.path.write_bytes(raw)
            with self.assertRaises(ValueError):
                load_program(self.path)

    def test_snapshot_preserves_source_and_hash_even_with_bom(self):
        raw = b"\xef\xbb\xbf" + self.path.read_bytes()
        self.path.write_bytes(raw)
        entry = load_program(self.path)
        self.path.write_text("{}", encoding="utf-8")
        self.assertEqual(entry["source"].encode("utf-8"), raw)
        self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(entry["program"]["schema"], 1)


if __name__ == "__main__":
    unittest.main()
