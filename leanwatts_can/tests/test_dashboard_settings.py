"""Dashboard settings: URL and station rules, protected key storage, no raw key on disk. No network."""
import json
import tempfile
import unittest
from pathlib import Path

from leanwatts_can.dashboard_settings import DashboardSettings, KEY_FILE, SETTINGS_FILE, checked_station_name

KEY = "lw_live_looking_test_key_9f8e7d6c5b4a"


def fake_protect(data):
    return b"DPAPI:" + bytes(reversed(data))


def fake_unprotect(data):
    if not data.startswith(b"DPAPI:"):
        raise RuntimeError("cannot unlock")
    return bytes(reversed(data[len(b"DPAPI:"):]))


def no_dpapi(data):
    raise RuntimeError("Private update credentials use Windows account encryption.")


class DashboardSettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.data = Path(self.folder.name)
        self.settings = DashboardSettings(self.data, protect=fake_protect, unprotect=fake_unprotect)

    def tearDown(self):
        self.folder.cleanup()

    def files_containing(self, needle):
        found = []
        for path in self.data.rglob("*"):
            if path.is_file() and needle.encode("ascii") in path.read_bytes():
                found.append(path.name)
        return found

    def test_https_address_is_saved_and_normalised(self):
        self.settings.save(base_url="https://OPS.leanwatts.com/", station_name=" LW-EOL-01 ")
        values = self.settings.settings()
        self.assertEqual(values, {"base_url": "https://ops.leanwatts.com", "station_name": "LW-EOL-01"})

    def test_plain_http_is_refused_for_settings(self):
        for value in ("http://ops.leanwatts.com", "http://127.0.0.1:8080", "http://localhost:8080"):
            with self.assertRaises(ValueError, msg=value):
                self.settings.save(base_url=value, station_name="LW-EOL-01")
        self.assertEqual(self.settings.settings()["base_url"], "")

    def test_loopback_http_only_when_test_flag_is_set(self):
        local = DashboardSettings(self.data, protect=fake_protect, unprotect=fake_unprotect, allow_loopback_http=True)
        local.save(base_url="http://127.0.0.1:8099", station_name="TEST")
        self.assertEqual(local.settings()["base_url"], "http://127.0.0.1:8099")

    def test_paths_credentials_and_other_schemes_are_refused(self):
        for value in ("https://ops.leanwatts.com/eol", "https://a:b@ops.leanwatts.com", "https://ops.leanwatts.com?x=1",
                      "ftp://ops.leanwatts.com", "", "   ", "https://ops.leanwatts.com/ space"):
            with self.assertRaises(ValueError, msg=value):
                self.settings.save(base_url=value, station_name="LW-EOL-01")

    def test_station_name_rules(self):
        self.assertEqual(checked_station_name("  LW-EOL-01 "), "LW-EOL-01")
        for value in ("", "   ", "x" * 65, "bad\tname", None):
            with self.assertRaises(ValueError):
                checked_station_name(value)

    def test_key_is_stored_protected_and_never_on_disk_in_clear(self):
        mode = self.settings.save_api_key(KEY)
        self.assertEqual(mode, "protected")
        self.assertEqual(self.settings.key_storage(), "protected")
        self.assertTrue((self.data / KEY_FILE).is_file())
        self.assertEqual(self.settings.api_key(), KEY)
        self.settings.save(base_url="https://ops.leanwatts.com", station_name="LW-EOL-01")
        self.assertEqual(self.files_containing(KEY), [])
        settings_text = (self.data / SETTINGS_FILE).read_text(encoding="utf-8")
        self.assertNotIn("key", json.loads(settings_text))

    def test_without_windows_protection_key_stays_in_memory_and_nothing_is_written(self):
        unavailable = DashboardSettings(self.data, protect=no_dpapi, unprotect=no_dpapi)
        self.assertEqual(unavailable.save_api_key(KEY), "memory")
        self.assertEqual(unavailable.key_storage(), "memory")
        self.assertFalse((self.data / KEY_FILE).exists())
        self.assertEqual(unavailable.api_key(), KEY)
        self.assertEqual(self.files_containing(KEY), [])
        # A new process has no key until the operator enters it again.
        restarted = DashboardSettings(self.data, protect=no_dpapi, unprotect=no_dpapi)
        self.assertEqual(restarted.key_storage(), "none")

    def test_memory_fallback_removes_a_stale_protected_key(self):
        self.settings.save_api_key(KEY)
        unavailable = DashboardSettings(self.data, protect=no_dpapi, unprotect=no_dpapi)
        unavailable.save_api_key(KEY.replace("9f8e", "0000"))
        self.assertFalse((self.data / KEY_FILE).exists())

    def test_unreadable_protected_key_asks_for_it_again(self):
        self.settings.save_api_key(KEY)
        other_account = DashboardSettings(self.data, protect=fake_protect, unprotect=no_dpapi)
        with self.assertRaises(ValueError) as raised:
            other_account.api_key()
        self.assertNotIn(KEY, str(raised.exception))

    def test_forget_key_removes_file_and_memory(self):
        self.settings.save_api_key(KEY)
        self.settings.forget_key()
        self.assertEqual(self.settings.key_storage(), "none")
        self.assertIsNone(self.settings.api_key())

    def test_client_needs_address_station_and_key(self):
        with self.assertRaises(ValueError):
            self.settings.client()
        self.settings.save(base_url="https://ops.leanwatts.com", station_name="LW-EOL-01")
        with self.assertRaises(ValueError):
            self.settings.client()
        self.settings.save_api_key(KEY)
        self.assertTrue(self.settings.is_configured())
        client = self.settings.client()
        self.assertEqual(client.base_url, "https://ops.leanwatts.com")
        self.assertNotIn(KEY, repr(client))

    def test_invalid_settings_file_reads_as_empty(self):
        (self.data / SETTINGS_FILE).write_text("{broken", encoding="utf-8")
        self.assertEqual(self.settings.settings(), {"base_url": "", "station_name": ""})


if __name__ == "__main__":
    unittest.main()
