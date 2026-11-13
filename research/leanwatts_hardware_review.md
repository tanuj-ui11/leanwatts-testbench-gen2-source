# LeanWatts EoL 0.1.0: hardware and calibration review

Static inspection on 2026-11-10. The original package was not edited, launched, or connected to hardware.

Source root: `LeanWatts-EoL-0.1.0-win64 (1)/LeanWatts EoL/_internal/app/lw_eol/`.

## Result

The package contains readable Python source, instrument abstractions, serial/TCP transports, a configurable protocol engine, variant recipes, and test steps. These are useful implementation references. **It does not contain a completed Maynuo M8437B protocol map, and its calibration algorithm is not equivalent to the JK charger duty-trim procedure.**

## Evidence

| File under source root | Evidence | Consequence |
|---|---|---|
| `drivers/protocol_maps/maynuo_m8437b.yaml:3` | Interface and protocol explicitly described as unknown; `status: placeholder` at line 8 | Not evidence of working remote control |
| Same file, lines 19–40 | Every command is `TODO(protocol)`, including identification, mode, setpoints, input enable/disable and measurements | No usable M8437B command bytes recovered from this map |
| `drivers/protocol_map.py:151` | Missing/TODO commands raise `ProtocolNotConfigured` | Normal mapped calls refuse guessed commands |
| `drivers/real/instruments.py:194` | `MaynuoM8437b` translates abstract functions to map command names | Reusable driver shell, dependent on the missing protocol |
| `config/examples/station_bench_maynuo.yaml` | Example load connection COM3/9600, simulated CAN and simulated safety I/O, production mode false | Example wiring/settings must not be treated as measured bench configuration |
| `config/examples/station_h1.yaml` | CAN interface is `TBD`, 250 kbit/s; Maynuo is COM5 in this different example | No adapter-specific compatibility established |
| `config/devices.yaml:18` | Internal catalogue labels M8437B as sink-only, 500 V / 120 A / 3700 W | Package-provided specifications; still confirm against the actual instrument/nameplate/manual |
| `recipes/examples/S1500W-72V-CAN.yaml` | CAN recipe references `lw_sim_charger.dbc` | Its example calibration/command messages are not proof of JK firmware compatibility |

The package README describes a Waveshare USB-to-RS232/485 connection and a DC-load-test protocol-detection tool. Those statements describe this package's intended use, not evidence that the user's present load has been identified or successfully controlled. No probes were run here.

Static inspection of `app/screens/load_test.py:367` finds generic `*IDN?` probes and Modbus function 3/4 reads of register zero across common baud rates. It records replies and selects a baud rate; it does not populate or verify the Maynuo protocol map. `discover()` at line 465 first requires a response to `*IDN?`, then logs a generic list of SCPI-style queries. A Modbus-only device cannot complete this path. These tools offer bring-up logging, not an implemented M8437B driver. No hidden model-specific control bytes were found in them.

## Calibration semantics

`engine/steps/calibration.py` explicitly calibrates the charger's **reported V/I measurement**. At configured current points (default 10% and 90%), it averages load/reference readings and charger telemetry, computes reference-minus-reported voltage and current offsets, sends additive offsets through CAN or RS485, then compares readings again.

The user's JK procedure adjusts current and voltage duty values until independently measured output equals the desired target, then performs full-current/voltage saves. A telemetry-offset command is not interchangeable with a regulation-duty correction. Reusing the LeanWatts calibration step unchanged would require firmware evidence that its command semantics match; that evidence is absent.

Its `cc_points.py` does contain useful mode logic: CAN variants use load CV while commanding charger current; non-CAN variants use CC loading for partial current points and CV loading at full current. Its `cv_points.py` measures voltage on a CC-loaded plateau, then optionally follows a virtual battery taper.

The termination test requires `DC_BIAS` capability (`cv_points.py:43`) and energises a separate bias supply after termination to hold battery voltage and test whether the charger stays off. The user has described only a charger and a sink load. That test therefore cannot be assumed available on the described fixture.

## Driver details to correct before production reuse

These observations are source review findings, not hardware-tested failures:

1. `MaynuoM8437b.status()` returns cached `_mode` and `_on`. Although the map defines `input_state`, the driver does not query it here. Commanded state must be distinguished from confirmed instrument state.
2. `status_bits` defaults to an empty mapping. Even a non-zero status word would produce no named alarms without the correct bit definitions. Commissioning must establish and test fault decoding.
3. The generic Modbus read path checks CRC but does not validate response slave, requested function, or expected byte count against the request. The write path does not verify CRC or the echoed address/value/count; it also reads eight bytes before handling an exception, although an exception frame is shorter. Tighten framing and acknowledgement checks before relying on this for control.
4. The generic protocol engine currently supports function codes 3/4/6/16, not coil read/write 1/5. If the exact M8437B manual requires coil operations, merely filling YAML registers will be insufficient.
5. Configuration status `verified` is not itself enforced by `MappedInstrument`; non-verified status only logs a warning. A command with a filled template can run. A production commissioning gate must enforce approved mappings separately.
6. Example recipes labelled `approved` are example data, not evidence of approval for the user's charger variant. Their addresses, limits, manufacturing writes, firmware identifiers and calibration commands require separate review.

## Reuse recommendation

Keep the original package intact. Reuse its readable source structure, variant/profile concepts, transport separation, instrument-limit catalogue, test-step architecture and reporting ideas. Implement a distinct JK charger adapter from recovered vendor evidence and a distinct JK calibration workflow. Add M8437B control only after a verified manual or supervised successful protocol capture establishes exact framing and commands. Keep simulation identity explicit in all views and reports.

Outstanding information remains the exact load interface/protocol, adapter compatibility, charger targets/tolerances, full parameter mapping, save/readback/persistence evidence, and a physically valid termination test for this fixture.
