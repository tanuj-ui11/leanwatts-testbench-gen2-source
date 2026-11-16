"""Outbox durability, backoff, dead-letter, auth pause and replay safety. No network, no hardware."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from leanwatts_can import outbox as outbox_module
from leanwatts_can.dashboard_client import DashboardError
from leanwatts_can.outbox import Outbox, report_client_id
from leanwatts_can.outbox_service import OutboxService, report_body

CID = "6f1c2a9e-1111-4a2b-8c3d-0123456789ab"
CID2 = "7a2d3b8f-2222-4b3c-9d4e-0123456789ab"


def body(serial="WO-S00429-1-045"):
    return {"serial": serial, "result": "pass", "tested_at": "2026-11-13T10:00:00+05:30",
            "generated_at": "2026-11-13T10:05:00+05:30", "station": "LW-EOL-01", "operator": "R. Kumar (OP07)",
            "tool_version": "1.1.13", "firmware_version": None, "summary": {"stages": 8, "failed": 0},
            "report": {"schema": "leanwatts-eol-report/1", "serial": serial}}


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeClient:
    """Records calls; each queued answer is a dict (success) or an Exception to raise."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def post_report(self, payload, client_report_id):
        self.calls.append((dict(payload), client_report_id))
        answer = self.answers.pop(0) if self.answers else {"id": "srv", "duplicate": False, "replaced": False}
        if isinstance(answer, Exception):
            raise answer
        return answer


def err(kind, message="dashboard said no"):
    return DashboardError(kind, 500 if kind == "server" else 400, message)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name) / "outbox"
        self.clock = FakeClock()
        self.box = Outbox(self.root, clock=self.clock, rand=lambda: 0.0)

    def tearDown(self):
        self.folder.cleanup()

    def queue(self, cid=CID, serial="WO-S00429-1-045"):
        return self.box.write_pending(body(serial), "<html>r</html>", cid, session_id="S1", now=self.clock.now)

    def test_folders_exist_and_write_is_atomic_with_sidecar(self):
        item = self.queue()
        self.assertEqual(item.status, "pending")
        for name in ("pending", "sent", "dead"):
            self.assertTrue((self.root / name).is_dir())
        self.assertTrue((self.root / "pending" / (CID + ".json")).is_file())
        self.assertTrue((self.root / "pending" / (CID + ".state.json")).is_file())
        self.assertEqual(list(self.root.rglob("*.tmp")), [])
        payload = json.loads((self.root / "pending" / (CID + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(payload["html"], "<html>r</html>")
        self.assertEqual(payload["body"]["serial"], "WO-S00429-1-045")

    def test_failed_replace_leaves_no_partial_item(self):
        real = os.replace
        calls = {"n": 0}

        def flaky(source, target):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real(source, target)

        with patch.object(outbox_module.os, "replace", side_effect=flaky):
            with self.assertRaises(OSError):
                self.queue()
        self.assertEqual(self.box.pending_items(), [])
        self.assertEqual(list((self.root / "pending").iterdir()), [])

    def test_same_id_is_never_queued_twice(self):
        first = self.queue()
        second = self.queue()
        self.assertEqual(first.client_report_id, second.client_report_id)
        self.assertEqual(len(self.box.pending_items()), 1)

    def test_stale_is_kept_and_old_sent_state_without_it_still_loads(self):
        item = self.queue()
        sent = self.box.mark_sent(item, {"id": "r1", "duplicate": False, "replaced": False, "stale": True, "other": 1},
                                  self.clock.now)
        self.assertEqual(sent.server, {"id": "r1", "duplicate": False, "replaced": False, "stale": True})
        self.assertTrue(sent.stale)
        self.assertEqual(sent.status, "sent")
        state = self.root / "sent" / (CID + ".state.json")
        data = json.loads(state.read_text(encoding="utf-8"))
        del data["server"]["stale"]  # a sent-state file written before 1.1.12
        state.write_text(json.dumps(data), encoding="utf-8")
        old = self.box.sent_items()[0]
        self.assertFalse(old.stale)
        self.assertEqual(old.status, "sent")

    def test_html_cap_counts_utf8_bytes_not_characters(self):
        html = "\u00e9" * 200_000  # 200k characters, 400k bytes
        self.assertLessEqual(len(html), 256 * 1024)
        with self.assertRaisesRegex(ValueError, "too large to send"):
            self.box.write_pending(body(), html, CID, session_id="S1", now=self.clock.now)
        self.assertEqual(self.box.pending_items(), [])
        ok = "\u00e9" * (128 * 1024)  # exactly 256 KiB
        self.assertEqual(self.box.write_pending(body(), ok, CID, session_id="S1", now=self.clock.now).status, "pending")

    def test_report_json_cap_counts_utf8_bytes(self):
        big = body()
        big["report"] = {"note": "\u00e9" * 200_000}
        with self.assertRaisesRegex(ValueError, "too large to send"):
            self.box.write_pending(big, "<html/>", CID, session_id="S1", now=self.clock.now)

    def test_replay_of_a_sent_item_never_creates_a_second_sent_item(self):
        item = self.queue()
        self.box.mark_sent(item, {"id": "r1", "duplicate": False, "replaced": False}, self.clock.now)
        self.queue()  # operator presses Generate again for the same attempt
        self.assertEqual(self.box.pending_items(), [])
        self.assertEqual(len(self.box.sent_items()), 1)
        # Crash-replay: a stale pending copy reappears after the sent copy exists.
        src = self.root / "sent" / (CID + ".json")
        (self.root / "pending" / (CID + ".json")).write_bytes(src.read_bytes())
        service = OutboxService(self.box, lambda: FakeClient(), busy_check=lambda: False, clock=self.clock)
        service.tick(self.clock.now + 1)
        self.assertEqual(len(self.box.sent_items()), 1)
        payloads = [path for path in (self.root / "sent").glob("*.json") if not path.name.endswith(".state.json")]
        self.assertEqual(len(payloads), 1)

    def test_backoff_sequence_doubles_from_15s_and_caps_at_15_minutes(self):
        item = self.queue()
        waits = []
        for _ in range(9):
            item = self.box.mark_failed(item, "network down", self.clock.now)
            waits.append(item.next_attempt_at - self.clock.now)
        self.assertEqual(waits, [15, 30, 60, 120, 240, 480, 900, 900, 900])

    def test_jitter_stays_within_twenty_percent(self):
        box = Outbox(self.root, clock=self.clock, rand=lambda: 0.999)
        item = self.queue()
        item = box.mark_failed(item, "slow", self.clock.now)
        wait = item.next_attempt_at - self.clock.now
        self.assertGreater(wait, 15)
        self.assertLess(wait, 18.1)

    def test_due_items_respect_next_attempt(self):
        item = self.queue()
        self.box.mark_failed(item, "network down", self.clock.now)
        self.assertEqual(self.box.due_items(self.clock.now + 14), [])
        self.assertEqual(len(self.box.due_items(self.clock.now + 16)), 1)

    def test_dead_letter_keeps_payload_and_reason(self):
        item = self.queue()
        dead = self.box.mark_dead(item, "The dashboard refused the request (422).", self.clock.now)
        self.assertEqual(dead.status, "dead")
        self.assertEqual(self.box.pending_items(), [])
        self.assertTrue((self.root / "dead" / (CID + ".json")).is_file())
        self.assertIn("422", self.box.dead_items()[0].last_error)

    def test_requeue_moves_dead_back_for_resend(self):
        self.box.mark_dead(self.queue(), "refused", self.clock.now)
        again = self.box.requeue(CID, self.clock.now)
        self.assertEqual(again.status, "pending")
        self.assertEqual(self.box.dead_items(), [])
        with self.assertRaises(ValueError):
            self.box.requeue(CID, self.clock.now)

    def test_unreadable_payload_is_skipped_and_not_deleted(self):
        self.queue()
        (self.root / "pending" / (CID2 + ".json")).write_text("{not json", encoding="utf-8")
        self.assertEqual([item.client_report_id for item in self.box.pending_items()], [CID])
        self.assertTrue((self.root / "pending" / (CID2 + ".json")).is_file())

    def test_status_for_serial_in_session(self):
        self.queue()
        item = self.box.status_for("S1", "WO-S00429-1-045")
        self.assertEqual(item.status, "pending")
        self.assertIsNone(self.box.status_for("S2", "WO-S00429-1-045"))

    def test_client_id_is_stable_uuid_shaped(self):
        first = report_client_id("LW-EOL-01", "S1", "WO-S00429-1-045", "a1")
        self.assertEqual(first, report_client_id("LW-EOL-01", "S1", "WO-S00429-1-045", "a1"))
        self.assertNotEqual(first, report_client_id("LW-EOL-01", "S1", "WO-S00429-1-045", "a2"))
        self.assertEqual(len(first), 36)

    def test_report_body_maps_spec_fields_and_refuses_simulation(self):
        report = {"serial": "WO-S00429-1-045", "verdict": "FAIL", "finished_at_utc": "2026-11-13T04:00:00Z",
                  "station": "LW-EOL-01", "operator": {"id": "OP07", "name": "R. Kumar"},
                  "software": {"version": "1.1.13"}, "identity": {"charger_fw": "0x000F"},
                  "stages": [{"status": "FAIL"}, {"status": "PASS"}]}
        mapped = report_body(report, "<html/>", station="LW-EOL-01", generated_at="2026-11-13T04:01:00Z")
        self.assertEqual(mapped["result"], "fail")
        self.assertEqual(mapped["operator"], "R. Kumar (OP07)")
        self.assertEqual(mapped["summary"], {"stages": 2, "failed": 1})
        self.assertNotIn("report_html", mapped)
        with self.assertRaises(ValueError):
            report_body(dict(report, verdict="SIMULATION"), None, station="x", generated_at="t")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name) / "outbox"
        self.clock = FakeClock()
        self.box = Outbox(self.root, clock=self.clock, rand=lambda: 0.0)

    def tearDown(self):
        self.folder.cleanup()

    def service(self, client, busy=False):
        return OutboxService(self.box, lambda: client, busy_check=lambda: busy, clock=self.clock)

    def queue(self, cid, serial):
        return self.box.write_pending(body(serial), "<html/>", cid, session_id="S1", now=self.clock.now)

    def test_sends_due_items_one_at_a_time_in_order(self):
        self.queue(CID, "WO-S00429-1-001")
        self.queue(CID2, "WO-S00429-1-002")
        client = FakeClient({"id": "a"}, {"id": "b"})
        summary = self.service(client).tick(self.clock.now)
        self.assertEqual(summary["sent"], 2)
        self.assertEqual([call[1] for call in client.calls], [CID, CID2])
        self.assertEqual(client.calls[0][0]["report_html"], "<html/>")
        self.assertEqual(client.calls[0][0]["serial"], "WO-S00429-1-001")
        self.assertEqual(len(self.box.sent_items()), 2)

    def test_busy_instrument_holds_all_sends(self):
        self.queue(CID, "WO-S00429-1-001")
        client = FakeClient()
        summary = self.service(client, busy=True).tick(self.clock.now)
        self.assertEqual(summary["stopped"], "busy")
        self.assertEqual(client.calls, [])
        self.assertEqual(len(self.box.pending_items()), 1)

    def test_network_error_backs_off_and_keeps_item(self):
        self.queue(CID, "WO-S00429-1-001")
        self.queue(CID2, "WO-S00429-1-002")
        client = FakeClient(err("network", "offline"))
        service = self.service(client)
        summary = service.tick(self.clock.now)
        self.assertEqual(summary["stopped"], "backoff")
        self.assertEqual(len(client.calls), 1)  # stops this tick rather than hammering
        pending = self.box.pending_items()
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0].attempts, 1)
        self.assertTrue((self.root / "pending" / (CID + ".json")).is_file())
        # Backoff is per item: the failed item waits, the other due item still goes out.
        later = service.tick(self.clock.now + 5)
        self.assertEqual(later["sent"], 1)
        self.assertEqual([item.client_report_id for item in self.box.pending_items()], [CID])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1][1], CID2)

    def test_server_error_is_retried_not_dead_lettered(self):
        self.queue(CID, "WO-S00429-1-001")
        self.service(FakeClient(err("server"))).tick(self.clock.now)
        self.assertEqual(self.box.dead_items(), [])
        self.assertEqual(len(self.box.pending_items()), 1)

    def test_rejected_and_too_large_go_to_dead_letter_and_queue_continues(self):
        self.queue(CID, "WO-S00429-1-001")
        self.queue(CID2, "WO-S00429-1-002")
        summary = self.service(FakeClient(err("rejected", "invalid body"), {"id": "ok"})).tick(self.clock.now)
        self.assertEqual((summary["dead"], summary["sent"]), (1, 1))
        self.assertEqual([item.client_report_id for item in self.box.dead_items()], [CID])
        self.queue("9c0d1e2f-3333-4c4d-8e5f-0123456789ab", "WO-S00429-1-003")
        self.service(FakeClient(err("too_large", "big"))).tick(self.clock.now)
        self.assertEqual(len(self.box.dead_items()), 2)
        self.assertEqual(self.box.pending_items(), [])

    def test_auth_error_pauses_queue_and_consumes_no_retry(self):
        self.queue(CID, "WO-S00429-1-001")
        client = FakeClient(err("auth", "The dashboard did not accept the API key."))
        service = self.service(client)
        summary = service.tick(self.clock.now)
        self.assertEqual(summary["stopped"], "paused")
        self.assertIn("API key", service.paused_message)
        item = self.box.pending_items()[0]
        self.assertEqual(item.attempts, 0)
        self.assertEqual(item.next_attempt_at, 1000.0)
        self.assertEqual(service.tick(self.clock.now + 3600)["stopped"], "paused")
        self.assertEqual(len(client.calls), 1)
        service.resume()
        self.assertEqual(service.paused_message, "")

    def test_unconfigured_dashboard_pauses_without_touching_items(self):
        self.queue(CID, "WO-S00429-1-001")

        def provider():
            raise ValueError("Set the dashboard address in Settings > Dashboard first.")

        service = OutboxService(self.box, provider, busy_check=lambda: False, clock=self.clock)
        self.assertEqual(service.tick(self.clock.now)["stopped"], "paused")
        self.assertEqual(len(self.box.pending_items()), 1)

    def test_unexpected_exception_backs_off_and_timer_survives(self):
        self.queue(CID, "WO-S00429-1-001")

        class Broken(FakeClient):
            def post_report(self, payload, client_report_id):
                raise RuntimeError("bug")

        summary = self.service(Broken()).tick(self.clock.now)
        self.assertEqual(summary["stopped"], "backoff")
        self.assertEqual(len(self.box.pending_items()), 1)

    def test_nothing_due_makes_no_client_call(self):
        client = FakeClient()
        self.assertEqual(self.service(client).tick(self.clock.now)["sent"], 0)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
