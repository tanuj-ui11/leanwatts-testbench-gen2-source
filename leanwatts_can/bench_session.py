"""Exclusive real bench adapter. Construction performs no instrument I/O.

The GUI must explicitly close its manual CAN session and reserve the fixture
before setting exclusive_confirmed. All I/O methods run on one dedicated worker;
request_abort only sets a thread-safe cancellation flag and performs no I/O.
"""
from __future__ import annotations

import copy
import math
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

from .automation import AutomationReading, AutomationSettings, BenchCapabilities
from .can_monitor import BITRATES, MonitorClient
from .maynuo import LoadIdentity, LoadReading
from .protocol import CONFIG, command, decode, encode_duty, encode_config, encode_all_config, poll


class BenchSessionError(RuntimeError):
    pass


class TraceCaptureError(BenchSessionError):
    """A trace-storage failure, not a CAN-adapter failure."""


class BenchSession:
    _claims_lock = threading.Lock()
    _claims = set()

    def __init__(self, load_factory, *, monitor_factory=None, can_index=0, bitrate=250000,
                 settings=None, protocol_approved=False, cc_mode_code=None, cv_mode_code=None,
                 standalone_ready=False, exclusive_confirmed=False, termination_ramp_approved=False,
                 cancellation_event=None, clock=None, post_test_can_start=False,
                 charger_parameters=None, test_start_method="external_restart", trace_sink=None):
        if not callable(load_factory) or (monitor_factory is not None and not callable(monitor_factory)):
            raise ValueError("Supply instrument factories; construction must not open hardware.")
        if type(can_index) is not int or not 0 <= can_index <= 31 or type(bitrate) is not int or bitrate not in BITRATES:
            raise ValueError("Choose a supported CAN adapter index and bitrate.")
        for flag in (protocol_approved, standalone_ready, exclusive_confirmed, termination_ramp_approved, post_test_can_start):
            if type(flag) is not bool:
                raise ValueError("Commissioning and exclusive-control confirmations must be explicit booleans.")
        if test_start_method != "external_restart":
            raise ValueError("Tests now require operator AC restart; charger commands and temporary parameter changes are disabled.")
        if post_test_can_start:
            raise ValueError("Test CAN Start is retired. Arm Test, then restart charger AC.")
        if trace_sink is not None and not callable(trace_sink):
            raise ValueError("The CAN trace sink must be callable.")
        self.load_factory = load_factory
        self.monitor_factory = monitor_factory or MonitorClient
        self.can_index, self.bitrate = can_index, bitrate
        self.settings = settings if settings is not None else AutomationSettings()
        if not isinstance(self.settings, AutomationSettings):
            raise ValueError("Supply validated automation commissioning settings.")
        self.protocol_approved = protocol_approved
        self.cc_mode_code, self.cv_mode_code = cc_mode_code, cv_mode_code
        self.standalone_ready = standalone_ready
        self.exclusive_confirmed = exclusive_confirmed
        self.termination_ramp_approved = termination_ramp_approved
        self.post_test_can_start = post_test_can_start
        self.test_start_method = test_start_method
        self.trace_sink = trace_sink
        self.capture_id = uuid.uuid4().hex
        self.trace_phase = ""
        self.trace_events = []
        self.trace_capture_complete = True
        self.trace_capture_error = ""
        self.charger_parameters = copy.deepcopy(charger_parameters)
        if charger_parameters is not None:
            encode_all_config(charger_parameters)
        self.preflight_result = None
        self.startup_can_tx_count = 0
        self.test_setup_can_tx_count = 0
        self.cleanup_can_tx_count = 0
        self.test_parameter_restore_result = dict(status="NOT_REQUIRED", required=False, restored=None,
            parameters_changed=False, restoration_required=False)
        self.load_transition_events = []
        self._test_control_scope = ""
        self._test_setup_attempted = False
        self._test_setup_verified = False
        self._test_parameter_dirty = False
        self._test_cleanup_attempted = False
        self.startup_events = []
        self._load_configuration_version = 0
        self._post_started_version = -1
        self._clock = clock or time.monotonic
        self._abort = cancellation_event if cancellation_event is not None else threading.Event()
        if not all(callable(getattr(self._abort, name, None)) for name in ("is_set", "set", "wait")):
            raise ValueError("Supply a thread-safe cancellation event.")
        self._load = self._can = None
        self._owned = False
        self._thread = None
        self._operation = ""
        self._passive = True
        self._can_failed = False
        self._claimed = set()
        self._identity = None
        self._resource = ""
        self._reference = ""
        self._probe_valid = False
        self._probe_commissioned = False
        self._ramp_supported = False
        self.probe_result = None
        self.can_tx_count = 0             # attempts, including uncertain/failed sends
        self.can_accepted_count = 0
        self.can_trace = []
        self.can_observations = {}
        self._can_observed_at = {}
        self._last_load_sequence = None
        self._applied_duties = None
        self._started = False
        self._baseline_started = False
        self._baseline_start_attempted = False
        self._shutdown_accepted = False
        self._save_attempts = set()
        self._load_configuration = None

    def _mode_codes_valid(self):
        return (type(self.cc_mode_code) is int and type(self.cv_mode_code) is int
                and 0 <= self.cc_mode_code <= 65535 and 0 <= self.cv_mode_code <= 65535
                and self.cc_mode_code != self.cv_mode_code)

    def readiness(self, operation="calibration"):
        if not self.protocol_approved:
            return "Confirm this load's M97-compatible protocol before automatic control."
        if not self._mode_codes_valid():
            return "Enter distinct CC/CV readback codes observed on this load; command numbers are not assumed."
        if not self._probe_valid or not self._probe_commissioned:
            return "Run the read-only load probe with the approved mode mapping first."
        if not self.exclusive_confirmed:
            return "Close the manual CAN adapter and reserve the complete fixture before automatic control."
        if operation not in ("calibration", "test"):
            return "Choose calibration or standalone test."
        problem = self.settings.validate(calibration=operation == "calibration")
        if problem:
            return problem
        if operation == "test" and not (self.standalone_ready or self.post_test_can_start):
            return "Confirm external standalone charger startup; tests cannot send CAN start/stop commands."
        return ""

    @property
    def capabilities(self):
        base = (self.protocol_approved and self._mode_codes_valid() and self._probe_valid
                and self._probe_commissioned and self.exclusive_confirmed)
        reason = "" if base else self.readiness("calibration")
        return BenchCapabilities(
            ready=bool(base), reason=reason, simulation=False,
            verified_load_protocol=bool(self.protocol_approved and self._probe_commissioned),
            independent_measurements=self._probe_valid, exclusive_control=self.exclusive_confirmed,
            calibration_commands=bool(base and not self.settings.validate(calibration=True)),
            passive_can=True, standalone_ready=self.standalone_ready,
            reference_identity=self._reference,
            termination_ramp=bool(base and self.termination_ramp_approved and self._ramp_supported),
            post_test_can_start=self.post_test_can_start,
            test_start_method=self.test_start_method,
        )

    def _trace_event(self, direction, *, action, frame=None, outcome="", event="frame", correlates_to=None,
                     allow_capture_failure=False):
        """Record host acquisition time, preserving the adapter timestamp separately.

        A TX attempt is durably offered to the sink before touching the adapter.
        Its result is a separate TX_RESULT event, never a second transmitted frame.
        Sink failure stops progression; no successful-capture claim is invented.
        """
        item = dict(capture_id=self.capture_id, event_id=len(self.trace_events) + 1, event=event,
                    timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    monotonic=self._clock(), direction=direction, operation=self._operation,
                    phase=self.trace_phase or self._operation, action=action, outcome=outcome)
        if correlates_to is not None:
            item["correlates_to"] = correlates_to
        if frame is not None:
            item.update({key: copy.deepcopy(frame.get(key)) for key in
                         ("id", "extended", "remote", "dlc", "data", "timestamp_us")})
            item["error"] = frame.get("error", False)
        self.trace_events.append(item)
        if self.trace_sink is not None:
            try:
                self.trace_sink(copy.deepcopy(item))
            except Exception as exc:
                item["capture_error"] = str(exc)
                self.trace_capture_complete = False
                self.trace_capture_error = str(exc)
                if not allow_capture_failure:
                    raise TraceCaptureError("CAN trace could not be saved; further commands were stopped: " + str(exc)) from exc
        return item

    @staticmethod
    def _confirmed(result, action):
        if result is not True:
            raise BenchSessionError(action + " was not confirmed by the load driver.")

    @staticmethod
    def _resource_for(load):
        config = getattr(load, "settings", None)
        port = getattr(config, "port", "")
        address = getattr(config, "address", "")
        if (not isinstance(port, str) or not port.strip()
                or type(address) is not int or not 1 <= address <= 200):
            raise BenchSessionError("The independent load reference must identify its selected serial port.")
        return f"{port.strip().upper()} / address {address}"

    def _identity_for(self, load, identity):
        if not isinstance(identity, LoadIdentity):
            raise BenchSessionError("The load probe did not return a documented instrument identity.")
        resource = self._resource_for(load)
        return resource, f"{identity.label} · {resource}"

    @staticmethod
    def _check_load_capacity(load):
        for name, ceiling in (("max_voltage", 500), ("max_current", 120), ("max_power", 3700)):
            value = getattr(load, name, None)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= ceiling:
                raise BenchSessionError(f"The load factory must enforce {name} within the M8437B hardware ceiling ({ceiling:g}).")

    def _check_cancelled(self):
        if self._abort.is_set():
            raise BenchSessionError("Bench operation cancelled; no further control write is allowed.")

    def _validate_reading(self, reading, *, mapped, identity=None):
        if not isinstance(reading, LoadReading):
            raise BenchSessionError("The load did not return an independent measurement.")
        numbers = (reading.voltage, reading.current, reading.power, reading.acquired_at)
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in numbers):
            raise BenchSessionError("The independent load reading contains invalid values.")
        age = self._clock() - reading.acquired_at
        if not 0 <= age <= self.settings.sample_max_age:
            raise BenchSessionError("The independent load measurement is stale or future-dated.")
        if type(reading.sequence) is not int or reading.sequence < 1:
            raise BenchSessionError("The load did not provide a valid acquisition sequence.")
        if type(reading.input_on) is not bool or type(reading.remote) is not bool or type(reading.unregulated) is not bool:
            raise BenchSessionError("The actual load input/remote state is unavailable.")
        expected = identity or self._identity
        if expected is not None and (reading.model_code, reading.firmware_code) != (expected.model_code, expected.firmware_code):
            raise BenchSessionError("The load identity changed after its read-only probe.")
        if mapped and (reading.mode not in ("CC", "CV") or type(reading.setpoint) not in (int, float)
                       or not math.isfinite(reading.setpoint) or reading.setpoint < 0):
            raise BenchSessionError("The actual load mode/setpoint does not match the commissioned CC/CV mapping.")
        if reading.faults:
            raise BenchSessionError("Load fault: " + "; ".join(reading.faults))
        return reading

    def probe(self):
        """Read identity/measurements only; never create CAN or write the load."""
        if self._owned:
            raise BenchSessionError("Stop and release the active bench session before probing.")
        self._probe_valid = self._probe_commissioned = False
        load = self.load_factory()
        self._check_load_capacity(load)
        claim = ("LOAD", self._resource_for(load))
        with self._claims_lock:
            if claim in self._claims:
                raise BenchSessionError("Another session owns this load; probing cannot interrupt it.")
            self._claims.add(claim)
        try:
            self._confirmed(load.open(), "Load connection")
            identity = load.probe()
            resource, reference = self._identity_for(load, identity)
            if self.protocol_approved and self._mode_codes_valid():
                self._confirmed(load.commission(identity, self.cc_mode_code, self.cv_mode_code), "Local mode commissioning")
                self._probe_commissioned = load.commissioned is True
            reading = self._validate_reading(load.read(), mapped=self._probe_commissioned, identity=identity)
            self._identity, self._resource, self._reference = identity, resource, reference
            self._probe_valid = True
            self._ramp_supported = callable(getattr(load, "set_load_current", None))
            self.probe_result = dict(identity=asdict(identity), label=identity.label, reference_identity=reference,
                                     protocol_source=identity.source, reading=asdict(reading),
                                     commissioned_mapping=self._probe_commissioned,
                                     instrument_writes=False, can_accessed=False)
            return copy.deepcopy(self.probe_result)
        finally:
            try:
                load.close()  # Driver close is explicitly non-mutating.
            finally:
                with self._claims_lock:
                    self._claims.discard(claim)

    def acquire(self, operation):
        if self._owned:
            return False
        reason = self.readiness(operation)
        if reason:
            raise BenchSessionError(reason)
        self._check_cancelled()
        claims = {("CAN", self.can_index), ("LOAD", self._resource)}
        with self._claims_lock:
            if self._claims.intersection(claims):
                raise BenchSessionError("Another automatic session owns this CAN adapter or load.")
            self._claims.update(claims)
            self._claimed = claims
        self._thread = threading.get_ident()
        self._operation, self._passive = operation, operation == "test" and not self.post_test_can_start
        self._can_failed = False
        self._started = self._shutdown_accepted = False
        self._baseline_started = self._baseline_start_attempted = False
        self._applied_duties = self._load_configuration = None
        self._save_attempts.clear()
        self._last_load_sequence = None
        self.can_observations.clear()
        self._can_observed_at.clear()
        try:
            self._load = self.load_factory()
            self._check_load_capacity(self._load)
            if self._resource_for(self._load) != self._resource:
                raise BenchSessionError("The selected load port/address changed after probing.")
            self._confirmed(self._load.open(), "Load connection")
            identity = self._load.probe()
            resource, reference = self._identity_for(self._load, identity)
            if identity != self._identity or resource != self._resource or reference != self._reference:
                raise BenchSessionError("The selected load changed since probing; probe and approve it again.")
            self._confirmed(self._load.commission(identity, self.cc_mode_code, self.cv_mode_code), "Local mode commissioning")
            self._validate_reading(self._load.read(), mapped=True)
            self._check_cancelled()
            self._can = self.monitor_factory()
            opened = self._can.open(index=self.can_index, channel=0)
            if opened.get("state") != "open":
                raise BenchSessionError("CAN device opening was not confirmed.")
            # Normal controller mode supplies CAN acknowledgements. Test command
            # transmission is independently forbidden by _send, including cleanup.
            started = self._can.start(bitrate=self.bitrate, listen_only=False)
            expected = "active"
            if started.get("state") != expected or started.get("listen_only") is not False:
                raise BenchSessionError("CAN started in an unexpected mode; automatic control was not enabled.")
            self._trace_event("STATE", action="can_opened", event="capture_state",
                              outcome="CAN ACK enabled; charger command transmission blocked" if self._passive else "Active receive started")
            self._check_cancelled()
            self._confirmed(self._load.enable_remote(), "Load remote mode")
            reading = self._validate_reading(self._load.read(), mapped=True)
            if not reading.remote:
                raise BenchSessionError("Load remote mode readback was not ON.")
            self._owned = True
            return True
        except Exception:
            self._close_resources()
            self._drop_claims()
            self._thread = None
            raise

    def _require_owner(self, *, allow_abort=False):
        if not self._owned or threading.get_ident() != self._thread:
            raise BenchSessionError("Only the acquiring worker may access the active bench session.")
        if self._abort.is_set() and not allow_abort:
            raise BenchSessionError("Bench operation cancelled; no further adjustment or save is allowed.")

    def request_abort(self):
        self._abort.set()

    def enter_passive(self):
        """Receive-only at the application layer; keep electrical CAN ACK enabled."""
        self._require_owner()
        if self._operation != "test" or not self._passive or self._can_failed:
            raise BenchSessionError("Transmit-free testing must be selected before opening CAN.")
        if self._can.state != "active" or self._can.listen_only is not False:
            raise BenchSessionError("Test monitoring requires normal CAN acknowledgement with command writes blocked.")
        return True

    def begin_post_test(self, stage="Test"):
        raise BenchSessionError("Tests use external AC restart; no CAN Start or parameter changes are allowed.")

    def finish_test(self):
        """Release the load without sending any charger command, on every exit."""
        self._require_owner(allow_abort=True)
        if self._operation != "test":
            raise BenchSessionError("Test cleanup requires test ownership.")
        if self._test_cleanup_attempted:
            return self.test_parameter_restore_result.get("status") == "PASS"
        self._test_cleanup_attempted = True
        result = self.test_parameter_restore_result
        result.update(status="CLEANING", required=False, restored=None,
                      parameters_changed=False, restoration_required=False,
                      charger_commands_sent=self.can_tx_count,
                      note="Charger settings unchanged. Switch charger AC OFF before disconnecting.")
        try:
            self.load_off()
            result["load_off_confirmed"] = True
            if self.can_tx_count != 0:
                raise BenchSessionError("A charger command was transmitted during the external-restart test.")
            result["status"] = "PASS"
            return True
        except Exception as exc:
            result.update(status="HOLD", error=str(exc),
                note="Test cleanup is unconfirmed. Switch charger AC OFF and check the load before disconnecting.")
            return False
        finally:
            result["finished_at"] = datetime.now(timezone.utc).isoformat()

    restore_test_parameters = finish_test

    def _drain_configuration(self):
        for _ in range(8):
            self._check_cancelled()
            if not self._receive_can(action="parameter_backlog_drain", observe=False).get("frames", []):
                break
        else:
            raise BenchSessionError("CAN receive backlog prevents a fresh parameter check.")
        for key in CONFIG:
            self.can_observations.pop(key, None)
            self._can_observed_at.pop(key, None)

    def _parameter_readback(self, expected):
        self._drain_configuration()
        required = {key for key in CONFIG if CONFIG[key]["response"] != 0x18FF56E5 or expected[key] != 0}
        deadline, next_poll = self._clock() + 8, 0.0
        while self._clock() < deadline:
            self._check_cancelled()
            if self._clock() >= next_poll:
                self._send("parameter_read", [poll(group) for group in range(1, 7)])
                next_poll = self._clock() + 1
            self._receive_can()
            if required.issubset(self.can_observations):
                break
            self._abort.wait(.1)
        observed = {key: self.can_observations[key] for key in CONFIG if key in self.can_observations}
        missing = sorted(required - observed.keys())
        optional_unavailable = sorted(set(CONFIG) - required - observed.keys())
        mismatched = {key: {"expected": expected[key], "actual": observed[key]}
            for key in observed if not math.isclose(expected[key], observed[key], rel_tol=0, abs_tol=1e-8)}
        return dict(observed=observed, missing=missing, optional_unavailable=optional_unavailable,
                    mismatched=mismatched)

    def prepare_charger_parameters(self):
        """Check each charger; write once only after actual mismatch and measured shutdown."""
        self._require_owner()
        if self._operation != "calibration" or self.charger_parameters is None:
            return None
        expected = copy.deepcopy(self.charger_parameters)
        frames = encode_all_config(expected)
        started = self._clock()
        result = dict(status="CHECKING", desired=expected, before={}, after={}, updated_fields=[],
                      optional_unavailable=[], duration_seconds=0.0,
                      note="Unavailable adjustable-limit readbacks are allowed only when requested values are zero.")
        self.preflight_result = result
        try:
            before = self._parameter_readback(expected)
            result.update(before=before["observed"], optional_unavailable=before["optional_unavailable"])
            if before["missing"]:
                raise BenchSessionError("Missing required charger parameter readback: " + ", ".join(before["missing"]))
            if not before["mismatched"]:
                result.update(status="PASS", after=before["observed"], action="Already matched; no parameter writes")
                result["duration_seconds"] = round(self._clock() - started, 3)
                return copy.deepcopy(result)
            result.update(status="UPDATING", updated_fields=sorted(before["mismatched"]))
            self.charger_shutdown()
            deadline, low_count = self._clock() + self.settings.shutdown_timeout, 0
            while self._clock() < deadline and low_count < 3:
                self._check_cancelled()
                reading = self._read_load()
                if (reading.voltage > self._load.max_voltage or reading.current > self._load.max_current
                        or reading.power > self._load.max_power):
                    raise BenchSessionError("Measured charger output exceeds bench limits during the parameter check.")
                low_count = low_count + 1 if (reading.voltage <= self.settings.shutdown_voltage
                    and reading.current <= self.settings.shutdown_current) else 0
                if low_count < 3:
                    self._abort.wait(.1)
            if low_count < 3:
                raise BenchSessionError("Charger output did not confirm shutdown; parameters were not written.")
            self._send("parameter_write", frames)
            after = self._parameter_readback(expected)
            result.update(after=after["observed"], optional_unavailable=after["optional_unavailable"])
            if after["missing"] or after["mismatched"]:
                raise BenchSessionError("Charger parameter update readback did not match; no calibration was started.")
            result.update(status="PASS", action="Updated mismatched parameters and confirmed readback")
            result["duration_seconds"] = round(self._clock() - started, 3)
            return copy.deepcopy(result)
        except Exception as exc:
            result.update(status="FAIL", error=str(exc))
            raise
        finally:
            result["duration_seconds"] = round(self._clock() - started, 3)

    def _read_load(self):
        reading = self._validate_reading(self._load.read(), mapped=True)
        if not reading.remote:
            raise BenchSessionError("The load left confirmed remote control; check fixture ownership.")
        return reading

    def load_off(self):
        self._require_owner(allow_abort=True)
        self._confirmed(self._load.set_input(False), "Load input OFF")
        if self._read_load().input_on:
            raise BenchSessionError("Load input OFF readback did not match.")
        self._load_configuration = None
        return True

    def configure_load(self, mode, setpoint):
        self._require_owner()
        if mode not in ("CC", "CV") or type(setpoint) not in (int, float) or not math.isfinite(setpoint) or setpoint < 0:
            raise ValueError("Choose CC/CV and a finite nonnegative setpoint.")
        if self._read_load().input_on:
            raise BenchSessionError("Confirm load input OFF before changing mode or setpoint.")
        self._check_cancelled()
        self._confirmed(self._load.configure(mode, setpoint), "Load configuration")
        reading = self._read_load()
        if reading.input_on or reading.mode != mode or not math.isclose(reading.setpoint, setpoint, rel_tol=1e-6, abs_tol=1e-6):
            raise BenchSessionError("Actual load mode/setpoint/input readback did not match.")
        self._load_configuration = (mode, float(setpoint))
        self._load_configuration_version += 1
        return True

    def load_on(self):
        self._require_owner()
        if self._load_configuration is None:
            raise BenchSessionError("Configure and verify the load before turning its input on.")
        self._confirmed(self._load.set_input(True), "Load input ON")
        reading = self._read_load()
        mode, setpoint = self._load_configuration
        if not reading.input_on or reading.mode != mode or not math.isclose(reading.setpoint, setpoint, rel_tol=1e-6, abs_tol=1e-6):
            raise BenchSessionError("Actual load input/mode/setpoint changed while enabling input.")
        return True

    def set_load_current(self, current):
        self._require_owner()
        if not self.capabilities.termination_ramp:
            raise BenchSessionError("Continuous CC ramp behavior has not been qualified on this fixture.")
        if type(current) not in (int, float) or not math.isfinite(current) or current < 0:
            raise ValueError("CC ramp current must be finite and nonnegative.")
        before = self._read_load()
        if not before.input_on or before.mode != "CC" or before.unregulated:
            raise BenchSessionError("Continuous CC ramp requires regulated CC mode with input ON.")
        self._check_cancelled()
        self._confirmed(self._load.set_load_current(current), "Continuous CC current change")
        after = self._read_load()
        if not after.input_on or after.mode != "CC" or not math.isclose(after.setpoint, current, rel_tol=1e-6, abs_tol=1e-6):
            raise BenchSessionError("Continuous CC mode/input/setpoint readback did not match.")
        self._load_configuration = ("CC", float(current))
        return True

    def transition_load_to_cc(self, current):
        """One CV -> CC transition while the load stays enabled during testing."""
        self._require_owner()
        if self._operation != "test" or not self._passive:
            raise BenchSessionError("Live load-mode transitions are restricted to transmit-free tests.")
        if type(current) not in (int, float) or not math.isfinite(current) or current <= 0:
            raise ValueError("Choose a positive, finite CC current.")
        before = self._read_load()
        if (not before.input_on or before.mode != "CV" or before.unregulated
                or before.voltage <= self.settings.shutdown_voltage or before.current <= self.settings.shutdown_current):
            raise BenchSessionError("Live transition requires conducting, regulated CV with load input ON.")
        transition = getattr(self._load, "transition_to_cc", None)
        if not callable(transition):
            raise BenchSessionError("The load driver does not support a live CV to CC transition.")
        event = dict(started_at=self._clock(), from_mode="CV", to_mode="CC", current=float(current),
                     before=asdict(before), status="ATTEMPTED", input_toggle_sent=False)
        self.load_transition_events.append(event)
        try:
            self._check_cancelled()
            self._confirmed(transition(current), "Live CV to CC transition")
            after = self._read_load()
            event["after"] = asdict(after)
            if (not after.input_on or after.mode != "CC"
                    or not math.isclose(after.setpoint, current, rel_tol=1e-6, abs_tol=1e-6)
                    or after.voltage <= self.settings.shutdown_voltage or after.current <= self.settings.shutdown_current):
                raise BenchSessionError("Load input/output was interrupted during CV to CC; this is not an end-current cutoff.")
            self._load_configuration = ("CC", float(current))
            self._load_configuration_version += 1
            event.update(status="READBACK_CONFIRMED", finished_at=self._clock(),
                         note="Input ON before and after; sub-sample transients are not resolved by serial readback.")
            return True
        except Exception as exc:
            event.update(status="FAILED", error=str(exc), finished_at=self._clock())
            raise

    def _receive_can(self, *, action="monitor", observe=True):
        if self._can_failed:
            raise BenchSessionError("CAN connection failed; no automatic reopening is allowed.")
        try:
            result = self._can.receive()
            for frame in result.get("frames", []):
                self._trace_event("RX", action=action, frame=frame, outcome="RECEIVED",
                                  allow_capture_failure=self._test_control_scope == "cleanup")
                if not observe:
                    continue
                if frame.get("error"):
                    self.can_observations["adapter_error"] = True
                    self._can_observed_at["adapter_error"] = self._clock()
                    continue
                try:
                    values = decode(frame).get("values", {})
                except ValueError:
                    continue
                self.can_observations.update(values)
                self._can_observed_at.update({key: self._clock() for key in values})
            return result
        except TraceCaptureError:
            # Keep the working adapter available for the abort shutdown path.
            raise
        except Exception:
            self._can_failed = True
            self._can.close()
            raise

    def read(self, now):
        self._require_owner(allow_abort=True)
        reading = self._read_load()
        self._receive_can()  # Receive only: no readback polls in either operation.
        self._validate_reading(reading, mapped=True)  # Account for time spent receiving CAN.
        if self._last_load_sequence is not None and reading.sequence <= self._last_load_sequence:
            raise BenchSessionError("The load repeated an acquisition instead of providing a fresh measurement.")
        self._last_load_sequence = reading.sequence
        fault = ""
        if self._clock() - self._can_observed_at.get("adapter_error", -100) <= 2:
            fault = "CAN adapter error frame received"
        return AutomationReading(reading.voltage, reading.current, reading.power, reading.acquired_at,
                                 reading.sequence, self._reference, reading.mode, reading.setpoint,
                                 reading.input_on, fault, unregulated=reading.unregulated,
                                 charger_fault_word=self.can_observations.get("faultWord"),
                                 charger_fault_observed_at=self._can_observed_at.get("faultWord"))

    def _duties(self, voltage, current):
        values = {"voltage": voltage, "current": current}
        for axis, value in values.items():
            encode_duty(axis, value)
            if not getattr(self.settings, axis + "_min") <= value <= getattr(self.settings, axis + "_max"):
                raise ValueError(axis.title() + " duty is outside the commissioned bounds.")
        return dict(values)

    def _send(self, action, frames, *, allow_abort=False):
        self._require_owner(allow_abort=allow_abort)
        if self._operation != "calibration" or self._passive:
            raise BenchSessionError("Test charger commands are disabled: no Start, Shutdown, parameter, trim, save or read-poll frames.")
        if self._can_failed or self._can.state != "active" or self._can.listen_only:
            raise BenchSessionError("Normal CAN mode is unavailable; no reopening or retry is attempted.")
        emergency_shutdown = allow_abort and action == "shutdown" and frames == [command("shutdown")]
        protective_write = emergency_shutdown
        if not self.trace_capture_complete and not protective_write:
            raise TraceCaptureError("CAN trace capture failed; no further normal command is allowed.")
        deadline = self._clock() + 10
        for index, frame in enumerate(frames):
            if not allow_abort and self._abort.is_set():
                self._started = False
                self._applied_duties = None
                raise BenchSessionError("Remaining charger frames cancelled; already accepted frames cannot be undone.")
            if self._clock() > deadline:
                raise BenchSessionError("Charger command batch exceeded its bounded time budget.")
            attempt = self._trace_event("TX", action=action, frame=frame,
                                       outcome="ATTEMPT; outcome unconfirmed",
                                       allow_capture_failure=protective_write)
            self.can_tx_count += 1
            trace = {"action": action, "frame": copy.deepcopy(frame), "status": "OUTCOME UNCONFIRMED"}
            self.can_trace.append(trace)
            try:
                result = self._can.send(frame)
                if result.get("accepted") != 1 or result.get("acknowledged") is not False:
                    raise BenchSessionError("CAN API did not confirm complete frame acceptance.")
            except Exception as exc:
                self._trace_event("TX_RESULT", action=action, frame=frame, event="tx_result",
                                  correlates_to=attempt["event_id"], outcome="ERROR; outcome unconfirmed: " + str(exc),
                                  allow_capture_failure=True)
                self._can_failed = True
                self._started = False
                self._applied_duties = None
                self._can.close()
                raise
            trace["status"] = "API ACCEPTED; persistence unconfirmed"
            self.can_accepted_count += 1
            self._trace_event("TX_RESULT", action=action, frame=frame, event="tx_result",
                              correlates_to=attempt["event_id"], outcome=trace["status"],
                              allow_capture_failure=protective_write)
            if index + 1 < len(frames) and frame["data"][1] in ({0x14, 0x16, 0x0B} | {spec["selector"] for spec in CONFIG.values()}):
                self._abort.wait(.1)
        return True

    def charger_shutdown(self):
        self._require_owner(allow_abort=True)
        self._started = False
        self._baseline_started = False
        self._applied_duties = None
        self._shutdown_accepted = False
        accepted = self._send("shutdown", [command("shutdown")], allow_abort=True)
        self._shutdown_accepted = accepted
        return accepted

    def charger_start_baseline(self):
        """Start unchanged normal operation once; retain active CAN for shutdown.

        This sends neither parameter writes nor calibration trim commands. It
        deliberately does not authorize charger_apply_duties/calibration save.
        """
        self._require_owner()
        if self._operation != "calibration" or self._passive or self._started:
            raise BenchSessionError("Baseline startup requires active calibration-session ownership before trim changes.")
        if self._baseline_start_attempted:
            raise BenchSessionError("Baseline Start was already attempted; no automatic retry is allowed.")
        if self._read_load().input_on:
            raise BenchSessionError("Baseline open-voltage measurement requires confirmed load input OFF.")
        self._baseline_start_attempted = True
        self._shutdown_accepted = False
        self._send("baseline_start", [command("workStart")])
        self._baseline_started = True
        return True

    def charger_start_calibration(self, voltage_duty, current_duty):
        self._require_owner()
        if not self._shutdown_accepted and not self._started:
            raise BenchSessionError("Request charger Shutdown before entering calibration.")
        duties = self._duties(voltage_duty, current_duty)
        self._applied_duties = None
        self._started = False
        frames = [command("calibrationStart")] + [encode_duty("voltage", voltage_duty), encode_duty("current", current_duty)] * 3
        self._send("calibration_start", frames)
        self._applied_duties, self._started, self._shutdown_accepted = duties, True, False
        return True

    def request_charger_diagnostics(self):
        """Request fresh diagnostics for the bounded calibration-start transition.

        Use all original poll groups: a group-to-response timing relationship
        has not been captured on this charger. No cache is cleared or re-aged.
        """
        self._require_owner()
        if self._operation != "calibration" or not (self._started or self._baseline_started):
            raise BenchSessionError("Diagnostic refresh requires an active calibration or baseline start.")
        return self._send("startup_diagnostic_read", [poll(group) for group in range(1, 7)])

    def charger_apply_duties(self, voltage_duty, current_duty):
        self._require_owner()
        if not self._started:
            raise BenchSessionError("Complete Cal. start before adjusting duties.")
        duties = self._duties(voltage_duty, current_duty)
        self._applied_duties = None
        self._send("apply_duties", [encode_duty("voltage", voltage_duty), encode_duty("current", current_duty)] * 3)
        self._applied_duties = duties
        return True

    def charger_save(self, axis):
        self._require_owner()
        if axis not in ("current", "voltage"):
            raise ValueError("Save current or voltage calibration explicitly.")
        if not self._started or self._applied_duties is None:
            raise BenchSessionError("Complete the displayed duty batch before saving calibration.")
        if axis in self._save_attempts:
            raise BenchSessionError("This calibration save was already attempted; no automatic retry is allowed.")
        self._save_attempts.add(axis)
        return self._send("save_" + axis, [command("saveFullCurrent" if axis == "current" else "saveVoltage")])

    def _close_resources(self):
        errors = []
        for name in ("_can", "_load"):
            client = getattr(self, name)
            setattr(self, name, None)
            if client is not None:
                try:
                    client.close()  # Never send Shutdown, load OFF, or restore a mode here.
                except Exception as exc:
                    errors.append(str(exc))
        return errors

    def _drop_claims(self):
        with self._claims_lock:
            self._claims.difference_update(self._claimed)
            self._claimed.clear()

    def release(self):
        if self._owned and threading.get_ident() != self._thread:
            raise BenchSessionError("Release the fixture on its owning worker.")
        errors = []
        if self._owned and self._operation == "test":
            if not self.finish_test():
                errors.append("Load shutdown is unconfirmed; switch charger AC OFF and check the fixture")
        errors.extend(self._close_resources())
        self._drop_claims()
        self._owned = False
        self._thread = None
        self._passive = True
        self._started = self._shutdown_accepted = False
        self._baseline_started = self._baseline_start_attempted = False
        self._applied_duties = None
        if errors:
            raise BenchSessionError("Connection close failed: " + "; ".join(errors))
