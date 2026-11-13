"""Offline update integrity/auth tests. No GitHub requests or installer execution."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request

from leanwatts_can import updater, update_credentials


def manifest(version="1.0.1", data=b"MZ local test payload", kind="full", base=None):
    result = {"schema": 1, "application": updater.APPLICATION, "version": version,
              "package_type": kind, "notes": "Test release",
              "installer": {"filename": "test-setup.exe", "asset_name": "test-setup.exe",
                            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
    if base:
        result["base_version"] = base
    return result


class UpdateTests(unittest.TestCase):
    def test_auth_is_removed_on_external_asset_redirect(self):
        request = Request("https://api.github.com/repos/company/release/releases/assets/3",
                          headers={"Authorization": "Bearer fake-secret", "Accept": "application/octet-stream"})
        redirected = updater.HTTPSRedirects().redirect_request(request, None, 302, "", {},
                                                                "https://release-assets.githubusercontent.com/file?x=1")
        self.assertFalse(any(key.lower() == "authorization" for key in redirected.headers))
        self.assertFalse(any(key.lower() == "authorization" for key in redirected.unredirected_hdrs))

    def test_auth_may_stay_on_github_api_https_origin(self):
        request = Request("https://api.github.com/repos/company/old/releases/assets/3",
                          headers={"Authorization": "Bearer fake-secret"})
        redirected = updater.HTTPSRedirects().redirect_request(request, None, 302, "", {},
                                                                "https://api.github.com/repos/company/new/releases/assets/3")
        self.assertEqual(redirected.get_header("Authorization"), "Bearer fake-secret")

    def test_redirect_cannot_downgrade_https(self):
        with self.assertRaises(ValueError):
            updater.HTTPSRedirects().redirect_request(Request("https://api.github.com/x"), None, 302, "", {}, "http://other.test/x")

    def test_token_cannot_be_sent_to_another_host_or_port(self):
        for url in ("https://example.test/x", "https://api.github.com.evil.test/x", "https://api.github.com:8443/x"):
            with self.subTest(url=url), self.assertRaises(ValueError), patch.object(updater, "build_opener") as opened:
                updater._open(url, token="fake-secret")
            opened.assert_not_called()

    def test_matching_delta_is_preferred_with_full_fallback(self):
        full = manifest()
        delta = manifest(kind="delta", base="1.0")
        delta["installer"]["filename"] = delta["installer"]["asset_name"] = "delta.exe"
        release = {**full, "packages": [full, delta]}
        self.assertEqual(updater._release(release, "1.0", needs_url=False)["package_type"], "delta")
        self.assertEqual(updater._release(release, "0.9", needs_url=False)["package_type"], "full")

    def test_wrong_product_and_delta_base_are_rejected(self):
        wrong = manifest()
        wrong["application"] = "different-app"
        with self.assertRaises(ValueError):
            updater._release(wrong, needs_url=False)
        with self.assertRaisesRegex(ValueError, "different installed base"):
            updater._release(manifest(kind="delta", base="0.9"), "1.0", needs_url=False)

    def test_local_inspection_checks_bytes_and_allows_same_version(self):
        data = b"MZ local test payload"
        with tempfile.TemporaryDirectory() as folder:
            setup = Path(folder) / "test-setup.exe"
            setup.write_bytes(data)
            companion = setup.with_suffix(".update.json")
            companion.write_text(json.dumps(manifest("1.0")))
            release = updater.local_update(setup, current_version="1.0")
            self.assertFalse(release["available"])
            self.assertEqual(release["source"], "local")
            setup.write_bytes(data[:-1] + b"x")
            with self.assertRaisesRegex(ValueError, "checksum changed"):
                updater.local_update(setup, current_version="1.0")

    def test_local_downgrade_and_renamed_binary_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            setup = Path(folder) / "test-setup.exe"
            setup.write_bytes(b"MZ local test payload")
            companion = setup.with_suffix(".update.json")
            companion.write_text(json.dumps(manifest("0.9")))
            with self.assertRaisesRegex(ValueError, "older"):
                updater.local_update(setup)
            companion.write_text(json.dumps(manifest("1.0.1")))
            renamed = setup.with_name("renamed.exe")
            setup.rename(renamed)
            with self.assertRaisesRegex(ValueError, "filename"):
                updater.local_update(renamed, companion, current_version="1.0")

    def test_private_release_resolves_assets_and_does_not_retain_token(self):
        secret = "github_pat_FAKE_FOR_UNIT_TEST_ONLY"
        private = {"draft": False, "prerelease": False, "assets": [
            {"name": "update-manifest.json", "id": 10, "state": "uploaded", "size": 1000,
             "url": "https://evil.test/ignored"},
            {"name": "test-setup.exe", "id": 11, "state": "uploaded", "size": 21,
             "url": "https://evil.test/ignored"}]}
        with tempfile.TemporaryDirectory() as folder:
            updater.save_config(folder, source="github_private", owner="company", repository="release")
            with patch.object(update_credentials, "load_token", return_value=secret), patch.object(updater, "_read_json", side_effect=[private, manifest()]) as fetched:
                result = updater.check_github(folder)
            self.assertEqual(result["installer"]["url"], "https://api.github.com/repos/company/release/releases/assets/11")
            self.assertNotIn(secret, json.dumps(result))
            self.assertEqual(fetched.call_args_list[1].args[0], "https://api.github.com/repos/company/release/releases/assets/10")
            self.assertEqual(fetched.call_args_list[1].kwargs["token"], secret)

    def test_failed_download_hash_does_not_leave_installer_or_partial(self):
        release = updater._release(manifest(), needs_url=False)
        release["installer"]["url"] = "https://example.test/setup.exe"
        response = io.BytesIO(b"MZ local test payloax")
        response.headers = {}
        with tempfile.TemporaryDirectory() as folder, patch.object(updater, "_open", return_value=response):
            with self.assertRaisesRegex(ValueError, "failed"):
                updater.download_update(release, folder)
            self.assertEqual(list(Path(folder).iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_windows_credential_roundtrip_is_not_plaintext(self):
        token = "github_pat_FAKE_FOR_LOCAL_DPAPI_TEST_ONLY"
        with tempfile.TemporaryDirectory() as folder:
            update_credentials.save_token(folder, token)
            encrypted = (Path(folder) / update_credentials.TOKEN_FILE).read_bytes()
            self.assertNotIn(token.encode(), encrypted)
            self.assertEqual(update_credentials.load_token(folder), token)
            update_credentials.remove_token(folder)
            self.assertFalse(update_credentials.has_token(folder))


if __name__ == "__main__":
    unittest.main()
