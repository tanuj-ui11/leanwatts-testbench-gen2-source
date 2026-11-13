# Charger CAN protocol recovered from the supplied software

Status: **static reverse engineering, not bench validated**. No charger, load bank, CAN adapter or serial port was opened or commanded during this analysis. The vendor executable was not run.

## Evidence and scope

- Input: `JKCAN_CHARGE/GBS_CAN上位机.exe`, 1,156,608 bytes.
- SHA-256: `D1E2C3FBF9EB721EB3520BC38BBCE427C2C72387707AE0248317F9EF2DE1284C`.
- Its configuration targets .NET Framework 4.5. PE machine `0x014C`; CLI flags `0x00000003` indicate managed IL with 32-bit required.
- `dump-il.js` parses PE/CLI metadata and method bodies as bytes; it does not load the executable. `vendor-il.txt` contains all 206 extracted methods. Method-specific `.il.txt` files retain IL offsets and metadata tokens.
- UI labels and locations were recovered from `Form1.InitializeComponent` and recorded in `ui-fields.json`. Labels establish which button handlers and text boxes correspond to the screenshot.
- The first compiled analysis helper could not be launched because Windows Application Control blocked it; a Windows PowerShell script was also blocked by script policy. No system policy was changed. The working analysis uses Node static byte parsing.

The static mapping has high confidence for what this exact application sends and displays. It does not prove that every charger firmware accepts those commands, that a save reached nonvolatile memory, or that the pictured adapter supports the bundled driver API.

## CAN adapter and framing

`ZLGCAN.Method` imports `ZCAN_OpenDevice`, `ZCAN_InitCAN`, `ZCAN_StartCAN`, `ZCAN_ResetCAN`, `ZCAN_Transmit`, `ZCAN_Receive`, `ZCAN_GetReceiveNum` and `GetIProperty` from **zlgcan.dll**. Device opening chooses a device type and index; channel configuration uses the chosen baud rate.

The user identified a **USBCAN-I (C+)**, manufacturer **北京爱泰联合科技有限公司**, serial **6024052071**. This label alone does not establish ZLG API compatibility. Obtain its matching SDK/driver or confirm the supplied application works with that exact adapter before using a live transport.

All calibration and configuration frames below use **29-bit extended ID `0x1806E550`**, classic CAN data frames, not remote frames. `Form1.CANsend_fram` (token `06000034`) calls `MakeCanId` with the extended flag, sets the requested DLC, uses normal `transmit_type=0`, and submits one frame. `MakeCanId` sets the ZLG extended flag at bit 31; that flag is an API representation and is not part of the 29-bit bus ID. The screenshot's filter text “standard frame” does not change this explicit calibration transmit behavior.

The screenshot shows 250 kbps, but the application allows baud selection; use the verified bench rate. A generated frame is not proof of successful transmission: the legacy wrapper stores the `ZCAN_Transmit` return value without an explicit success check.

## Button and adjustment commands

Bytes are hexadecimal. Send the exact DLC; do not pad the two-byte save commands to eight bytes without a verified reason.

| Action / original label | Handler | DLC | Payload |
|---|---|---:|---|
| Work start / 工作启动 | `button3_Click_1`, token `06000050`, IL `008E–00A8` | 4 | `00 14 00 00` |
| Shutdown / 关机 | `button4_Click`, token `0600004E`, IL `00A8–00C6` | 4 | `00 14 00 80` |
| Calibration start / 校准启动 | `button5_Click`, token `0600004F`, IL `009B–00B5` | 4 | `00 14 00 01` |
| Voltage calibration save / 校准保存 | `button8_Click`, token `0600005D`, IL `000C–001E` | 2 | `00 17` |
| Full-current calibration save / 全电流校准保存 | `button9_Click`, token `0600005E`, IL `000C–001E` | 2 | `00 18` |
| Half-current calibration save / 半电流校准保存 | `button16_Click`, token `0600006F`, IL `000C–001E` | 2 | `00 19` |
| Voltage duty adjustment / 电压占空比 | `timer1_Tick`, token `06000046`, IL `132A–1372` | 4 | `00 15 HH LL` |
| Current duty adjustment / 电流占空比 | `timer1_Tick`, token `06000046`, IL `1378–13C0` | 4 | `00 16 HH LL` |

`HH LL` is big-endian `integer(displayed duty × 10)`. Example: displayed duty 80.0 produces `03 20`; 80.1 produces `03 21`. The +/- controls move by 0.1 and clamp within 0..1000 in the legacy code. **That UI range is not a validated safe hardware range.** The duty field's true relationship to converter PWM and the sign/gain of the response need bench measurement.

Important distinction: the old application changes **charger duty**, not the Maynuo load setting. The likely loop is load in CV at 48 V → measure actual current → adjust charger current duty, followed by load in CC at 1 A → measure actual voltage → adjust charger voltage duty. This is an engineering interpretation of the supplied workflow and code, not a proven control model. Confirm it before energizing an automated loop.

No explicit positive acknowledgement for either calibration save was identified in the receive/display code. A successful API transmit must not be shown as “calibration saved and verified.” Use measured post-save checks, and verify persistence through a controlled restart if that is required by the production procedure.

## Complete parameter panel

Every setting write uses DLC 4: `00 SS HH LL`, where `SS` is the selector and the final word is big-endian. The table's multiplier converts displayed units into the raw word. The old sender truncates floating-point values after scaling; the supplied offline codec instead rejects unsupported precision to avoid silent changes.

Readback IDs are extended. Offsets are zero-based byte offsets. Two-byte readback words are decoded **signed** by the old application; nonnegative writes above 32767 raw therefore cannot round-trip cleanly in its display. The offline codec conservatively limits such writes to 32767 raw. This representable range does not establish a safe charger operating range.

| Saved profile key | UI meaning | Input → readback box | Selector | Multiplier | Readback ID / offset |
|---|---|---|---|---:|---|
| `batteryType` | Battery type code | 1 → 22 | `01` | 1 | `18FF53E5` / 0 |
| `ratedVoltage` | Rated voltage, V | 2 → 23 | `02` | 10 | `18FF53E5` / 2 |
| `startVoltage` | Start-charge voltage, V | 3 → 24 | `03` | 10 | `18FF53E5` / 4 |
| `temperatureLimit` | Temperature protection point, °C | 4 → 25 | `04` | 1 | `18FF53E5` / 6 |
| `maxChargeMinutes` | Maximum charging time, min | 7 → 26 | `05` | 1 | `18FF54E5` / 0 |
| `endVoltage` | Full-charge/end voltage, V | 8 → 27 | `06` | 10 | `18FF54E5` / 2 |
| `ccCurrent` | Charging/CC current, A | 6 → 28 | `07` | 10 | `18FF54E5` / 4 |
| `derating110` | 110 V derating, % | 9 → 31 | `08` | 1 | `18FF54E5` / 6 |
| `derating220` | 220 V derating, % | 10 → 30 | `09` | 1 | `18FF55E5` / 0 |
| `endCurrent` | Full-charge/end current, A | 5 → 29 | `0A` | 10 | `18FF55E5` / 2 |
| `blindCharge` | “Blind charging” code / 盲充 | 71 → 70 | `0C` | 1 | `18FF55E5` / **byte 5 only** |
| `rechargeVoltage` | Recharge voltage, V | 73 → 72 | `0D` | 10 | `18FF55E5` / 6 |
| `adjustCurrentMin` | Adjustable-current lower limit, A | 82 → 81 | `0E` | 10 | `18FF56E5` / 6 |
| `adjustCurrentMax` | Adjustable current, A | 78 → 77 | `0F` | 10 | `18FF56E5` / 4 |
| `adjustVoltageMin` | Adjustable-voltage lower limit, V | 76 → 74 | `10` | 10 | `18FF56E5` / 2 |
| `adjustVoltageMax` | Adjustable voltage, V | 80 → 79 | `11` | 10 | `18FF56E5` / 0 |

Box numbers in the table mean `textBoxN`. The “Max” names for adjustable V/I distinguish them from the explicitly labeled lower limits; the original upper fields are labeled simply “adjustable voltage/current.” Their exact operating role should be verified against the firmware.

Battery-type and blind-charge enumerations are not documented by this binary. In particular, **do not assume `blindCharge=1` is the required non-CAN mode**. Store the proven per-variant numeric values and keep “intended non-CAN operation” as a separate application test requirement until the firmware meaning is confirmed.

Write evidence: `timer1_Tick`, IL `13E9–1A99`. Receive evidence: `SetTEXTBox`, token `06000036`, IL `22A3–29FB`.

## Sequencing and readback

Clicking Send data (`button12_Click`, token `0600005B`) sets `downloaddata=16`. The timer then sends **one setting per eligible tick** in this order:

`10, 11, 0E, 0F, 0D, 0C, 01, 02, 03, 04, 05, 06, 07, 08, 09, 0A` (hex selectors).

After duty edits, `checkpwm=3` and the timer sends **both** voltage and current duty frames on each eligible tick, decrementing the counter. Thus one edit normally results in three pairs, preserving the other axis value. These updates take precedence over configuration writes within the relevant timer branch.

`button5_Click` (Cal. start) likewise sets `checkpwm=3` and `run=1` after its start command. Startup therefore includes the displayed voltage/current duties on the next three eligible timer ticks, not just the one start frame. Both duty text boxes initialize to `80.0` in `InitializeComponent`; these are UI seeds, not readbacks or established factory calibration. +/- changes a duty by `0.1` and schedules the pairs; direct text validation formats/clamps the input but does not itself schedule transmission. Save handlers send the voltage/full-current/half-current save command on the corresponding button click.

When no duty/configuration write is pending in that branch, the timer cycles poll requests `00 0B 00 GG`, where `GG=01..06`, DLC 4. This behavior is evidenced at IL `1AA4–1BD7`; it is not an acknowledgement scheme. The receiver recognizes six diagnostic/readback IDs `18FF51E5..18FF56E5`, consistent with the six groups, but a one-to-one request/response timing relationship has not been captured on a bus.

`button1_Click` starts the CAN channel and enables the timer. No explicit interval assignment was found for **Form1's** timer; other forms' unrelated logging timers have explicit intervals. Capture timing on the bench rather than adopting unrelated logging intervals.

For complete configuration verification, compare **fresh** readback for all 16 fields after completing the write sequence. This is an implementation recommendation, **not a recovered firmware prerequisite**. Values that happened to be on screen before Send data do not establish that the new write succeeded.

The Chinese application does not enforce that comparison: `button4_Click.il.txt:3–6` enables the calibration-start button when Shutdown is clicked; `button5_Click` sends the calibration command without comparing configuration readbacks or requiring Send data. `button3_Click_1` (Work start) also has no configuration comparison. Absence of `18FF56E5` blocked the earlier replacement GUI's added gate, not the original application's controls. It does not establish whether the four adjustable settings were applied or what their exact firmware role is. The replacement GUI now separates manual calibration from parameter updating and retains unknown readbacks as unconfirmed; manual operation does not certify configuration verification or persistence.

## Telemetry identified

| Extended RX ID | Bytes | Displayed meaning |
|---|---|---|
| `18FF50E5` | 0..1 / 10, 2..3 / 10, byte 4 | Output voltage V, output current A, raw status byte |
| `18FF51E5` | 0..1, 2..3, 4..5, 6..7 | Input voltage V, PFC voltage V, maximum power W, working minutes; signed BE16 |
| `18FF52E5` | 0..1, 2..3, 4..5 | Protection temperature °C, ambient temperature °C, fault word; BE16 |

Evidence: `SetTEXTBox` IL `1965–22A3`. Other protocol branches also decode `1806B4C4` and additional BMS traffic. The offline codec intentionally omits those alternate interpretations rather than guessing which variant uses them. Status/fault meanings vary with the selected protocol branch; the codec preserves raw values rather than flattening them into a universal fault list.

The original default protocol display labels fault-word bit `0x0020` **“Battery not connected”** (`电池未接`): `SetTEXTBox.il.txt`, IL `1C17–1C35`, tests `err & 32`; `err` is assigned from `18FF52E5` bytes 4/5 at IL `2110–2157`. The alternative protocol-selection branch groups `0x0030` as battery absent/reversed/undervoltage at IL `226E–228C`. This does **not** establish `0x0020` as a shutdown-status bit: the charge-off text at IL `1D56–1D70` tests a different status value from `1806B4C4`, with the bit clear. The application now names only the evidenced default `0x0020` diagnosis and preserves all other bits numerically.

Automation permits an isolated `0x0020` indication only during a commanded calibration shutdown when independent voltage/current measurements are already below the approved shutdown thresholds. Three actual low-output acquisitions remain required. If that indication was present before calibration startup, adjustment/save waits for a fresh post-start zero fault word within a bounded deadline; an old indication aging out is not a clear diagnostic. Fresh active `0x0020`, mixed/other bits, independent load faults and adapter faults still fail the run. This transition rule is application behavior, not a claim that the firmware fault bit means shutdown.

The load bank's calibrated voltage/current readings (or an external calibrated meter) should be the calibration reference. Charger self-reported readings are useful for monitoring and consistency, but cannot independently prove their own accuracy.

## Offline implementation and validation

- `charger-protocol.js`: zero-I/O command/configuration/duty/poll encoder and readback/telemetry decoder; requires explicit extended/data flags, exact receive length, valid byte values, complete parameter sets and representable precision.
- `charger-protocol.test.js`: 69 passing checks, including independently written golden byte vectors, all readback groups, signed temperatures, malformed frames, unknown fields, missing values and the actual nonsequential write order.
- Run: `node reverse_engineering/charger-protocol.test.js`.
- Tests establish consistency of the offline implementation with the static findings. They are not a hardware commissioning result.

## Remaining live-integration evidence

1. Confirm adapter SDK/driver and successful receive/transmit on this exact USBCAN-I (C+); check bitness against its DLLs.
2. Identify the exact charger model/firmware, current/end-voltage/end-current targets and tolerances for each variant, including all 16 panel values.
3. Capture one complete known-good manual session, including both duty values before changes, send-data readback, save events and final settings after restart.
4. The user has confirmed the final requirement: **the charger must operate standalone without CAN commands after calibration**. Establish how this firmware enters and persists that mode, then cross-check standalone behavior without normal BMS command traffic. The meaning of the blind-charge numeric code remains unverified.
5. Establish safe duty limits, response direction, settling time, measurement stability requirements, watchdog behavior and a bench shutdown path. CAN shutdown alone cannot protect a disconnected controller.
6. Validate end-current termination by observing the actual charging transition under the approved load procedure, not by setting the electronic load to that current and reading it back.
7. The user has confirmed the Maynuo load bank is **not connected to the laptop yet**. Connect and identify the appropriate laptop-to-load interface and validate its protocol before enabling automatic load control or treating load readings as automatically acquired.
