"""Evidence export must preserve failures and never claim a missing ZIP exists."""
import csv
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from leanwatts_can.run_bundle import RunBundle


class RunBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def make(self, serial="WO-S00430-1-001"):
        program = dict(schema=1, name="test", workflow="test_only")
        source = json.dumps(program)
        payload = dict(program_source=source, program_sha256=hashlib.sha256(source.encode()).hexdigest(),
                       serial=serial, serial_override=serial.startswith("="),
                       program_path="01-program.json", program=program)
        bundle = RunBundle(self.temp.name, payload, SimpleNamespace(to_dict=lambda: {"name": "fixture"}),
                           1, {"id": "operator"})
        self.addCleanup(lambda: None if bundle._closed else (bundle._events.close(), bundle._csv.close()))
        return bundle

    def test_failed_zip_export_is_retryable_without_false_success(self):
        bundle = self.make()
        result = dict(outcome="INCOMPLETE", detail="diagnostic", stage_details=[])
        original = zipfile.ZipFile.write
        calls = []
        def fail_once(archive, *args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise OSError("simulated storage interruption")
            return original(archive, *args, **kwargs)
        with patch.object(zipfile.ZipFile, "write", fail_once):
            with self.assertRaisesRegex(OSError, "storage interruption"):
                bundle.finish(result)
            self.assertTrue(bundle._closed)
            self.assertIsNone(bundle._exported)
            self.assertFalse(bundle.folder.with_suffix(".zip").exists())
            self.assertTrue((bundle.folder / "can-events.jsonl").exists())
            destination = bundle.finish(result)
        self.assertTrue(destination.is_file())
        self.assertFalse(bundle.folder.with_suffix(".zip.partial").exists())
        with zipfile.ZipFile(destination) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("can-trace.csv", archive.namelist())
        self.assertEqual(bundle.finish(result), destination)

    def test_csv_preserves_capture_id_and_formula_protects_summary_serial(self):
        bundle = self.make('=HYPERLINK("https://example.invalid")')
        for capture in ("calibration-capture", "test-capture"):
            bundle.append(dict(capture_id=capture, event_id=1, direction="TX", id=0x1806E550,
                               dlc=4, data=[0, 0x14, 0, 1], outcome="ATTEMPT"))
            bundle.append(dict(capture_id=capture, event_id=2, correlates_to=1,
                               direction="TX_RESULT", id=0x1806E550, dlc=4,
                               data=[0, 0x14, 0, 1], outcome="API ACCEPTED"))
        bundle.finish(dict(outcome="INCOMPLETE", stage_details=[]))
        with (bundle.folder / "can-trace.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([row["capture_id"] for row in rows],
                         ["calibration-capture"] * 2 + ["test-capture"] * 2)
        self.assertEqual(rows[1]["correlates_to"], rows[0]["event_id"])
        with (bundle.folder / "summary.csv").open(encoding="utf-8-sig", newline="") as stream:
            serial_row = next(csv.reader(stream))
        self.assertTrue(serial_row[1].startswith("'="))


if __name__ == "__main__":
    unittest.main()
