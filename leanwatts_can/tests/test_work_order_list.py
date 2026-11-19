"""Work-order list parsing, display entries and the offline cache. Pure; no Qt, devices or network."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from leanwatts_can import work_order_list as wol

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def row(number="WO-S00410", state="open", untested=50, total=50, customer="RTCXFC INDUSTRIES PRIVATE LIMITED.", signoff=None, **extra):
    value = {"wo_number": number, "order": number[3:], "customer": customer, "total": total, "tested": total - untested,
             "passed": total - untested, "failed": 0, "untested": untested, "last_report_at": None, "state": state,
             "signoff": signoff}
    value.update(extra)
    return value


def signed(days_ago, operator="Asha (1042)", note="done"):
    return {"signed_off_at": (NOW - timedelta(days=days_ago)).isoformat(), "operator": operator, "note": note,
            "untested_count": 3, "total_units": 50, "station": "Bench 1"}


def payload(*rows):
    return {"generated_at": NOW.isoformat(), "work_orders": list(rows)}


class ParseTests(unittest.TestCase):
    def test_valid_rows_parse(self):
        rows, skipped, generated = wol.parse_work_orders(payload(row(), row("WO-S00411", "signed_off", signoff=signed(2))))
        self.assertEqual([r.wo_number for r in rows], ["WO-S00410", "WO-S00411"])
        self.assertEqual(skipped, 0)
        self.assertEqual(generated, NOW.isoformat())
        self.assertEqual(rows[1].signoff["operator"], "Asha (1042)")

    def test_malformed_rows_are_skipped_and_counted(self):
        bad = [row(untested=-1), row(state="weird"), row(number="bad number!"), row(total=True), "text", None,
               row(untested=60, total=50), row("WO-S00500", "signed_off", signoff=None),
               row("WO-S00501", signoff={"signed_off_at": "yesterday"}), dict(row("WO-S00502"), total="5")]
        rows, skipped, _ = wol.parse_work_orders(payload(row("WO-GOOD"), *bad))
        self.assertEqual([r.wo_number for r in rows], ["WO-GOOD"])
        self.assertEqual(skipped, len(bad))

    def test_duplicates_keep_the_first_and_size_is_capped(self):
        rows, skipped, _ = wol.parse_work_orders(payload(row("WO-A1"), row("wo-a1")))
        self.assertEqual((len(rows), skipped), (1, 1))
        many = [row(f"WO-{i}") for i in range(wol.MAX_ROWS + 25)]
        rows, skipped, _ = wol.parse_work_orders(payload(*many))
        self.assertEqual((len(rows), skipped), (wol.MAX_ROWS, 25))

    def test_wrong_payload_shape_raises(self):
        for value in (None, [], {"work_orders": "x"}, {}):
            with self.assertRaises(ValueError):
                wol.parse_work_orders(value)

    def test_text_is_cleaned_and_capped(self):
        rows, _, _ = wol.parse_work_orders(payload(row(customer="A\x1b[31m  B" + "x" * 500)))
        self.assertNotIn("\x1b", rows[0].customer)
        self.assertLessEqual(len(rows[0].customer), wol.MAX_TEXT)

    def test_signoff_response(self):
        already, signoff = wol.parse_signoff_response({"wo_number": "WO-S00410", "already_signed_off": True, "signoff": signed(1)})
        self.assertTrue(already)
        self.assertEqual(signoff["operator"], "Asha (1042)")
        for bad in (None, {"already_signed_off": "yes", "signoff": signed(1)}, {"already_signed_off": False, "signoff": None},
                    {"already_signed_off": False, "signoff": {"signed_off_at": "no"}}):
            with self.assertRaises(ValueError):
                wol.parse_signoff_response(bad)

    def test_valid_reference(self):
        for good in ("WO-S00410", " WO-S00410 ", "S00410"):
            self.assertTrue(wol.valid_reference(good), good)
        for bad in ("", "  ", "WO S1", "a/b", "x" * 41, None):
            self.assertFalse(wol.valid_reference(bad), bad)


class EntryTests(unittest.TestCase):
    def build(self, *rows):
        parsed, _, _ = wol.parse_work_orders(payload(*rows))
        return parsed, wol.build_entries(parsed, NOW)

    def test_open_entry_text_and_customer_shortening(self):
        _, entries = self.build(row())
        self.assertEqual(entries[0].text, "WO-S00410 · RTCXFC INDUSTRIES… · 50 untested")
        self.assertEqual(entries[0].wo_number, "WO-S00410")

    def test_short_customer_not_shortened_and_all_tested(self):
        _, entries = self.build(row(customer="ACME", untested=0))
        self.assertEqual(entries[0].text, "WO-S00410 · ACME · all tested")

    def test_signed_off_shows_days_left_and_order_is_open_signed_past(self):
        _, entries = self.build(row("WO-P", "past", signoff=signed(20)), row("WO-S", "signed_off", signoff=signed(2)),
                                row("WO-O"))
        kinds = [(e.kind, e.wo_number) for e in entries]
        self.assertEqual(kinds, [("work_order", "WO-O"), ("work_order", "WO-S"), ("heading", ""), ("work_order", "WO-P")])
        self.assertIn("12 days left", entries[1].text)
        self.assertEqual(entries[2].text, wol.PAST_HEADING)

    def test_days_left_edges(self):
        parsed, _ = self.build(row("WO-S", "signed_off", signoff=signed(13)))
        self.assertEqual(wol.days_left(parsed[0], NOW), 1)
        self.assertIn("1 day left", wol.display_text(parsed[0], NOW))
        self.assertEqual(wol.days_left(parsed[0], NOW + timedelta(days=5)), 0)
        self.assertIn("ends today", wol.display_text(parsed[0], NOW + timedelta(days=5)))

    def test_no_past_heading_without_past_rows(self):
        _, entries = self.build(row(), row("WO-S", "signed_off", signoff=signed(1)))
        self.assertFalse([e for e in entries if e.kind == "heading"])
        self.assertEqual(wol.build_entries([], NOW), [])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = wol.WorkOrderCache(self.temp.name, clock=lambda: NOW)

    def rows(self):
        parsed, _, _ = wol.parse_work_orders(payload(row(), row("WO-S", "signed_off", signoff=signed(2)),
                                                     row("WO-P", "past", signoff=signed(30))))
        return parsed

    def test_round_trip_and_file_content(self):
        self.cache.save(self.rows())
        data = json.loads((Path(self.temp.name) / "work_orders.json").read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["fetched_at"], NOW.isoformat(timespec="seconds"))
        text = json.dumps(data)
        for private in ("Asha", "1042", "done", "Bench 1", "operator", "note"):
            self.assertNotIn(private, text)
        self.assertFalse(list(Path(self.temp.name).glob("*.tmp")))
        loaded, fetched = self.cache.load()
        self.assertEqual([(r.wo_number, r.state, r.untested) for r in loaded],
                         [("WO-S00410", "open", 50), ("WO-S", "signed_off", 50), ("WO-P", "past", 50)])
        self.assertEqual(fetched, NOW.isoformat(timespec="seconds"))
        self.assertEqual(wol.days_left(loaded[1], NOW), 12)

    def test_no_serial_is_ever_written(self):
        parsed, _, _ = wol.parse_work_orders(payload(row(serials=["WO-S00410-1-001"], serial="WO-S00410-1-002")))
        self.cache.save(parsed)
        self.assertNotIn("-1-00", (Path(self.temp.name) / "work_orders.json").read_text(encoding="utf-8"))

    def test_tolerant_read(self):
        path = Path(self.temp.name) / "work_orders.json"
        self.assertIsNone(self.cache.load())
        for content in ("", "not json", "[]", '{"schema": 2}', '{"schema": 1, "fetched_at": "x", "work_orders": []}',
                        '{"schema": 1, "fetched_at": "2026-11-19T10:00:00+00:00"}'):
            path.write_text(content, encoding="utf-8")
            self.assertIsNone(self.cache.load(), content)
        path.write_bytes(b"\xff\xfe\x00")
        self.assertIsNone(self.cache.load())

    def test_bad_rows_in_cache_are_dropped(self):
        path = Path(self.temp.name) / "work_orders.json"
        path.write_text(json.dumps({"schema": 1, "fetched_at": NOW.isoformat(), "work_orders": [row(), {"x": 1}]}))
        rows, _ = self.cache.load()
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
