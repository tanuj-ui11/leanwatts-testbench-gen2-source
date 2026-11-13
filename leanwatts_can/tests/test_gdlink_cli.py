"""Deterministic fake console/USB tests; never connect or flash hardware."""
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from leanwatts_can import gdlink_cli as cli


SERIAL = "36390B7A0257"
TARGET = cli.CLITarget("USB\\VID_28E9&PID_0698\\" + SERIAL, SERIAL)
IDENTITY = ("Current SN  36390B7A0257\nTarget Device: GD32F103C8T6, Flash Size: 64KB, SRAM Size: 20KB\n"
            "Connect successfully.\n")


def tool():
    return dict(cli_path="C:/vendor/GDLink_CLI.exe", cli_sha256=cli.TOOL_FILES["GDLink_CLI.exe"],
                cli_version=cli.CLI_VERSION, tool_files=dict(cli.TOOL_FILES))


def profile():
    return dict(tool(), approved=True, declared_mcu="GD32F103C8T6", flash_start=0x08000000,
                flash_size_kib=64, speed_khz=1000, programmer_serial=SERIAL)


class FakeConsole:
    def __init__(self, root, mode="success"):
        self.root, self.mode = Path(root), mode
        self.cancel = threading.Event()
        self.calls = []
        self.data = bytes(range(256)) * 256
        self.image = cli._image_from_bytes("source with spaces.bin", self.data, profile())

    def run(self, args, *, cwd, timeout, output_path):
        script = Path(args[-1]).read_text(encoding="ascii")
        phase = Path(args[-1]).stem
        self.calls.append((args, script, cwd))
        self.assertions = args[1:5] == ["-speed", "1000", "-commandfile", "-e"]
        if phase == "backup":
            if self.mode != "missing_backup":
                (Path(cwd) / "backup.bin").write_bytes(bytes(len(self.data)))
            if self.mode == "protected":
                return cli.CLIExecution(33, "Read protection enabled\n")
            if self.mode == "wrong_target":
                return cli.CLIExecution(0, IDENTITY.replace("C8T6", "CBT6"))
            if self.mode == "missing_identity":
                return cli.CLIExecution(0, "Connected\n")
            if self.mode == "cancel_backup":
                self.cancel.set()
            return cli.CLIExecution(0, IDENTITY)
        if self.mode == "timeout":
            return cli.CLIExecution(None, IDENTITY, True, True)
        if self.mode == "nonzero":
            return cli.CLIExecution(33, IDENTITY + "Memory write successfully. Total time needed: 7.000s\n")
        if self.mode != "missing_readback":
            data = self.data[:-1] if self.mode == "short_readback" else self.data
            if self.mode == "mismatch":
                data = bytes(len(data))
            (Path(cwd) / "readback.bin").write_bytes(data)
        output = IDENTITY + ("Unrecognized result\n" if self.mode == "no_success" else "Memory write successfully. Total time needed: 7.000s\n")
        if self.mode == "cancel_after":
            self.cancel.set()
        return cli.CLIExecution(0, output)

    def job(self, **kwargs):
        options = dict(cancel=self.cancel, runner=self.run, discovery=lambda: [TARGET],
                       tool_inspector=lambda path: tool(), workspace_factory=lambda: self.root / "fresh",
                       evidence_dir=self.root / "evidence", ac_off_confirmed=True)
        options.update(kwargs)
        return cli.ProgrammingJob(TARGET, profile(), self.image, **options)


class ProfileTests(unittest.TestCase):
    def test_discovery_uses_exact_parent_usb_identity_without_drive(self):
        rows = [dict(identity=TARGET.identity, status="OK", description="GD-Link"),
                dict(identity="USB\\VID_28E9&PID_0698&MI_00\\" + SERIAL, status="OK"),
                dict(identity="USB\\VID_1234&PID_0698\\OTHER123", status="OK")]
        with patch.object(cli, "_windows_usb", return_value=rows):
            self.assertEqual(cli.discover_programmers(), [replace(TARGET, description="GD-Link")])
        with patch.object(cli, "_windows_usb", return_value=[rows[0], rows[0]]):
            with self.assertRaisesRegex(cli.GDLinkError, "duplicate"):
                cli.discover_programmers()

    def test_profile_rejects_script_injection_and_unapproved_tool(self):
        for key, value in [("programmer_serial", SERIAL + "\nload evil.bin"), ("approved", False),
                           ("declared_mcu", "GD32F103C8T6\nq"), ("speed_khz", True),
                           ("speed_khz", 999999), ("flash_size_kib", True), ("flash_start", 0x20000000),
                           ("cli_sha256", "forged"), ("tool_files", {}),
                           ("cli_version", "2.1.10.38023")]:
            values = profile()
            values[key] = value
            with self.subTest(key=key), self.assertRaises(cli.GDLinkError):
                cli.validate_profile(values)

    def test_generated_commands_are_fixed_and_exclude_erase_and_protection_changes(self):
        expected = "c 36390B7A0257\nsi 1\nload input.bin 8000000\nsavebin readback.bin 8000000 10000\nq\n"
        self.assertEqual(cli.command_file(profile(), "program"), expected)
        self.assertEqual(cli.command_file(profile(), "backup"),
                         "c 36390B7A0257\nsi 1\nsavebin backup.bin 8000000 10000\nq\n")
        for value in ("erase", "setOPT", "setRDP", "writeOTP", "\nr\n", "\ng\n"):
            self.assertNotIn(value, expected)

    def test_full_image_required_and_file_data_frozen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "firmware.bin"
            path.write_bytes(bytes(65536))
            image = cli.inspect_firmware(path, profile())
            path.write_bytes(b"different")
            self.assertEqual(image.data, bytes(65536))
            self.assertEqual(image.sha256, hashlib.sha256(image.data).hexdigest())
            with self.assertRaisesRegex(cli.GDLinkError, "complete approved"):
                cli.inspect_firmware(path, profile())
            with self.assertRaisesRegex(cli.GDLinkError, "complete .bin"):
                cli._image_from_bytes("image.hex", bytes(65536), profile())

    def test_identity_cannot_be_inferred_from_echoed_script_or_conflicting_fields(self):
        self.assertEqual(cli.parse_identity(IDENTITY), "GD32F103C8T6")
        for value in ("sd GD32F103C8T6", "Connect success", IDENTITY + IDENTITY.replace("C8T6", "CBT6")):
            with self.assertRaises(cli.GDLinkError):
                cli.parse_identity(value)

    def test_live_identity_requires_selected_serial_flash_size_and_connection(self):
        self.assertEqual(cli.validate_connected_identity(IDENTITY, profile()), "GD32F103C8T6")
        for output in (IDENTITY.replace(SERIAL, "123456789ABC"), IDENTITY.replace("64KB", "128KB"),
                       IDENTITY.replace("Connect successfully.", "Connection unsuccessful.")):
            with self.assertRaises(cli.GDLinkError):
                cli.validate_connected_identity(output, profile())

    def test_tool_inspection_requires_official_complete_unmodified_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "GDLink_CLI.exe"
            path.write_bytes(b"not a vendor program")
            with self.assertRaisesRegex(cli.GDLinkError, "does not match"):
                cli.inspect_cli_tool(path)


class ProgrammingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_pass_requires_backup_fresh_identity_success_and_byte_exact_readback(self):
        fake = FakeConsole(self.temp.name)
        job = fake.job()
        result = job.run()
        self.assertEqual(result["status"], "VERIFIED", result)
        self.assertTrue(result["independent_readback_verified"])
        self.assertTrue(result["target_mcu_verified"])
        self.assertTrue(result["console_closed"])
        self.assertFalse(result["disconnect_acknowledged"])
        self.assertEqual(result["readback_sha256"], fake.image.sha256)
        self.assertEqual(len(fake.calls), 2)
        self.assertTrue(fake.assertions)
        self.assertTrue((Path(self.temp.name) / "evidence" / "backup.bin").exists())
        with self.assertRaisesRegex(cli.GDLinkError, "already attempted"):
            job.run()
        self.assertEqual(len(fake.calls), 2)

    def test_no_attach_without_ac_off_confirmation_or_on_prestart_cancel(self):
        fake = FakeConsole(self.temp.name)
        self.assertEqual(fake.job(ac_off_confirmed=False).run()["status"], "FAILED")
        self.assertEqual(fake.calls, [])
        fake.cancel.set()
        self.assertEqual(fake.job().run()["status"], "CANCELLED")
        self.assertEqual(fake.calls, [])

    def test_preflight_failure_never_submits_firmware_or_unlocks(self):
        for mode in ("wrong_target", "missing_identity", "missing_backup", "protected", "cancel_backup"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                fake = FakeConsole(folder, mode)
                result = fake.job().run()
                self.assertFalse(result["submitted"])
                self.assertFalse(result["independent_readback_verified"])
                self.assertEqual(len(fake.calls), 1)
                self.assertNotIn("load input", fake.calls[0][1])

    def test_unconfirmed_after_any_missing_or_mismatched_programming_evidence(self):
        for mode in ("missing_readback", "short_readback", "mismatch", "no_success", "nonzero", "timeout"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                fake = FakeConsole(folder, mode)
                result = fake.job().run()
                self.assertEqual(result["status"], "UNCONFIRMED")
                self.assertTrue(result["submitted"])
                self.assertFalse(result["independent_readback_verified"])
                self.assertEqual(len(fake.calls), 2)
                if mode == "timeout":
                    self.assertTrue(result["process_running"])
                    self.assertFalse(result["console_closed"])

    def test_stop_after_submission_does_not_interrupt_verification(self):
        fake = FakeConsole(self.temp.name, "cancel_after")
        self.assertEqual(fake.job().run()["status"], "VERIFIED")
        self.assertEqual(len(fake.calls), 2)

    def test_disappeared_or_changed_programmer_and_tool_stop_before_write(self):
        fake = FakeConsole(self.temp.name)
        result = fake.job(discovery=lambda: []).run()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(fake.calls, [])
        result = fake.job(tool_inspector=lambda path: dict(tool(), cli_sha256="changed")).run()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(fake.calls, [])

    def test_stale_workspace_rejected_before_any_console_process(self):
        fake = FakeConsole(self.temp.name)
        folder = Path(self.temp.name) / "fresh"
        folder.mkdir()
        (folder / "readback.bin").write_bytes(fake.data)
        self.assertEqual(fake.job().run()["status"], "FAILED")
        self.assertEqual(fake.calls, [])

    def test_pending_process_blocks_new_job_without_termination(self):
        class StillRunning:
            def poll(self):
                return None
        fake = FakeConsole(self.temp.name)
        with patch.object(cli, "_pending_processes", [StillRunning()]):
            result = fake.job().run()
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("still running", result["detail"])
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
