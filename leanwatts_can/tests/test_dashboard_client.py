"""Dashboard client against a local ThreadingHTTPServer on 127.0.0.1. No external network."""
import json
import os
import socket
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from leanwatts_can.dashboard_client import MAX_RESPONSE_BYTES, DashboardClient, DashboardError

KEY = "lw_test_key_0123456789abcdef"
CID = "6f1c2a9e-1111-4a2b-8c3d-0123456789ab"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, value):
        self._send(status, json.dumps(value).encode("utf-8"), {"Content-Type": "application/json"})

    def _record(self, body=b""):
        self.server.hits.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})

    def _route(self, body=b""):
        self._record(body)
        path = self.path
        if path == "/api/v1/eol/ping":
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return self._json(200, {"user": "bench-user", "role": "testing", "server_time": now})
        if path == "/api/v1/eol/reports":
            return self._json(201, {"id": "r1", "duplicate": False, "replaced": False})
        if path.startswith("/api/v1/eol/serials/"):
            tail = path.rsplit("/", 1)[1]
            if tail == "unauth":
                return self._json(401, {"error": {"code": "invalid_api_key"}})
            if tail == "forbidden":
                return self._json(403, {"error": {"code": "forbidden"}})
            if tail == "missing":
                return self._json(404, {"error": {"code": "unknown_serial"}})
            if tail == "big":
                return self._json(413, {"error": {"code": "too_large"}})
            if tail == "rejected":
                return self._json(422, {"error": {"code": "invalid_result"}})
            if tail == "boom":
                return self._json(503, {"error": {"code": "unavailable"}})
            if tail == "huge":
                return self._send(200, b"x" * (MAX_RESPONSE_BYTES + 10), {"Content-Type": "application/json"})
            if tail == "nonjson":
                return self._send(200, b"<html>login</html>", {"Content-Type": "text/html"})
            if tail == "read-only":
                return self._json(403, {"error": {"code": "forbidden", "message": "This key is read-only."}})
            if tail == "gone":
                return self._json(404, {"error": {"code": "not_found", "message": "No such serial on this dashboard."}})
            if tail == "conflict":
                return self._json(409, {"detail": "A newer report exists.\x1b[31m " + KEY})
            if tail == "leaky":
                return self._json(400, {"error": {"code": "Bad Key " + KEY}})
            return self._json(200, {"serial": tail})
        if path == "/api/v1/eol/work-orders/WO-R/serials":
            return self._send(302, b"", {"Location": "/api/v1/eol/ping"})
        return self._json(404, {"error": {"code": "not_here"}})

    def do_GET(self):
        self._route()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self._route(self.rfile.read(length))


class DashboardClientTests(unittest.TestCase):
    def setUp(self):
        # Loopback must never be sent through a system proxy.
        self.env = patch.dict(os.environ, {"no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"})
        self.env.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.hits = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.client = DashboardClient(self.base, KEY, timeout=5, allow_loopback_http=True)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.env.stop()

    def hits(self, path):
        # urllib capitalises header names on the wire (X-api-key); HTTP header names are case-insensitive.
        found = []
        for hit in self.server.hits:
            if hit["path"] == path:
                found.append(dict(hit, headers={key.lower(): value for key, value in hit["headers"].items()}))
        return found

    def expect(self, kind, call):
        with self.assertRaises(DashboardError) as raised:
            call()
        self.assertEqual(raised.exception.kind, kind)
        self.assertNotIn(KEY, raised.exception.message)
        return raised.exception

    def test_ping_sends_api_key_and_no_idempotency_key(self):
        result = self.client.ping()
        self.assertEqual(result["role"], "testing")
        hit = self.hits("/api/v1/eol/ping")[0]
        self.assertEqual(hit["headers"]["x-api-key"], KEY)
        self.assertIsNone(hit["headers"].get("idempotency-key"))
        self.assertNotIn("authorization", hit["headers"])

    def test_post_sends_idempotency_key_and_json_body(self):
        report = {"serial": "WO-S00429-1-045", "result": "pass", "report": {"verdict": "PASS"}}
        result = self.client.post_report(report, client_report_id=CID)
        self.assertEqual(result["id"], "r1")
        hit = self.hits("/api/v1/eol/reports")[0]
        self.assertEqual(hit["headers"]["idempotency-key"], CID)
        self.assertEqual(hit["headers"]["x-api-key"], KEY)
        self.assertEqual(hit["headers"]["content-type"], "application/json")
        sent = json.loads(hit["body"].decode("utf-8"))
        self.assertEqual(sent["client_report_id"], CID)
        self.assertEqual(sent["serial"], "WO-S00429-1-045")
        self.assertEqual(sent["report"], {"verdict": "PASS"})

    def test_serial_path_is_quoted_and_fetched(self):
        self.assertEqual(self.client.serial("WO-S00429-1-045")["serial"], "WO-S00429-1-045")
        self.assertEqual(len(self.hits("/api/v1/eol/serials/WO-S00429-1-045")), 1)

    def test_status_codes_map_to_error_kinds(self):
        self.assertEqual(self.expect("auth", lambda: self.client.serial("unauth")).status, 401)
        self.assertEqual(self.expect("auth", lambda: self.client.serial("forbidden")).status, 403)
        self.expect("not_found", lambda: self.client.serial("missing"))
        self.expect("too_large", lambda: self.client.serial("big"))
        self.expect("rejected", lambda: self.client.serial("rejected"))
        self.expect("server", lambda: self.client.serial("boom"))

    def test_401_and_403_have_distinct_messages_and_keep_kind_auth(self):
        unauth = self.expect("auth", lambda: self.client.serial("unauth"))
        forbidden = self.expect("auth", lambda: self.client.serial("forbidden"))
        self.assertEqual(unauth.message, "The dashboard did not accept this key (check the key in Settings > Dashboard).")
        self.assertEqual(forbidden.message, "This key is not allowed to use the EoL API (it needs a Testing-role user with write access).")

    def test_server_text_is_shown_cleaned_and_without_the_key(self):
        missing = self.expect("not_found", lambda: self.client.serial("gone"))
        self.assertIn("not_found", missing.message)
        self.assertIn("No such serial on this dashboard", missing.message)
        conflict = self.expect("rejected", lambda: self.client.serial("conflict"))
        self.assertIn("A newer report exists.", conflict.message)
        self.assertNotIn("\x1b", conflict.message)
        self.assertNotIn(KEY, conflict.message)
        self.assertEqual(self.expect("auth", lambda: self.client.serial("read-only")).status, 403)

    def test_error_message_carries_only_vetted_code(self):
        error = self.expect("not_found", lambda: self.client.serial("missing"))
        self.assertIn("unknown_serial", error.message)
        leaky = self.expect("rejected", lambda: self.client.serial("leaky"))
        self.assertNotIn(KEY, leaky.message)
        self.assertNotIn("Bad", leaky.message)

    def test_redirect_is_refused_and_not_followed(self):
        error = self.expect("network", lambda: self.client.work_order_serials("WO-R"))
        self.assertEqual(error.status, 302)
        self.assertEqual(len(self.hits("/api/v1/eol/ping")), 0)

    def test_response_size_cap_is_enforced(self):
        self.expect("server", lambda: self.client.serial("huge"))

    def test_non_json_success_is_a_server_error(self):
        self.expect("server", lambda: self.client.serial("nonjson"))

    def test_oversized_request_is_refused_before_sending(self):
        before = len(self.server.hits)
        self.expect("too_large", lambda: self.client.post_report({"serial": "x", "blob": "a" * (2 * 1024 * 1024 + 10)}, client_report_id=CID))
        self.assertEqual(len(self.server.hits), before)

    def test_unreachable_host_is_network_error(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        client = DashboardClient(f"http://127.0.0.1:{port}", KEY, timeout=2, allow_loopback_http=True)
        self.expect("network", client.ping)

    def test_https_is_enforced_for_remote_hosts(self):
        with self.assertRaises(ValueError):
            DashboardClient("http://ops.example.com", KEY)
        with self.assertRaises(ValueError):
            DashboardClient("http://127.0.0.1:8080", KEY)
        with self.assertRaises(ValueError):
            DashboardClient("http://localhost:8080", KEY)  # loopback needs the explicit test flag
        with self.assertRaises(ValueError):
            DashboardClient("http://ops.example.com", KEY, allow_loopback_http=True)
        self.assertEqual(DashboardClient("https://ops.example.com/", KEY).base_url, "https://ops.example.com")

    def test_url_with_path_credentials_or_query_is_refused(self):
        for value in ("https://ops.example.com/api", "https://user:pw@ops.example.com", "https://ops.example.com?x=1", "ftp://ops.example.com"):
            with self.assertRaises(ValueError, msg=value):
                DashboardClient(value, KEY)

    def test_api_key_shape_and_repr(self):
        with self.assertRaises(ValueError):
            DashboardClient("https://ops.example.com", "short")
        self.assertNotIn(KEY, repr(self.client))

    def test_clock_skew_is_server_minus_local(self):
        tz = timezone(timedelta(hours=5, minutes=30))
        local = datetime(2026, 11, 13, 10, 0, 0, tzinfo=tz)
        skew = DashboardClient.clock_skew_seconds({"server_time": "2026-11-13T10:00:30+05:30"}, local)
        self.assertEqual(skew, 30.0)
        with self.assertRaises(ValueError):
            DashboardClient.clock_skew_seconds({"server_time": "2026-11-13T10:00:30"}, local)
        with self.assertRaises(ValueError):
            DashboardClient.clock_skew_seconds({"server_time": "2026-11-13T10:00:30+05:30"}, local.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
