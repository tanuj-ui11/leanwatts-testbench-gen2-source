"""GD-Link tests use temporary fake volumes; no USB discovery or flashing."""
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from leanwatts_can import gdlink


CONFIG = """# Program config:
Target CPU: ARM
Program Flash start address: 0x08000000
Erase method: Page
Reset method: Software
Read protection: Disable
Sram start address: 0x20000000
Debug interface: SWD
"""


def profile(config=CONFIG):
    return dict(declared_mcu="GD32F303CCT6", flash_start=0x08000000,
                flash_size_kib=128, config_text=config, approved=True)


def hex_record(address, kind, data=b""):
    body = bytes([len(data), address >> 8, address & 255, kind]) + data
    return ":" + (body + bytes([-sum(body) & 255])).hex().upper()


class FormatTests(unittest.TestCase):
    def test_profile_requires_explicit_approval_address_span_and_known_config(self):
        parsed = gdlink.parse_config(CONFIG)
        self.assertEqual(parsed["flash_start"], 0x08000000)
        self.assertEqual(parsed["core"], "ARM")
        self.assertEqual(gdlink.parse_config(CONFIG.replace("Program Flash", "Program")), parsed)
        self.assertEqual(gdlink.validate_profile(profile())["config_text"], CONFIG)
        for key, value in (("approved", False), ("flash_start", 0x08004000),
                           ("flash_size_kib", True), ("flash_size_kib", 0),
                           ("declared_mcu", ""), ("expected_core", "RISC-V")):
            values = profile()
            values[key] = value
            with self.subTest(key=key), self.assertRaises(gdlink.GDLinkError):
                gdlink.validate_profile(values)
        for text in (CONFIG + "Run: erase_all\n", CONFIG + "Program start address: 0x08000000\n",
                     CONFIG.replace("Target CPU: ARM\n", ""), CONFIG.replace("SWD", "JTAG")):
            with self.assertRaises(gdlink.GDLinkError):
                gdlink.parse_config(text)

    def test_bin_bounds_hash_and_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"firmware.bin"
            path.write_bytes(b"\x12\x34")
            image = gdlink.inspect_firmware(path, profile())
            path.write_bytes(b"changed")
            self.assertEqual(image.data, b"\x12\x34")
            self.assertEqual(image.sha256, hashlib.sha256(image.data).hexdigest())
            self.assertEqual(image.ranges, ((0x08000000, 0x08000002),))
            path.write_bytes(bytes(129 * 1024))
            with self.assertRaisesRegex(gdlink.GDLinkError, "outside"):
                gdlink.inspect_firmware(path, profile())

    def test_intel_hex_checks_checksum_eof_overlap_and_absolute_ranges(self):
        header = hex_record(0, 4, bytes.fromhex("0800"))
        data = hex_record(0x10, 0, bytes.fromhex("12345678"))
        end = hex_record(0, 1)
        valid = (header + "\n" + data + "\n" + end + "\n").encode()
        image = gdlink._image_from_bytes("image.hex", valid, profile())
        self.assertEqual(image.ranges, ((0x08000010, 0x08000014),))
        self.assertEqual(image.payload_bytes, 4)
        cases = [(header + "\n" + data + "\n").encode(),
                 valid + (data + "\n").encode(),
                 (header + "\n" + data + "\n" + data + "\n" + end).encode(),
                 (hex_record(0, 4, bytes.fromhex("0900")) + "\n" + data + "\n" + end).encode(),
                 valid.replace(data.encode(), (data[:-2] + "00").encode())]
        for content in cases:
            with self.subTest(content=content), self.assertRaises(gdlink.GDLinkError):
                gdlink._image_from_bytes("image.hex", content, profile())

    def test_discovery_requires_usb_identity_label_signature_and_unambiguous_mapping(self):
        row = dict(root="Z:\\", volume_label="GigaDevice", pnp_id="USBSTOR\\DISK&VEN_GIGADEVICE\\SERIAL123&0", serial_number="SERIAL123")
        with patch.object(gdlink, "_windows_disks", return_value=[row]), patch.object(gdlink, "_read_config", return_value=CONFIG):
            targets = gdlink.discover_programmers()
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0].identity, row["pnp_id"])
        for rows in ([row, row], [dict(row, pnp_id="")], [dict(row, root="C:\\unsafe")]):
            with patch.object(gdlink, "_windows_disks", return_value=rows), patch.object(gdlink, "_read_config", return_value=CONFIG):
                with self.assertRaises(gdlink.GDLinkError):
                    gdlink.discover_programmers()
        with patch.object(gdlink, "_windows_disks", return_value=[dict(row, volume_label="Other USB")]), patch.object(gdlink, "_read_config") as read:
            self.assertEqual(gdlink.discover_programmers(), [])
            read.assert_not_called()


class FakeProgrammer:
    """File mutations after submission simulate the device, not app cleanup."""
    def __init__(self, folder, mode="success"):
        self.root = Path(folder)
        (self.root/"CONFIG.TXT").write_text(CONFIG, encoding="utf-8")
        self.mode, self.now, self.submitted, self.after_count = mode, 0.0, False, 0
        self.writes = []
        self.cancel = threading.Event()
        self.identity = "USBSTOR\\DISK&VEN_GIGADEVICE\\SERIAL123&0"

    def target(self):
        return gdlink.ProgrammerTarget(self.identity, self.root, "GigaDevice", "SERIAL123",
                                       (self.root/"CONFIG.TXT").read_text(encoding="utf-8"))

    def discover(self):
        if not self.submitted:
            return [self.target()]
        self.after_count += 1
        if self.mode == "never_returns":
            return []
        if self.mode != "no_cycle" and self.after_count == 1:
            return []
        # Simulate the V3 virtual filesystem after its programming attempt.
        if (self.root/"firmware.bin").exists():
            (self.root/"firmware.bin").unlink()
        if self.mode == "failure":
            (self.root/"FAIL.TXT").write_text("Target connection failed", encoding="utf-8")
        return [self.target()]

    def write(self, path, data, *, exclusive=False):
        self.writes.append((Path(path).name, bytes(data), exclusive))
        gdlink._write_file(path, data, exclusive=exclusive)
        if exclusive:
            self.submitted = True
            if self.mode == "copy_uncertain":
                raise OSError("device disappeared during flush")
            if self.mode == "cancel_after":
                self.cancel.set()
        elif self.mode == "cancel_before":
            self.cancel.set()

    def sleep(self, seconds):
        self.now += seconds

    def job(self, **kwargs):
        image = gdlink._image_from_bytes("firmware.bin", bytes.fromhex("01020304"), profile())
        options = dict(cancel=self.cancel, discovery=self.discover, clock=lambda: self.now,
                       sleep=self.sleep, writer=self.write, timeout=5)
        options.update(kwargs)
        return gdlink.ProgrammingJob(self.target(), profile(), image, **options)


class ProgrammingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def fixture(self, mode="success"):
        return FakeProgrammer(self.temp.name, mode)

    def test_success_requires_cycle_and_exact_result_and_never_claims_verification(self):
        fixture = self.fixture()
        job = fixture.job()
        result = job.run()
        self.assertEqual(result["status"], "PROGRAMMER_REPORTED_SUCCESS")
        self.assertTrue(result["observed_unmount"] and result["observed_remount"])
        self.assertFalse(result["independent_flash_verification"])
        self.assertFalse(result["target_mcu_verified"])
        self.assertEqual([item[0] for item in fixture.writes], ["CONFIG.TXT", "firmware.bin"])
        with self.assertRaisesRegex(gdlink.GDLinkError, "already attempted"):
            job.run()
        self.assertEqual(len(fixture.writes), 2)

    def test_no_cycle_is_unconfirmed_even_when_only_config_remains(self):
        fixture = self.fixture("no_cycle")
        result = fixture.job().run()
        self.assertEqual(result["status"], "UNCONFIRMED")
        self.assertTrue(result["copy_completed"])
        self.assertFalse(result["observed_unmount"])

    def test_disconnect_without_return_and_uncertain_copy_never_pass(self):
        fixture = self.fixture("never_returns")
        result = fixture.job().run()
        self.assertEqual(result["status"], "UNCONFIRMED")
        self.assertFalse(result["observed_remount"])
        # A separate fake station avoids removing leftovers automatically.
        with tempfile.TemporaryDirectory() as folder:
            fixture = FakeProgrammer(folder, "copy_uncertain")
            result = fixture.job().run()
            self.assertEqual(result["status"], "UNCONFIRMED")
            self.assertTrue(result["observed_remount"])
            self.assertFalse(result["copy_completed"])

    def test_failure_file_after_cycle_is_saved_and_never_deleted(self):
        fixture = self.fixture("failure")
        result = fixture.job().run()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["programmer_failure"], "Target connection failed")
        self.assertTrue((fixture.root/"FAIL.TXT").exists())

    def test_old_failure_and_other_files_block_before_config_or_firmware_write(self):
        fixture = self.fixture()
        (fixture.root/"FAIL.TXT").write_text("old result")
        result = fixture.job().run()
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(result["submitted"])
        self.assertEqual(fixture.writes, [])
        self.assertTrue((fixture.root/"FAIL.TXT").exists())

    def test_cancel_before_and_after_submission_have_different_truthful_results(self):
        fixture = self.fixture("cancel_before")
        result = fixture.job().run()
        self.assertEqual(result["status"], "CANCELLED")
        self.assertFalse(result["submitted"])
        self.assertEqual(len(fixture.writes), 1)
        with tempfile.TemporaryDirectory() as folder:
            fixture = FakeProgrammer(folder, "cancel_after")
            result = fixture.job().run()
            self.assertEqual(result["status"], "PROGRAMMER_REPORTED_SUCCESS")
            self.assertTrue(result["cancel_requested_after_submission"])
            self.assertTrue(result["observed_remount"])

    def test_logging_failure_blocks_before_submission_but_monitoring_continues_after(self):
        fixture = self.fixture()
        def fail_before(event):
            raise OSError("disk full")
        result = fixture.job(event_sink=fail_before).run()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(fixture.writes, [])
        def fail_after(event):
            if event["submitted"]:
                raise OSError("disk full")
        result = fixture.job(event_sink=fail_after).run()
        self.assertEqual(result["status"], "PROGRAMMER_REPORTED_SUCCESS")
        self.assertTrue(result["logging_errors"])

    def test_identity_mismatch_and_ambiguity_prevent_all_writes(self):
        fixture = self.fixture()
        wrong = replace(fixture.target(), identity="USBSTOR\\DISK&VEN_GIGADEVICE\\ANOTHER")
        for targets in ([wrong], [fixture.target(), fixture.target()]):
            result = fixture.job(discovery=lambda: targets).run()
            self.assertEqual(result["status"], "FAILED")
            self.assertFalse(result["submitted"])
        self.assertEqual(fixture.writes, [])

    def test_frozen_image_hash_mismatch_is_rejected_without_discovery(self):
        fixture = self.fixture()
        job = fixture.job()
        with self.assertRaisesRegex(gdlink.GDLinkError, "metadata changed"):
            gdlink.ProgrammingJob(fixture.target(), profile(), replace(job.image, sha256="wrong"),
                                  discovery=lambda: self.fail("Discovery must not run"))
        self.assertEqual(fixture.writes, [])

    def test_fast_observer_catches_removal_during_copy_and_still_requires_same_identity(self):
        fixture = self.fixture("no_cycle")
        class Observer:
            error = ""
            armed = absent = stopped = False
            def start(self):
                return True
            def mark_submitted(self):
                self.armed = True
            def saw_absence(self):
                return self.absent
            def stop(self):
                self.stopped = True
        observer = Observer()
        original_write = fixture.write
        def fast_copy(path, data, **kwargs):
            if kwargs.get("exclusive"):
                self.assertTrue(observer.armed)
                observer.absent = True  # Device disappeared during the copy.
            return original_write(path, data, **kwargs)
        result = fixture.job(writer=fast_copy, observer_factory=lambda root: observer).run()
        self.assertEqual(result["status"], "PROGRAMMER_REPORTED_SUCCESS")
        self.assertTrue(result["observed_remount"])
        self.assertTrue(observer.stopped)
        with tempfile.TemporaryDirectory() as folder:
            fixture = FakeProgrammer(folder, "no_cycle")
            observer = Observer()
            original_write = fixture.write
            original_discover = fixture.discover
            def replacement():
                targets = original_discover()
                return [replace(item, identity="USBSTOR\\DISK&VEN_OTHER\\DIFFERENT") for item in targets] if fixture.submitted else targets
            result = fixture.job(writer=fast_copy, discovery=replacement,
                                 observer_factory=lambda root: observer).run()
            self.assertEqual(result["status"], "UNCONFIRMED")
            self.assertFalse(result["observed_remount"])
            self.assertTrue(observer.stopped)

    def test_fast_observer_does_not_infer_absence_before_submission_or_on_api_failure(self):
        # C remains present while the selected Z drive disappears. All-zero is
        # an API error, not proof that every volume was removed.
        mask = [1 << 2]
        observer = gdlink._DriveAbsenceObserver("Z:\\", mask_reader=lambda: mask[0])
        self.assertTrue(observer.start())
        time.sleep(.05)
        self.assertFalse(observer.saw_absence())
        mask[0] |= 1 << 25
        observer.mark_submitted()
        time.sleep(.05)
        self.assertFalse(observer.saw_absence())
        mask[0] = 1 << 2
        time.sleep(.05)
        self.assertTrue(observer.saw_absence())
        observer.stop()
        self.assertFalse(observer._thread.is_alive())
        failed = gdlink._DriveAbsenceObserver("Z:\\", mask_reader=lambda: 0)
        self.assertFalse(failed.start())
        failed.mark_submitted()
        self.assertFalse(failed.saw_absence())
        failed.stop()


if __name__ == "__main__":
    unittest.main()
