"""Device sign-in and refresh behavior with fake HTTP and fake credentials."""
import json
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from leanwatts_can import github_auth as auth, update_credentials as credentials, updater

CONFIG = dict(schema=1, owner="leanwatts-fixture", repository="testbench-releases",
              repository_id=123, client_id="Iv1.FAKE_PUBLIC_CLIENT_ID", manifest_asset="update-manifest.json")
ACCESS = "ghu_FAKE_ACCESS_TOKEN_FOR_TESTS_12345"
REFRESH = "ghr_FAKE_REFRESH_TOKEN_FOR_TESTS_12345"


def tokens(access=ACCESS):
    return dict(access_token=access, refresh_token=REFRESH, expires_in=28800,
                refresh_token_expires_in=15897600, token_type="bearer")


class GitHubAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.config = patch.object(auth, "channel_config", return_value=dict(CONFIG))
        self.config.start()
        self.crypt = patch.object(credentials, "_crypt", side_effect=lambda value, protect: b"TEST:" + value if protect else value[5:])
        self.crypt.start()

    def tearDown(self):
        self.crypt.stop()
        self.config.stop()
        self.temp.cleanup()

    def flow(self):
        with patch.object(auth, "_request_json", return_value=dict(device_code="d" * 40,
                user_code="ABCD-EFGH", verification_uri=auth.VERIFY_URL, expires_in=900, interval=5)):
            flow = auth.begin_sign_in()
        flow["_next_poll"] = 0
        return flow

    def test_pending_then_slow_down_obeys_poll_interval(self):
        flow = self.flow()
        with patch.object(auth, "_request_json", return_value={"error": "authorization_pending"}) as request:
            self.assertEqual(auth.poll_sign_in(self.data, flow)["status"], "pending")
            self.assertEqual(auth.poll_sign_in(self.data, flow)["status"], "pending")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[1]["repository_id"], "123")
        flow["_next_poll"] = 0
        with patch.object(auth, "_request_json", return_value={"error": "slow_down", "interval": 10}):
            result = auth.poll_sign_in(self.data, flow)
        self.assertEqual(result, {"status": "slow_down", "interval": 10})
        self.assertGreater(flow["_next_poll"], time.monotonic())

    def test_expiry_and_cancel_make_no_request_and_remove_code(self):
        for mode in ("expired", "cancelled"):
            flow = self.flow()
            event = threading.Event()
            if mode == "expired":
                flow["_deadline"] = 0
            else:
                event.set()
            with patch.object(auth, "_request_json") as request:
                self.assertEqual(auth.poll_sign_in(self.data, flow, event)["status"], mode)
            request.assert_not_called()
            self.assertNotIn("device_code", flow)

    def test_completion_saves_credentials_but_returns_only_public_status(self):
        flow = self.flow()
        with patch.object(auth, "_request_json", side_effect=[tokens(), {"login": "factory-fixture"}]):
            result = auth.poll_sign_in(self.data, flow)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(credentials.load_auth(self.data)["refresh_token"], REFRESH)
        self.assertNotIn(ACCESS, json.dumps(result))
        self.assertNotIn(REFRESH, json.dumps(auth.connection_status(self.data)))
        self.assertNotIn("device_code", flow)
        self.assertEqual(auth.connection_status(self.data)["status"], "connected")

    def test_cancel_during_request_or_save_does_not_leave_credentials(self):
        flow = self.flow()
        event = threading.Event()
        def response(*args, **kwargs):
            event.set()
            return tokens()
        with patch.object(auth, "_request_json", side_effect=response):
            self.assertEqual(auth.poll_sign_in(self.data, flow, event)["status"], "cancelled")
        self.assertFalse(credentials.has_auth(self.data))
        event.clear()
        flow = self.flow()
        original = credentials.save_auth
        def save_then_cancel(*args):
            original(*args)
            event.set()
        with patch.object(auth, "_request_json", side_effect=[tokens(), {"login": "factory-fixture"}]), patch.object(credentials, "save_auth", side_effect=save_then_cancel):
            self.assertEqual(auth.poll_sign_in(self.data, flow, event)["status"], "cancelled")
        self.assertFalse(credentials.has_auth(self.data))

    def test_signout_during_request_invalidates_pending_flow(self):
        flow = self.flow()
        def response(url, *args, **kwargs):
            if url == auth.TOKEN_ENDPOINT:
                auth.sign_out(self.data)
                return tokens()
            return {"login": "factory-fixture"}
        with patch.object(auth, "_request_json", side_effect=response):
            self.assertEqual(auth.poll_sign_in(self.data, flow)["status"], "cancelled")
        self.assertFalse(credentials.has_auth(self.data))

    def test_refresh_is_serialized_and_pair_rotates_once(self):
        record = auth._tokens(tokens(), CONFIG, "factory-fixture")
        record["expires_at"] = time.time() - 1
        credentials.save_auth(self.data, record)
        new_access = "ghu_FAKE_REFRESHED_ACCESS_TOKEN_67890"
        def refreshed(*args, **kwargs):
            time.sleep(.03)
            return tokens(new_access)
        with patch.object(auth, "_request_json", side_effect=refreshed) as request, ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(auth.access_token, [self.data] * 4))
        self.assertEqual(results, [new_access] * 4)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("client_secret", request.call_args.args[1])
        self.assertEqual(credentials.load_auth(self.data)["access_token"], new_access)

    def test_refresh_expiry_requires_signin_and_does_not_request(self):
        record = auth._tokens(tokens(), CONFIG, "factory-fixture")
        record["expires_at"] = record["refresh_expires_at"] = time.time() - 1
        credentials.save_auth(self.data, record)
        self.assertEqual(auth.connection_status(self.data)["status"], "expired")
        with patch.object(auth, "_request_json") as request, self.assertRaisesRegex(RuntimeError, "expired"):
            auth.access_token(self.data)
        request.assert_not_called()

    def test_begin_rejects_untrusted_browser_destination(self):
        with patch.object(auth, "_request_json", return_value=dict(device_code="d"*40,
                user_code="ABCD-EFGH", verification_uri="https://evil.test/login", expires_in=900, interval=5)):
            with self.assertRaisesRegex(RuntimeError, "invalid sign-in"):
                auth.begin_sign_in()

    def test_no_auth_requests_can_leave_fixed_github_destinations(self):
        with patch.object(auth, "build_opener") as opened:
            for url in ("https://evil.test/", "http://github.com/login/device/code", "https://github.com.evil.test/login/device/code"):
                with self.assertRaises(ValueError):
                    auth._request_json(url, {"client_id": "fake"})
            with self.assertRaises(ValueError):
                auth._request_json(auth.DEVICE_ENDPOINT, token=ACCESS)
        opened.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "unexpected redirect"):
            auth._NoRedirects().redirect_request(None, None, 302, "", {}, "https://evil.test/")

    def test_provider_error_description_cannot_leak_credentials(self):
        flow = self.flow()
        with patch.object(auth, "_request_json", return_value=dict(error="access_denied", error_description=ACCESS)):
            result = auth.poll_sign_in(self.data, flow)
        self.assertEqual(result["status"], "error")
        self.assertNotIn(ACCESS, json.dumps(result))

    def test_fixed_channel_bypasses_saved_manual_settings(self):
        with patch.object(auth, "access_token", return_value=ACCESS), patch.object(updater, "check_github", return_value={"available": False}) as check, patch.object(updater, "load_config", side_effect=AssertionError("legacy config must not be used")):
            self.assertEqual(updater.check_configured(self.data), {"available": False})
        self.assertEqual(check.call_args.kwargs["channel"], CONFIG)
        self.assertEqual(check.call_args.kwargs["auth_token"], ACCESS)

    def test_download_rejects_foreign_asset_before_token_access(self):
        release = dict(source="github_app", repository=CONFIG["owner"] + "/" + CONFIG["repository"],
            installer={"url": "https://api.github.com/repos/other/repo/releases/assets/5"})
        with patch.object(auth, "access_token") as credential, self.assertRaisesRegex(ValueError, "does not belong"):
            updater.download_configured(self.data, release)
        credential.assert_not_called()


class PublicChannelTests(unittest.TestCase):
    def test_public_channel_needs_no_app_ids_or_credential_reads(self):
        public = dict(schema=1, visibility="public", owner="leanwatts-fixture",
                      repository="testbench-releases", manifest_asset="update-manifest.json")
        with tempfile.TemporaryDirectory() as folder:
            channel = Path(folder) / "release_channel.json"
            channel.write_text(json.dumps(public), encoding="utf-8")
            with patch.object(auth, "CHANNEL_FILE", channel), patch.object(credentials, "has_auth", side_effect=AssertionError("public status must not inspect credentials")), patch.object(auth, "_request_json", side_effect=AssertionError("public status must not use network")):
                self.assertEqual(auth.channel_config(), public)
                self.assertTrue(auth.configured())
                self.assertEqual(auth.connection_status(folder)["status"], "public")
                with self.assertRaisesRegex(RuntimeError, "do not require"):
                    auth.begin_sign_in()

    def test_public_release_check_and_download_are_anonymous(self):
        public = dict(schema=1, visibility="public", owner="leanwatts-fixture",
                      repository="testbench-releases", manifest_asset="update-manifest.json")
        data = b"MZ public test binary"
        release = dict(draft=False, prerelease=False, assets=[
            dict(name="update-manifest.json", id=11, state="uploaded", size=500),
            dict(name="test.exe", id=12, state="uploaded", size=len(data))])
        manifest = dict(schema=1, application=updater.APPLICATION, version="1.1", package_type="full",
            installer=dict(filename="test.exe", asset_name="test.exe", size=len(data), sha256=hashlib.sha256(data).hexdigest()))
        with tempfile.TemporaryDirectory() as folder, patch.object(auth, "channel_config", return_value=public), patch.object(auth, "access_token", side_effect=AssertionError("public channel must not access tokens")), patch.object(credentials, "load_token", side_effect=AssertionError("public channel must not load PATs")), patch.object(credentials, "load_auth", side_effect=AssertionError("public channel must not load auth")), patch.object(updater, "_read_json", side_effect=[release, manifest]) as requested:
            result = updater.check_configured(folder, "1.0")
            self.assertEqual(result["source"], "github_public")
            self.assertTrue(all(call.kwargs["token"] is None for call in requested.call_args_list))
            self.assertEqual(result["installer"]["url"], "https://api.github.com/repos/leanwatts-fixture/testbench-releases/releases/assets/12")
            with patch.object(updater, "download_update", return_value="downloaded") as download:
                self.assertEqual(updater.download_configured(folder, result), "downloaded")
            self.assertIsNone(download.call_args.kwargs["token"])


if __name__ == "__main__":
    unittest.main()
