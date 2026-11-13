# Maynuo load adapter evidence

Research and implementation date: 2026-11-10. No instrument was opened or commanded during development; tests use fake serial transports.

The user states that this M8437B uses the M97 firmware/menu/protocol architecture. The adapter is therefore labelled **M97-compatible, M84 compatibility user asserted**. Neither linked manual independently establishes M84 firmware compatibility. Each bench must pass a read-only comparison and explicit engineering setup before writes are enabled.

## Primary documents

- User-supplied [Maynuo Electronics 2009 English manual](http://www.maynuo.com/downloadfile/2010011957866497.pdf), downloaded as `M97-user-provided-2010011957866497.pdf` (51 PDF pages). SHA256: `0acac8739f7992b8334907eddf8a7bd69288716f3f636740b138f79f6036a15d`.
- Cross-check: [official M97 V3.9, 2021 manual](https://www.maynuo.cn/uploads/soft/220609/%E8%AF%B4%E6%98%8E%E4%B9%A6.pdf), sections 4.9.4–4.9.10.
- Hardware ceiling only: [official M84 product page](https://www.maynuo.cn/a/product/M84%20DC%20LOAD/) lists M8437B 3700 W / 500 V / 120 A.

The 2009 English register table says “double”, but defines two 16-bit words and provides four-byte IEEE-754 examples. Its 2.3 A example is `40 13 33 33`. The 2021 document explicitly calls this float. The driver uses big-endian float32. Some English prose also has a wrong PC1 address; both register tables and the 2021 complete command example establish PC1 = 0x0500.

## Implemented subset

Modbus RTU: slave 1–200; 8 data bits, 1 stop bit; N/E/O parity; documented baud rates 2400, 9600, 14400, 28800, 57600, 115200. Register data are big-endian; CRC16 is low byte first. Functions 01/03 read, 05 write one coil, 10 (hex) write registers.

| Function | Address | Interpretation |
|---|---|---|
| Remote control | coil 0x0500 | PC1 true/false, confirmed by coil readback |
| Actual input state | coil 0x0510 | ISTATE: 1 on, 0 off |
| Protection/error flags | coils 0x0520–0x0527 except 0x0525 | Current, voltage, power, temperature, reverse, EEPROM, calibration |
| Regulation status | coil 0x0525 | UNREG: unable to regulate to the requested current/voltage; preserved separately from protection errors |
| Command | register 0x0A00 | CC 1; CV 2; input on 42; input off 43 |
| Current setpoint | registers 0x0A01–0x0A02 | float32 amps |
| Voltage setpoint | registers 0x0A03–0x0A04 | float32 volts |
| Measured voltage/current | registers 0x0B00–0x0B03 | two float32 values |
| Mode / input mode | registers 0x0B04 / 0x0B05 | raw codes; mode enum not published |
| Model / firmware | registers 0x0B06 / 0x0B07 | raw 16-bit codes; no invented textual decoding |

There is no documented power measurement register in this subset: displayed power is explicitly calculated from measured voltage × measured current.

## Unresolved semantics and implemented boundaries

- CC/CV SETMODE values are not specified. Station setup captures them read-only while an engineer selects CC and CV on the front panel with input OFF. Distinct, identity-bound observations are saved. Command values are not assumed to equal mode codes.
- `set_load_current` changes IFIX without an input toggle and verifies setpoint, mode and input state. The manual does not establish that IFIX-only writes immediately alter an already-running CC regulator. Therefore continuous-ramp acceptance is a separate engineering qualification flag, disabled by default. Actual current response must be checked on the bench before using end-current termination tests.
- No load calibration register, remote-sense setting, local-lockout setting, short-circuit mode or undocumented register is written.
- Port close does not send commands. Returning to local control is a separate explicit operation requiring confirmed input OFF.
- A successful write acknowledgement is insufficient: control methods check the documented independent state/readback. Failed or uncertain writes are never retried automatically.

## Regulation status handling — update 7.3

The 2009 manual §§4.1.1.1 and 4.1.3.1 explicitly explain UNREG as failure to reach the requested current/voltage, including an absent source or an unsuitable source operating point. Its register table's “register parameter failed” wording is not interpreted as an EEPROM or calibration failure. On this bench UNREG was observed with input OFF and zero measured V/I.

Bit 5 remains visible as `unregulated` in load readings, automatic samples and reports. All other protection/error flags remain immediate failures. Idle probes, input OFF and initial input enabling do not require regulation. Active calibration and normal test measurements wait within the existing stage timeout, clear their stability window, and cannot adjust duties, save calibration or pass using UNREG samples. Sustained-load tests fail if regulation is lost after their measurement interval begins. Shutdown and termination use the existing independent voltage/current thresholds and prior conducting-current evidence; UNREG alone is never proof of shutdown. Continuous CC stepping requires regulation before writing the next current demand.
