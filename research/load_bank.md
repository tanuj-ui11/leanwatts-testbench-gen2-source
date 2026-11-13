# Maynuo M8437B integration findings

## Current implementation — update 7

The user later supplied the M97 programming manual and asserted that the M84 shares its firmware/control architecture. A documented M97-compatible driver is now implemented, with real automated bench sessions available after one-time station commissioning. This supersedes the earlier unavailable-driver notes below. It does not turn the user's compatibility assertion into a manufacturer guarantee: read-only identity/front-panel checks, observed CC/CV codes and independent readbacks qualify the actual station. See `maynuo/PROTOCOL-SOURCES.md` for the command source and unverified details.

The user clarified that OV means normal output/end voltage; this variant needs a 49 V full-current test point because it derates at 48 V; its team reduces CC demand from 5 A to observe cutoff. Required targets, programmed workaround values and observed cutoffs are now distinct. The example programmed 1.5 A / observed 1.1 A is not silently made an acceptance target. Only tests possible with the load and CAN charger are included: voltage/current regulation, power derived from V/I, sustained-load stability and a qualified cutoff ramp. Test settings remain per variant.

Research date: 2026-11-10. No instrument was connected, commanded, or energised during this research.

## Verified evidence and its limits

The [manufacturer's China website](https://maynuo.cn/index.html) lists an M84 high-performance programmable DC electronic-load family. Its M84 product link could not be retrieved during this research. No exact M8437B programming manual was verified.

The [official download centre](https://maynuo.cn/a/download/) links an "all series" electronic-load manual. However, the actual [2021 V3.9 manual](https://maynuo.cn/uploads/soft/220609/%E8%AF%B4%E6%98%8E%E4%B9%A6.pdf) cover lists M97 and some M98 models, not M8437B. Section 4.9 documents Modbus and serial communications for that covered family; it does **not** establish M8437B compatibility. Section 4.9.2 describes isolated M131/M132/M133 cables, with TTL levels at the instrument end. Do not infer an M8437B cable pinout from this.

The same manual explains CC as constant current sinking and CV as current sinking to maintain voltage. CV cannot regulate a source whose available voltage is below the setpoint (sections 4.1.1 and 4.1.3). These general operating principles inform the interpretation below; M8437B-specific limits still require its manual.

## Interpretation of the user's process

This section is engineering interpretation, not a verified charger procedure.

| Stage | Load-bank setting | Measured quantity | Actuator that likely needs adjustment |
|---|---|---|---|
| Full-current calibration | CV, initially 48 V if correct for this variant | Actual current from a calibrated independent reference/load measurement | Charger current calibration trim over CAN |
| Voltage calibration | CC, initially 1 A if within the charger's operating envelope | Actual voltage at the specified measurement point | Charger voltage calibration trim over CAN |
| CC verification | CV at approved test voltage(s) | Actual current | No calibration writes |
| End-voltage verification | Appropriate light CC load | Actual regulated/termination voltage | No calibration writes |
| End-current verification | A validated termination test profile | Measured current and independently observed termination event | Load profile only; no calibration writes |

"Tune the current/voltage in the DC load bank" is ambiguous. With CV fixed, the charger normally determines current. With CC fixed, the charger normally determines voltage. Changing load setpoints until a display happens to equal the target does not calibrate the charger. The original application's current/voltage duty controls are a plausible trim actuator; confirm their semantics from the recovered CAN code and a supervised capture.

The screenshot shows a 29.6 V charge command, but the described current-calibration setup uses 48 V load CV. These cannot be assumed to be the same variant. A 48 V CV setpoint cannot pull a 29.6 V source up to 48 V. Store these as variant-specific values and reject incompatible recipes.

## Required driver boundary

Keep the M8437B hardware driver unavailable until its remote-control protocol is confirmed. Do not implement arbitrary SCPI commands or copy M97 register addresses and label them M8437B support. Needed functions are: identify instrument; read range/limits and status; set CV/CC with input disabled; set and read back setpoint; input on/off with state confirmation; read voltage/current/power; read protection/regulation state; and return to local control.

To enable this driver, obtain the exact instrument manual or vendor protocol, firmware revision, physical interface/cable model, communication parameters, and a reference capture of successful remote operation. Read-only identity/status checks come before setpoint writes. The driver should validate framing, identity, units, finite values, freshness, exception responses, and command acknowledgement.

## Automation requirements

The following are proposed implementation requirements, not claims about existing hardware capabilities:

- A variant recipe defines charge parameters, calibration fixture values, current/voltage tolerances, settling window, maximum trim changes, test timeout, and validated voltage/current/power ceilings.
- Current and voltage trim loops require several fresh stable independent samples before declaring convergence. Calibration saves happen once after convergence, followed by acknowledgement/readback and verification of persistence. Never repeatedly save calibration during search.
- Loss of CAN/load telemetry, instrument faults, unstable regulation, or limits exceeded must stop progression. Shutdown attempts and actual confirmed states must be logged separately; an unacknowledged stop is not evidence of an electrically safe state.
- Define shutdown order from the fixture's electrical design. Removing the load first can leave the charger at open-circuit voltage. A physical emergency cutoff is independent of laptop communications.
- Every result records charger serial/model/firmware, recipe revision, load/reference identification, target and measured values, units, timestamps, raw CAN evidence, trim changes, save outcome, and actual pass/fail criteria.
- If "non charger" means operation without CAN control, final verification needs the documented autonomous operating mode and a test without host start/keepalive messages. Simply reporting a CAN-controlled current or voltage does not establish autonomous charging.
- End-current verification must observe the charger crossing its termination condition. A programmed 1 A load value alone is not evidence that the charger terminates at 1 A. The fixture may need a battery simulator or approved CV/CC transition procedure, depending on firmware logic.

## Open items

1. Exact M8437B manual/protocol and fitted communication interface/cable.
2. Charger model/firmware and the actual full-current, end-voltage, end-current targets and tolerances.
3. Firmware settings and validated acceptance procedure for standalone operation without CAN commands; the user has confirmed this requirement.
4. Reference instrument accuracy, calibration validity, sense wiring/measurement point, and approved load/fixture operating limits.
5. Approved end-current test method and proof that saved trims survive restart.

## User-confirmed setup update

The user subsequently confirmed the load bank is USB-connected to the working laptop. Final acceptance must demonstrate standalone charging without CAN commands. The GUI records unperformed hardware checks explicitly; no simulated result satisfies them.

The [official M84 page](https://www.maynuo.cn/a/product/M84%20DC%20LOAD/) confirms M8437B ratings of 3700 W, 500 V, 120 A and optional USB/RS232/RS485/LAN with standard Modbus. Public downloads checked again on 2026-11-10 did not provide an exact M84 register map or USB framing. The M97/M98 manual must not be substituted. Operator workspace 6 therefore includes a bounded workflow engine and explicit demo adapter, with real automatic actuation unavailable until this driver boundary is verified. The working manual charger path is unchanged.
