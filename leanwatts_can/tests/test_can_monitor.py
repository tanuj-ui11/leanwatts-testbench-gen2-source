"""Hardware-free tests of explicit connection, protocol bounds and failure states."""
import json
import queue
import unittest
from pathlib import Path
from unittest.mock import patch

from leanwatts_can.can_monitor import MonitorClient, MonitorError, PROTOCOL, validate_frame
from leanwatts_can.protocol import command, poll, encode_config, encode_duty, CONFIG


class FakeInput:
    def __init__(self, owner):
        self.owner = owner
        self.closed = False

    def write(self, data):
        request = json.loads(data)
        self.owner.requests.append(request)
        reply = self.owner.reply(request)
        if reply is not None:
            self.owner.responses.put(reply if isinstance(reply, bytes) else json.dumps(reply).encode() + b"\n")
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, responses):
        self.responses = responses
        self.stdin = FakeInput(self)
        self.stdout = None
        self.killed = False
        self.requests = []
        self.state, self.listen_only = "closed", True
        self.transform = None

    def poll(self):
        return 0 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0

    def reply(self, request):
        action, args = request["action"], request["args"]
        if action == "open":
            self.state = "open"
        elif action == "start":
            self.listen_only = args["listen_only"]
            self.state = "listening" if self.listen_only else "active"
        elif action == "reset":
            self.state, self.listen_only = "open", True
        elif action == "close":
            self.state, self.listen_only = "closed", True
        result = {"state": self.state, "listen_only": self.listen_only}
        if action == "receive":
            result.update(frames=[], dropped_malformed=0)
        if action == "send":
            result.update(accepted=1, acknowledged=False)
        response = {"protocol": PROTOCOL, "nonce": request["nonce"], "seq": request["seq"], "ok": True, "result": result}
        return self.transform(response) if self.transform else response


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.client = MonitorClient(timeout=0.1)
        self.process = None

        def spawn():
            self.client._nonce = "a" * 32
            self.client._seq = 0
            self.client._responses = queue.Queue()
            self.process = FakeProcess(self.client._responses)
            self.client._process = self.process

        self.client._spawn = spawn

    def tearDown(self):
        self.client._abort()

    def test_constructor_never_starts_hardware(self):
        with patch("leanwatts_can.can_monitor.subprocess.Popen") as start:
            client = MonitorClient()
            self.assertEqual(client.state, "closed")
            self.assertEqual(client.close()["state"], "closed")
            start.assert_not_called()

    def test_default_start_is_listen_only_and_tx_is_unavailable(self):
        self.client.open()
        self.assertEqual(self.client.start()["state"], "listening")
        before = len(self.process.requests)
        with self.assertRaises(MonitorError):
            self.client.send(command("workStart"))
        self.assertEqual(len(self.process.requests), before)
        self.assertEqual(self.client.receive()["frames"], [])
        self.client.reset()
        self.assertEqual(self.client.state, "open")
        self.client.close()
        self.assertTrue(self.process.killed)

    def test_explicit_normal_mode_acceptance_is_not_acknowledgement(self):
        self.client.open(index=1)
        self.client.start(listen_only=False)
        result = self.client.send(command("shutdown"))
        self.assertEqual(result["accepted"], 1)
        self.assertIs(result["acknowledged"], False)
        self.assertEqual(self.process.requests[-1]["args"]["frame"]["data"], [0, 20, 0, 128])

    def test_whitelist_covers_recovered_frames_and_rejects_altered_shapes(self):
        examples = [command(name) for name in ("workStart", "shutdown", "calibrationStart", "saveVoltage", "saveFullCurrent", "saveHalfCurrent")]
        examples += [poll(n) for n in range(1, 7)]
        examples += [encode_config(name, 1) for name in CONFIG]
        examples += [encode_duty(axis, 80) for axis in ("current", "voltage")]
        for frame in examples:
            self.assertEqual(validate_frame(frame), frame)
        variants = [dict(command("shutdown"), id=1), dict(command("shutdown"), extended=False),
                    dict(command("shutdown"), remote=True), dict(command("shutdown"), error=True),
                    dict(command("shutdown"), data=[False, 20, 0, 128]),
                    dict(command("shutdown"), data=[0, 20, 0, 2]),
                    dict(command("shutdown"), data=[0, 11, 0, 7]),
                    dict(command("shutdown"), data=[0, 0x12, 0, 0]),
                    dict(command("shutdown"), data=[0, 0x0C, 1, 0]),
                    dict(command("shutdown"), data=[0, 0x15, 0xff, 0xff]),
                    dict(command("saveVoltage"), dlc=4)]
        for frame in variants:
            with self.subTest(frame=frame), self.assertRaises(MonitorError):
                validate_frame(frame)

    def test_timeout_closes_connection_without_retry(self):
        self.client.open()
        self.client.start(listen_only=False)
        before = len(self.process.requests)
        self.process.transform = lambda response: None
        with self.assertRaisesRegex(MonitorError, "unknown.*No automatic retry"):
            self.client.send(command("saveVoltage"))
        self.assertEqual(len(self.process.requests), before + 1)
        self.assertTrue(self.process.killed)
        self.assertEqual(self.client.state, "closed")

    def test_wrong_response_nonce_is_fail_closed(self):
        self.client.open()
        self.process.transform = lambda response: dict(response, nonce="different")
        with self.assertRaisesRegex(MonitorError, "did not match"):
            self.client.start()
        self.assertTrue(self.process.killed)

    def test_sdk_error_is_fail_closed(self):
        self.client.open()
        self.process.transform = lambda response: dict(response, ok=False, error="SDK could not start")
        with self.assertRaisesRegex(MonitorError, "SDK could not start"):
            self.client.start()
        self.assertIsNone(self.client._process)

    def test_invalid_receive_payload_is_rejected(self):
        self.client.open()
        self.client.start()
        def corrupt(response):
            response["result"]["frames"] = [{"id": 0x18FF50E5, "extended": True, "remote": False, "error": False,
                                              "dlc": 8, "data": [0, 1], "timestamp_us": 0}]
            return response
        self.process.transform = corrupt
        with self.assertRaisesRegex(MonitorError, "malformed receive"):
            self.client.receive()
        self.assertTrue(self.process.killed)

    def test_unexpected_mode_transition_is_rejected(self):
        self.client.open()
        def corrupt(response):
            response["result"]["listen_only"] = False
            return response
        self.process.transform = corrupt
        with self.assertRaisesRegex(MonitorError, "changed mode"):
            self.client.start()
        self.assertTrue(self.process.killed)

    def test_invalid_selection_and_rate_never_reaches_sdk(self):
        for index, channel in ((True, 0), (32, 0), (0, 1)):
            with self.assertRaises(MonitorError):
                self.client.open(index=index, channel=channel)
        self.assertIsNone(self.process)
        self.client.open()
        with self.assertRaises(MonitorError):
            self.client.start(bitrate=123456)
        self.assertEqual(len(self.process.requests), 1)

    def test_sdk_acceptance_must_be_exactly_one(self):
        self.client.open()
        self.client.start(listen_only=False)
        def corrupt(response):
            response["result"]["accepted"] = 2
            return response
        self.process.transform = corrupt
        with self.assertRaisesRegex(MonitorError, "explicit SDK acceptance"):
            self.client.send(command("workStart"))
        self.assertTrue(self.process.killed)

    def test_non_json_stdout_is_fail_closed(self):
        self.client.open()
        self.process.transform = lambda response: b"unrelated SDK log\n"
        with self.assertRaisesRegex(MonitorError, "communication failed"):
            self.client.start()
        self.assertTrue(self.process.killed)


if __name__ == "__main__":
    unittest.main()
