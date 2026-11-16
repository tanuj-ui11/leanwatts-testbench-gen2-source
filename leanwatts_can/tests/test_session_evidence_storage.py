"""Raw evidence lives in per-attempt sidecars; caches follow the store revision; the outbox scan is cached."""
import copy
import json
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from leanwatts_can import outbox as outbox_module, session_store
from leanwatts_can.outbox import Outbox, report_client_id
from leanwatts_can.session_store import SessionStore, atomic_json

from test_session_evidence_equivalence import (EVIDENCE_FIELDS_UNDER_TEST, OPERATOR, RECIPE, cal_result, deterministic, run,
                                               scenario, strip_heavy, test_only_result)

HEAVY = EVIDENCE_FIELDS_UNDER_TEST


def session_file(folder, session_id):
    return Path(folder) / "sessions" / (session_id + ".json")


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = self.temp.name
        self.addCleanup(self.temp.cleanup)

    def finished_store(self, name="pass"):
        with deterministic():
            return scenario(name, self.folder)

    def test_main_file_has_no_raw_lists_and_sidecars_hold_them(self):
        store = self.finished_store()
        session_id = store.active()["session_id"]
        raw = json.loads(session_file(self.folder, session_id).read_text(encoding="utf-8"))
        for unit in raw["units"]:
            for key in ("calibration", "test"):
                record = unit.get(key) or {}
                self.assertFalse(any(name in record for name in HEAVY), (unit["attempt_id"], key))
            self.assertEqual(unit["evidence_sidecar"]["schema"], "leanwatts-evidence/1")
            payload = store.load_evidence(session_id, unit["attempt_id"])
            for key, counts in unit["evidence_sidecar"]["counts"].items():
                for name, count in counts.items():
                    self.assertEqual(len(payload[key][name]), count)
        # Small records stay inline: reports, CSV and the EoL builder read them.
        self.assertTrue(raw["units"][0]["calibration"]["results"])
        self.assertTrue(raw["units"][0]["calibration"]["events"])
        # The checkpoint is compact JSON.
        self.assertNotIn("\n", session_file(self.folder, session_id).read_text(encoding="utf-8"))

    def test_hydrate_restores_exactly_what_the_engine_produced(self):
        originals = {}
        with deterministic():
            store = SessionStore(self.folder)
            store.start(OPERATOR, mode="both_required")
            result = cal_result("a")
            originals["cal"] = copy.deepcopy(result)
            attempt = store.begin_unit("WO-S00430-1-001", copy.deepcopy(RECIPE), 3, run_tests=False, run_calibration=True)
            returned = store.finish_unit(attempt, result)
        # finish_unit still hands back the complete attempt.
        self.assertEqual(returned["calibration"]["samples"], originals["cal"]["calibration"]["samples"])
        self.assertNotIn("evidence_sidecar", returned)
        full = store.hydrate_session(store.active())
        unit = full["units"][0]
        for name in HEAVY:
            self.assertEqual(unit["calibration"][name], originals["cal"]["calibration"][name])
        self.assertNotIn("evidence_sidecar", unit)
        # The caller's own result object is not modified.
        self.assertIn("samples", result["calibration"])

    def test_old_active_session_with_inline_samples_is_migrated_on_start(self):
        store = self.finished_store("repeated_serial")
        session_id = store.active()["session_id"]
        inline = store.hydrate_session(store.active())
        expected_summaries = SessionStore.summarize(inline, evidence=True)
        # Rewrite the files the way the previous version stored them: everything inline, indented.
        shutil_rmtree(Path(self.folder) / "sessions" / (session_id + ".evidence"))
        atomic_json(session_file(self.folder, session_id), inline)
        before = json.loads(session_file(self.folder, session_id).read_text(encoding="utf-8"))
        self.assertIn("samples", before["units"][0]["calibration"])
        reopened = SessionStore(self.folder)
        after = json.loads(session_file(self.folder, session_id).read_text(encoding="utf-8"))
        self.assertNotIn("samples", after["units"][0]["calibration"])
        self.assertEqual(SessionStore.summarize(reopened.hydrate_session(reopened.active()), evidence=True), expected_summaries)
        self.assertEqual(strip_heavy(SessionStore.summarize(reopened.active(), evidence=True)), strip_heavy(expected_summaries))
        for unit_before, unit_after in zip(inline["units"], reopened.hydrate_session(reopened.active())["units"]):
            self.assertEqual(unit_after, unit_before)

    def test_old_closed_session_loads_inline_and_is_left_untouched(self):
        store = self.finished_store("pass")
        store.close()
        session_id = store.list_sessions()[0]["session_id"]
        inline = store.hydrate_session(store.list_sessions()[0])
        path = session_file(self.folder, session_id)
        shutil_rmtree(Path(self.folder) / "sessions" / (session_id + ".evidence"))
        atomic_json(path, inline)
        original_bytes = path.read_bytes()
        reopened = SessionStore(self.folder)
        loaded = reopened.list_sessions()[0]
        self.assertIn("samples", loaded["units"][0]["calibration"])
        self.assertEqual(reopened.hydrate_session(loaded), loaded)
        self.assertEqual(path.read_bytes(), original_bytes)
        destination = Path(self.folder) / "export.json"
        reopened.export_record(session_id, destination)
        self.assertEqual(destination.read_bytes(), original_bytes)

    def test_restart_after_a_crash_marks_the_running_attempt_and_keeps_finished_evidence(self):
        with deterministic():
            store = SessionStore(self.folder)
            store.start(OPERATOR, mode="both_required")
            first = run(store, "WO-S00430-1-001", "cal", cal_result("a"))
            store.begin_unit("WO-S00430-1-001", copy.deepcopy(RECIPE), 3, run_tests=True, run_calibration=False)
            session_id = store.active()["session_id"]
            del store  # the process ends here
            reopened = SessionStore(self.folder)
        units = reopened.active()["units"]
        self.assertEqual([u["outcome"] for u in units], ["CALIBRATED", "INTERRUPTED"])
        self.assertEqual(units[1]["calibration_result"], "INCOMPLETE")
        self.assertEqual(units[1]["test_result"], "NOT RUN")
        self.assertNotIn("evidence_sidecar", units[1])
        evidence = reopened.load_evidence(session_id, first)
        self.assertEqual(len(evidence["calibration"]["samples"]), len(cal_result("a")["calibration"]["samples"]))
        # And the recovered session keeps working: the next attempt can be recorded.
        with deterministic():
            run(reopened, "WO-S00430-1-001", "cal", cal_result("z"))
        self.assertEqual(len(reopened.active()["units"]), 3)

    def test_failed_sidecar_write_keeps_the_evidence_inline_and_retries_later(self):
        real = session_store.atomic_json
        def flaky(path, value, **kwargs):
            if ".evidence" in str(path):
                raise OSError("disk full")
            return real(path, value, **kwargs)
        with deterministic():
            store = SessionStore(self.folder)
            store.start(OPERATOR, mode="independent")
            with mock.patch.object(session_store, "atomic_json", flaky), self.assertLogs(session_store.LOGGER, "WARNING"):
                attempt = store.begin_unit("WO-S00430-1-001", copy.deepcopy(RECIPE), 3, run_tests=False, run_calibration=True)
                store.finish_unit(attempt, cal_result("a"))
            session_id = store.active()["session_id"]
            raw = json.loads(session_file(self.folder, session_id).read_text(encoding="utf-8"))
            self.assertIn("samples", raw["units"][0]["calibration"])
            self.assertNotIn("evidence_sidecar", raw["units"][0])
            # Nothing was lost; the next healthy save moves it.
            store.begin_unit("WO-S00430-1-002", copy.deepcopy(RECIPE), 3, run_tests=False, run_calibration=True)
        raw = json.loads(session_file(self.folder, session_id).read_text(encoding="utf-8"))
        self.assertNotIn("samples", raw["units"][0]["calibration"])
        self.assertTrue(store.load_evidence(session_id, raw["units"][0]["attempt_id"])["calibration"]["samples"])

    def test_missing_sidecar_is_reported_not_silently_dropped(self):
        store = self.finished_store("test_only")
        session_id = store.active()["session_id"]
        attempt = store.active()["units"][0]["attempt_id"]
        (Path(self.folder) / "sessions" / (session_id + ".evidence") / (attempt + ".json")).unlink()
        with self.assertRaises(ValueError):
            store.hydrate_session(store.active(), strict=True)
        with self.assertRaises(ValueError):
            store.export_record(session_id, Path(self.folder) / "x.json")
        with self.assertLogs(session_store.LOGGER, "WARNING"):
            soft = store.hydrate_session(store.active())
        self.assertIn("evidence_sidecar", soft["units"][0])

    def test_export_record_contains_the_raw_acquisitions(self):
        store = self.finished_store("pass")
        session_id = store.active()["session_id"]
        destination = Path(self.folder) / "complete.json"
        store.export_record(session_id, destination)
        exported = json.loads(destination.read_text(encoding="utf-8"))
        self.assertTrue(all(name in exported["units"][0]["calibration"] for name in HEAVY))
        self.assertTrue(all(name in exported["units"][1]["test"] for name in HEAVY))
        self.assertFalse(any("evidence_sidecar" in unit for unit in exported["units"]))

    def test_unsafe_attempt_ids_are_refused(self):
        store = self.finished_store("test_only")
        with self.assertRaises(ValueError):
            store.load_evidence(store.active()["session_id"], "../operators")

    def test_session_store_keeps_crlf_line_endings(self):
        data = (Path(session_store.__file__)).read_bytes()
        self.assertEqual(data.count(b"\r\n"), data.count(b"\n"))


def shutil_rmtree(path):
    import shutil
    shutil.rmtree(path)


class RevisionCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with deterministic():
            self.store = scenario("pass", self.temp.name)

    def test_repeated_reads_do_not_resummarize_and_changes_invalidate(self):
        store = self.store
        calls = []
        real = SessionStore.summarize.__func__
        def counting(cls, data, **kwargs):
            calls.append(1)
            return real(cls, data, **kwargs)
        with mock.patch.object(SessionStore, "summarize", classmethod(counting)):
            store.pending_unit()
            first = len(calls)
            for _ in range(5):
                store.pending_unit()
                store.readiness("WO-S00430-1-003", RECIPE, 3, run_calibration=True, run_tests=False)
            self.assertEqual(len(calls), first)
            revision = store.revision
            with deterministic():
                store.begin_unit("WO-S00430-1-003", copy.deepcopy(RECIPE), 3, run_tests=False, run_calibration=True)
            self.assertGreater(store.revision, revision)
            pending = store.pending_unit()
        self.assertEqual(pending["serial"], "WO-S00430-1-003")
        self.assertEqual(pending["next_step"], "Parameters + Calibration")

    def test_pending_unit_and_readiness_match_a_fresh_summary(self):
        store = self.store
        with deterministic():
            run(store, "WO-S00430-1-003", "cal", cal_result("n"))
        fresh = [item for item in SessionStore.summarize(store.active()) if item["completion_state"] != "COMPLETE"][0]
        self.assertEqual(store.pending_unit(), fresh)
        self.assertIn("Finish Test for WO-S00430-1-003",
                      store.readiness("WO-S00430-1-001", RECIPE, 3, run_calibration=True, run_tests=False))
        # A caller may mutate what it was given without corrupting the cache.
        store.pending_unit()["serial"] = "changed"
        self.assertEqual(store.pending_unit()["serial"], "WO-S00430-1-003")

    def test_paired_stage_details_match_the_full_evidence_summary(self):
        store = self.store
        with deterministic():
            attempt = store.begin_unit("WO-S00430-1-003", copy.deepcopy(RECIPE), 3, run_tests=False, run_calibration=True)
            store.finish_unit(attempt, cal_result("n"))
            test_attempt = store.begin_unit("WO-S00430-1-003", copy.deepcopy(RECIPE), 3, run_tests=True, run_calibration=False)
        old = next(item for item in SessionStore.summarize(store.active() or {}, evidence=True)
                   if test_attempt in item.get("attempt_ids", []))
        result, details = store.paired_stage_details(test_attempt)
        self.assertEqual(result, old["calibration_result"])
        self.assertEqual(details, old["stage_details"])
        self.assertIsNone(store.paired_stage_details("nope"))

    def test_is_open_follows_the_session(self):
        self.assertTrue(self.store.is_open())
        self.store.close()
        self.assertFalse(self.store.is_open())
        self.assertIsNone(self.store.pending_unit())

    def test_worker_thread_finish_while_the_gui_thread_reads(self):
        store = self.store
        with deterministic():
            attempt = store.begin_unit("WO-S00430-1-003", copy.deepcopy(RECIPE), 3, run_tests=False, run_calibration=True)
        errors, stop = [], threading.Event()
        def reader():
            while not stop.is_set():
                try:
                    pending = store.pending_unit()
                    if pending is not None:
                        self.assertIn(pending["serial"], ("WO-S00430-1-003",))
                    store.readiness("WO-S00430-1-004", RECIPE, 3, run_calibration=True, run_tests=False)
                    store.is_open()
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)
                    return
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            with deterministic():
                store.finish_unit(attempt, cal_result("w"))
        finally:
            stop.set()
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(store.pending_unit()["next_step"], "Test")

    def test_list_sessions_is_served_from_memory_until_the_file_changes(self):
        store = self.store
        first = store.list_sessions()
        with mock.patch.object(SessionStore, "_load", side_effect=AssertionError("re-parsed")):
            self.assertEqual(store.list_sessions(), first)
        with deterministic():
            run(store, "WO-S00430-1-003", "cal", cal_result("n"))
        again = store.list_sessions()
        self.assertEqual(len(again[0]["units"]), len(first[0]["units"]) + 1)


class OutboxCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.outbox = Outbox(Path(self.temp.name) / "outbox")

    def queue(self, serial, session="S1", attempt="a"):
        body = {"serial": serial, "result": "pass"}
        return self.outbox.write_pending(body, "<html/>", report_client_id("ST", session, serial, attempt), session_id=session)

    def test_status_map_matches_per_serial_lookups(self):
        items = {serial: self.queue(serial) for serial in ("WO-S00430-1-001", "WO-S00430-1-002", "WO-S00430-1-003")}
        self.outbox.mark_sent(items["WO-S00430-1-002"], {"id": "x"})
        self.outbox.mark_dead(items["WO-S00430-1-003"], "rejected")
        mapping = self.outbox.status_map()
        for serial in items:
            self.assertEqual(mapping[("S1", serial)], self.outbox.status_for("S1", serial))
        self.assertEqual([mapping[("S1", s)].status for s in items], ["pending", "sent", "dead"])
        self.assertIsNone(self.outbox.status_for("S1", "WO-S00430-1-999"))

    def test_unchanged_files_are_not_parsed_again_but_changes_are_seen(self):
        item = self.queue("WO-S00430-1-001")
        self.outbox.pending_items()
        with mock.patch.object(Outbox, "_item", side_effect=AssertionError("re-parsed")):
            self.assertEqual(len(self.outbox.pending_items()), 1)
            self.assertEqual(len(self.outbox.due_items(10 ** 12)), 1)
        failed = self.outbox.mark_failed(item, "network", now=1000.0)
        self.assertEqual(self.outbox.pending_items()[0].attempts, 1)
        self.assertEqual(self.outbox.pending_items()[0].next_attempt_at, failed.next_attempt_at)
        # Another process rewriting the state file is noticed through its size/mtime.
        state_path = Path(self.temp.name) / "outbox" / "pending" / (item.client_report_id + ".state.json")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["attempts"] = 7
        state["last_error"] = "changed elsewhere, with a longer message"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.outbox.pending_items()[0].attempts, 7)
        self.outbox.mark_sent(self.outbox.pending_items()[0])
        self.assertEqual(self.outbox.pending_items(), [])
        self.assertEqual(len(self.outbox.sent_items()), 1)

    def test_a_damaged_state_is_rebuilt_and_a_removed_file_disappears(self):
        item = self.queue("WO-S00430-1-001")
        state_path = Path(self.temp.name) / "outbox" / "pending" / (item.client_report_id + ".state.json")
        self.outbox.pending_items()
        state_path.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.outbox.pending_items()[0].next_attempt_at, 0.0)
        (Path(self.temp.name) / "outbox" / "pending" / (item.client_report_id + ".json")).unlink()
        self.assertEqual(self.outbox.pending_items(), [])


if __name__ == "__main__":
    unittest.main()
