# GD-Link V3 / GD Utility Programmer automation feasibility

Checked 2026-11-12. Documentation review only: no programmer connection, flashing, software installation, or firmware download was performed. User identified GD-Link V3 and Utility Programmer 2.1 on the production laptop; the exact MCU and installed build remain unknown.

## Verified primary-source findings

The [official V3 guide, revision 1.2](https://www.gd32mcu.com/data/documents/userManual/GD-Link%20V3%20Adapter%20User%20Guide_Rev1.2.pdf) documents these paths:

- Utility programming requires version 2.0.2.34758 or newer. Its GUI connects over SWD/JTAG, identifies the MCU, loads BIN/HEX, and programs it (§3.2.2).
- V3 virtual-disk programming can be enabled in the Utility configuration. Firmware version 2 disables this function by default. Configure `CONFIG.TXT`, then copy BIN/HEX to the GigaDevice virtual disk. The disk unmounts/remounts after programming; `FAIL.TXT` gives failure details, while only `CONFIG.TXT` indicates success. Programming configuration returns to defaults after the adapter loses power (§3.2.5).
- Offline machine triggering uses a 100 ms low pulse on `T_START`; `T_BUSY`, `T_GOOD`, and `T_NG` provide hardware status (§3.2.4).
- Eclipse/OpenOCD programming is documented, but requires the appropriate target configuration (§3.2.1).

The disk configuration examples/table name target core, flash start address, erase method, reset method, read protection, SRAM address, and debug interface. The guide inconsistently calls the flash field `Program Flash start address` versus `Program start address`; inspect and preserve the actual adapter file format. Its example addresses are not this charger's approved map.

The [current official download listing](https://www.gd32mcu.com/en/download?kw=GD-Link) offers Utility Programmer 2.2.3.41771, dated 2026-08-14. This does not establish the installed 2.1 build's exact CLI/SDK interface.

[Official application note AN014](https://www.gd32mcu.com/data/documents/userManual/AN014_EN_Rev1.0.pdf), Figure 4-6, shows an older `GD_Link_CLI` used for Arduino uploading. That is evidence of a historical CLI, not proof of compatibility with GD-Link V3 or the user's Utility 2.1. No current V3 CLI syntax or public SDK contract was verified. Do not invent executable flags or reuse old CLI assumptions.

## Integration recommendation (engineering interpretation)

Prefer the installed tool's documented CLI if its actual package/help confirms one. Otherwise, V3's documented virtual-disk programming is a plausible integration route without brittle screen clicks. This requires commissioning on the production laptop before enablement.

A future application integration should identify the exact USB programmer/volume, bind it to the station, validate firmware hash and target/addresses, apply the approved configuration, initiate once, and wait for the expected detach/remount and result. Copy completion alone is not a programming success. Avoid writing to a drive chosen by letter alone. Report timeout/ambiguous outcome explicitly and do not automatically repeat a flash after uncertain completion.

Still needed: MCU part number; installed programmer folder/build and bundled help; approved BIN/HEX and start addresses; flash/option-byte/erase policy; calibration storage region if programming and calibration share flash. Without the storage map, a reflash must not be assumed to preserve previous charger calibration.

Do not equate page erase with preserving calibration: calibration could share an erased page. Do not automatically alter read protection/option bytes or offer mass erase as a connection-recovery attempt. The V3 disk method's documented success indication is not independent flash readback verification; a future integration must label the evidence it actually acquires.

GUI automation is a fallback only after inspecting the installed application's accessible controls and explicit completion messages. Stable menu/control identifiers and output-state checks are preferable to screen coordinates. No GUI automation was implemented in this review.
