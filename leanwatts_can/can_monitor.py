"""Explicit live CAN operations via the supplied x86 SDK in a bounded subprocess.

Constructing this client does nothing. The GUI must call open explicitly. SDK
acceptance is never a charger acknowledgement or calibration persistence proof.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import uuid

from .protocol import COMMAND_ID

PROTOCOL = "leanwatts-can/1"
BITRATES = (1000000, 800000, 500000, 250000, 125000, 100000, 50000, 20000, 10000, 5000)
MAX_REPLY = 524288


class MonitorError(RuntimeError):
    pass


def validate_frame(frame):
    if not isinstance(frame, dict) or type(frame.get("id")) is not int or frame["id"] != COMMAND_ID:
        raise MonitorError("Only the recovered charger command ID is permitted.")
    if frame.get("extended") is not True or frame.get("remote") is not False or frame.get("error", False) is not False:
        raise MonitorError("Expected an extended CAN data frame.")
    data, dlc = frame.get("data"), frame.get("dlc")
    if (type(dlc) is not int or dlc not in (2, 4) or not isinstance(data, (list, tuple, bytes, bytearray))
            or len(data) != dlc or any(type(n) is not int or not 0 <= n <= 255 for n in data) or data[0] != 0):
        raise MonitorError("Invalid charger command bytes.")
    selector = data[1]
    if dlc == 2:
        valid = 0x17 <= selector <= 0x19
    else:
        word = data[2] * 256 + data[3]
        valid = ((1 <= selector <= 0x11 and selector != 0x0B and word <= (255 if selector == 0x0C else 32767))
                 or (selector == 0x0B and 1 <= word <= 6)
                 or (selector == 0x14 and word in (0, 1, 128))
                 or (selector in (0x15, 0x16) and word <= 10000))
    if not valid:
        raise MonitorError("Command is outside the recovered protocol whitelist.")
    return {"id": COMMAND_ID, "extended": True, "remote": False, "dlc": dlc, "data": list(data)}


class MonitorClient:
    def __init__(self, executable=None, sdk_dir=None, timeout=5.0):
        default = Path(__file__).resolve().parent.parent / "vendor_can"
        self.executable = Path(executable) if executable is not None else default / "CanReceiveBridge.exe"
        self.sdk_dir = Path(sdk_dir) if sdk_dir is not None else default
        if type(timeout) not in (int, float) or not 0.1 <= timeout <= 30:
            raise ValueError("Timeout must be between 0.1 and 30 seconds.")
        self.timeout = float(timeout)
        self._lock = threading.RLock()
        self._process = None
        self._responses = None
        self._seq = 0
        self._nonce = None
        self.state = "closed"
        self.listen_only = True

    @staticmethod
    def _read_responses(process, responses):
        try:
            while True:
                line = process.stdout.readline(MAX_REPLY + 1)
                if not line:
                    responses.put_nowait(MonitorError("CAN bridge exited before responding."))
                    return
                if len(line) > MAX_REPLY or not line.endswith(b"\n"):
                    responses.put_nowait(MonitorError("CAN bridge returned an oversized or incomplete response."))
                    return
                responses.put_nowait(line)
        except (OSError, ValueError, queue.Full):
            # A full response queue is itself a protocol failure; the parent
            # rejects unexpected sequence/nonce or times out and closes it.
            return

    def _spawn(self):
        if os.name != "nt":
            raise MonitorError("The supplied USB-CAN SDK requires Windows.")
        if not self.executable.is_file():
            raise MonitorError("The x86 CAN bridge is missing. Extract the complete live-CAN package.")
        if not (self.sdk_dir / "zlgcan.dll").is_file():
            raise MonitorError("The supplied CAN SDK is missing from vendor_can.")
        self._nonce = uuid.uuid4().hex
        self._seq = 0
        self._responses = queue.Queue(maxsize=4)
        try:
            self._process = subprocess.Popen(
                [str(self.executable.resolve()), "--sdk-dir", str(self.sdk_dir.resolve())],
                cwd=str(self.sdk_dir.resolve()), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                bufsize=0,
            )
        except OSError as error:
            raise MonitorError("Windows could not start the CAN bridge: " + str(error)) from error
        threading.Thread(target=self._read_responses, args=(self._process, self._responses), daemon=True).start()

    def _abort(self):
        process, self._process = self._process, None
        self.state, self.listen_only = "closed", True
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for stream in (process.stdin, process.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass

    def _request(self, action, **args):
        if self._process is None:
            raise MonitorError("CAN bridge is not open.")
        self._seq += 1
        request = {"protocol": PROTOCOL, "nonce": self._nonce, "seq": self._seq, "action": action, "args": args}
        try:
            self._process.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
            self._process.stdin.flush()
            raw = self._responses.get(timeout=self.timeout)
            if isinstance(raw, Exception):
                raise raw
            reply = json.loads(raw.decode("utf-8"))
            if (not isinstance(reply, dict) or reply.get("protocol") != PROTOCOL or reply.get("nonce") != self._nonce
                    or type(reply.get("seq")) is not int or reply["seq"] != self._seq or type(reply.get("ok")) is not bool):
                raise MonitorError("CAN bridge response did not match the request.")
            if not reply["ok"]:
                raise MonitorError(str(reply.get("error", "CAN SDK operation failed.")))
            result = reply.get("result")
            if not isinstance(result, dict) or result.get("state") not in ("open", "closed", "listening", "active"):
                raise MonitorError("CAN bridge response state is invalid.")
            expected = {"open": "open", "reset": "open", "close": "closed", "start": "listening" if args.get("listen_only") else "active"}
            if action in expected and result["state"] != expected[action]:
                raise MonitorError("CAN bridge returned an unexpected state.")
            if type(result.get("listen_only")) is not bool:
                raise MonitorError("CAN bridge response mode is invalid.")
            if (action == "start" and result["listen_only"] != args["listen_only"]
                    or action in ("send", "receive") and (result["state"] != self.state or result["listen_only"] != self.listen_only)
                    or action in ("open", "reset", "close") and result["listen_only"] is not True):
                raise MonitorError("CAN bridge changed mode unexpectedly.")
            self.state, self.listen_only = result["state"], result["listen_only"]
            return result
        except queue.Empty as error:
            self._abort()
            raise MonitorError("CAN bridge timed out. Connection closed; charger state is unknown. No automatic retry was made.") from error
        except (OSError, ValueError, TypeError, MonitorError) as error:
            self._abort()
            if isinstance(error, MonitorError):
                raise
            raise MonitorError("CAN bridge communication failed: " + str(error)) from error

    def open(self, index=0, channel=0):
        with self._lock:
            if type(index) is not int or not 0 <= index <= 31 or type(channel) is not int or channel != 0:
                raise MonitorError("USBCAN1 supports device indexes 0..31 and channel 0.")
            if self._process is not None:
                raise MonitorError("Device is already open; close it before reopening.")
            self._spawn()
            return self._request("open", index=index, channel=channel)

    def start(self, bitrate=250000, listen_only=True):
        with self._lock:
            if type(bitrate) is not int or bitrate not in BITRATES or type(listen_only) is not bool:
                raise MonitorError("Unsupported bitrate or CAN mode.")
            if self.state != "open":
                raise MonitorError("Open or reset CAN before starting it.")
            return self._request("start", bitrate=bitrate, listen_only=listen_only)

    def receive(self):
        with self._lock:
            if self.state not in ("listening", "active"):
                raise MonitorError("Start CAN before receiving.")
            result = self._request("receive")
            frames = result.get("frames")
            valid = isinstance(frames, list) and len(frames) <= 256
            if valid:
                for frame in frames:
                    valid = (isinstance(frame, dict) and type(frame.get("id")) is int and 0 <= frame["id"] <= 0x1fffffff
                             and all(type(frame.get(key)) is bool for key in ("extended", "remote", "error"))
                             and (frame["extended"] or frame["id"] <= 0x7ff)
                             and type(frame.get("dlc")) is int and 0 <= frame["dlc"] <= 8
                             and isinstance(frame.get("data"), list) and len(frame["data"]) == frame["dlc"]
                             and all(type(byte) is int and 0 <= byte <= 255 for byte in frame["data"])
                             and type(frame.get("timestamp_us")) is int and 0 <= frame["timestamp_us"] <= 0xffffffffffffffff)
                    if not valid:
                        break
            if not valid or type(result.get("dropped_malformed")) is not int or result["dropped_malformed"] < 0:
                self._abort()
                raise MonitorError("CAN bridge returned malformed receive data.")
            return result

    def send(self, frame):
        validated = validate_frame(frame)
        with self._lock:
            if self.state != "active" or self.listen_only:
                raise MonitorError("Real transmit requires explicitly starting normal CAN mode.")
            result = self._request("send", frame=validated)
            if type(result.get("accepted")) is not int or result["accepted"] != 1 or result.get("acknowledged") is not False:
                self._abort()
                raise MonitorError("CAN bridge did not report explicit SDK acceptance.")
            return result

    def reset(self):
        with self._lock:
            return self._request("reset")

    def close(self):
        with self._lock:
            if self._process is None:
                return {"state": "closed", "listen_only": True}
            try:
                return self._request("close")
            finally:
                self._abort()
