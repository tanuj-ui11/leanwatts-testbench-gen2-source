# CAN adapter integration: static evidence and next checks

Updated 10 November 2026. This inspection did not load a vendor DLL, open a CAN device, initialize a channel, launch the vendor application or send any CAN frames.

## Current connection finding

The user connected the charger through the USBCAN-I (C+) adapter. The parent agent's Windows device inspection reported:

- Hardware ID: `USB\VID_0471&PID_1200`.
- Device: `ZLG USBCAN`, manufacturer `ZLGMCU`, installed INF `oem18.inf`.
- Device problem code 39 (`CM_PROB_DRIVER_FAILED_LOAD`), problem status `0xC000007B`.

The hardware ID matches the supplied `JKCAN_CHARGE/kerneldlls/USBCAN.INF`. This is stronger evidence of a matching driver family than the exterior product label, but it is **not** proof that a functioning driver or SDK connection exists. Resolve the Windows device problem before attempting SDK access.

The subsequent Windows event-log check identified the cause for this attempt: Code Integrity event 3111 names the installed `usbcan.sys` as incompatible with hypervisor enforcement. Memory Integrity is enabled. The installed driver is x64 and has a valid Microsoft signature, version 1.94.0.2 dated 27 November 2017. See `hardware_checks/adapter-driver-check.json` for the exact evidence and timestamp. No driver or security setting was changed.

## Bundled driver file is not a suitable x64 installation package

The bundled `USBCAN.INF` has a device entry for this ID in `[ZLGMCU]`, but its `[ZLGMCU.NTamd64]`, `[ZLGMCU.NTarm]` and `[ZLGMCU.NTarm64]` sections are empty. It references `usbcan.sys` and `usbcan.cat`; neither is present in that folder. Therefore it is not a complete verified installation package for this x64 laptop and should not be installed as a guessed fix.

The official [ZLG USB CAN downloads page](https://www.zlg.cn/can/down/down/id/22.html) lists a CAN driver collection and a ZLGCAN development library with examples. Its SDK archive link is [CAN_lib.zip](https://www.zlg.cn/data/upload/software/Can/CAN_lib.zip). The driver collection link leads to ZLG's dynamic manual portal. These are sources to obtain a complete signed package, not evidence that a particular downloaded driver is compatible. Match the exact hardware ID, architecture and supported Windows release before installation. No package was installed during this inspection.

## Binary architecture, dependencies and exported APIs

`research/inspect-can-binaries.js` statically parsed PE headers and import/export directories. Full results are in `research/can-binaries-static.json`.

| File | Architecture | Direct non-system runtime dependencies | Relevant exports |
|---|---|---|---|
| `JKCAN_CHARGE/zlgcan.dll` | x86, PE machine `0x014C` | `MSVCP90.dll`, `MSVCR90.dll` | `ZCAN_OpenDevice`, `ZCAN_CloseDevice`, `ZCAN_GetDeviceInf`, `ZCAN_InitCAN`, `ZCAN_StartCAN`, `ZCAN_ResetCAN`, `ZCAN_GetReceiveNum`, `ZCAN_Receive`, `ZCAN_ReadChannelErrInfo`, `ZCAN_ReadChannelStatus`, `GetIProperty`, `ReleaseIProperty`, transmit APIs |
| `kerneldlls/USBCAN.dll` | x86 | `MFC80.dll`, `MSVCR80.dll`, `MSVCP80.dll` | Legacy `VCI_OpenDevice`, `VCI_InitCAN`, `VCI_StartCAN`, `VCI_Receive`, board/status/error APIs and transmit APIs |
| All other supplied `kerneldlls/*.dll` | x86 | Varies; see JSON | Varies by device family |

The root library's file version is `1, 0, 0, 3`. `kerneldlls/dll_cfg.ini` associates device type 3 and type 4 with `USBCAN.dll`. The root library also exports FD/LIN/cloud functions; their presence does not mean this adapter supports those buses or functions.

Do not confuse process bitness with kernel-driver bitness: an x86 application/DLL stack can run on x64 Windows, but the Windows kernel driver must be compatible with the x64 OS. The existing x64 Python/PySide GUI cannot directly load these x86 DLLs. It needs an x86 helper or a verified x64 SDK package that supports this same device.

The DLLs use older C++ runtimes. Availability of those dependencies and their side-by-side manifests has not been tested by loading them. File presence alone is not sufficient to prove a successful load. Do not copy miscellaneous DLLs into system directories as a dependency repair.

## Recovered managed ABI

`research/inspect-can-layout.js` reads the already-extracted CLI metadata from the vendor executable. `research/can-structs-static.txt` records field types, explicit offsets and fixed-array marshal metadata. No assembly was loaded. Native import metadata records calling convention **StdCall** (`ImplMap` flag `0x0300`) for the ZCAN functions.

Recovered signatures relevant to receiving:

```text
IntPtr ZCAN_OpenDevice(uint device_type, uint device_index, uint reserved)
uint   ZCAN_CloseDevice(IntPtr device)
IntPtr ZCAN_InitCAN(IntPtr device, uint channel_index, IntPtr init_config)
uint   ZCAN_StartCAN(IntPtr channel)
uint   ZCAN_ResetCAN(IntPtr channel)
uint   ZCAN_GetReceiveNum(IntPtr channel, byte type)
uint   ZCAN_Receive(IntPtr channel, IntPtr receive_buffer, uint capacity, int wait_time)
uint   ZCAN_ReadChannelErrInfo(IntPtr channel, IntPtr error_info)
IntPtr GetIProperty(IntPtr device)
```

These signatures come from the supplied managed wrapper's metadata. Device information/status and property-release functions exist in the DLL exports but are not declared by that wrapper; verify their signatures in the matching SDK before adding them.

### Initialization structure

`ZCAN_CHANNEL_INIT_CONFIG` uses explicit layout: `uint can_type` at offset 0, classic-CAN and CAN-FD configurations overlaid at offset 4. The CAN-FD member is 28 bytes, so the complete structure is **32 bytes** under this layout. For classic CAN, `can_type=0`.

| Full structure offset | Classic member | Type |
|---:|---|---|
| 4 | `acc_code` | uint32 |
| 8 | `acc_mask` | uint32 |
| 12 | `reserved` | uint32 |
| 16 | `filter` | byte |
| 17 | `timing0` | byte |
| 18 | `timing1` | byte |
| 19 | `mode` | byte |
| 20..31 | Unused union remainder for classic CAN | zero-initialize |

The vendor app explicitly initializes classic CAN with timing/filter/acceptance values. It zero-initializes the configuration and **never assigns `can.mode`**, leaving it at 0. Its `Start CAN` handler also enables its periodic send timer. Therefore the original program is not a passive sniffer merely because the operator has not clicked a charger-action button.

### CAN frame and receive stride

`can_frame`: uint32 ID at offset 0; DLC byte at 4; padding/reserved bytes at 5..7; an inline 8-byte data array at 8..15. Size **16 bytes**.

`ZCAN_Receive_Data`: `can_frame` at offset 0 and uint64 timestamp at 16. Size/stride **24 bytes**. `ZCAN_Transmit_Data`: frame plus uint32 transmit type, size **20 bytes**. These are derived from the recovered metadata and ordinary field alignment; assert these offsets/sizes in a helper's offline layout test before calling the SDK.

The ZLG ID field includes flag bits: bit 31 extended, bit 30 remote, bit 29 error. Mask those flags from the bus ID before applying the charger decoder. An eight-byte allocation is not the same as a received DLC of eight; preserve the actual DLC and ignore malformed/unsupported frame shapes rather than interpreting padding as data.

## Evidence-backed receive-only option

The supplied `kerneldlls/devices_property/usbcan1.xml`, lines 84–94, defines `work_mode` as 0=`normal_mode`, 1=`readonly_mode`. Its locale file `devices_property/device_locale_strings.xml`, lines 303–308, translates `readonly_mode` to **只听模式** (listen-only). This is direct evidence from the same supplied driver package for choosing **classic `can.mode=1`** in a dedicated receiving helper. It still requires a successful driver load and confirmation that this exact device honors that setting.

ZLG's official [CAN test/API manual](https://www.zlg.cn/data/upload/software/Can/CAN_test_um.pdf) describes the legacy CAN interface library and initialization structures; it is supplementary documentation, not a substitute for the exact ZCAN wrapper metadata. Do not transfer mode bits from unrelated CANDTU or FD device structures.

Candidate receiving-only helper after the driver is healthy:

1. Use the verified device type/index/channel and known bench bit rate; 250 kbps is visible in the original screenshot and the supplied USBCAN1 property default, but still confirm the attached charger's rate.
2. Initialize a zeroed 32-byte configuration with `can_type=0` and **`can.mode=1`**. Configure timing/acceptance fields using the matching SDK evidence, rather than reusing the original UI's misleading filter caption.
3. Start the channel, receive into bounded buffers, report raw ID/flags/DLC/data/time and errors, then reset and close in a finally path.
4. Do **not** expose or import transmit functions into this helper, enable periodic polling, call the recovered `00 0B` requests, or execute vendor application timers. This first stage observes only unsolicited traffic.
5. Treat “no frames seen” as inconclusive. A charger may emit data only after requests, the bit rate/filter may differ, and a truly passive listener may not acknowledge a lone transmitter. Do not automatically change to normal mode or inject discovery traffic when nothing appears.

This is a proposed implementation sequence, not an executed hardware test.

## x86 bridge feasibility and workstation policy

The machine contains `C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe` (32-bit host) and `C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe`. A small .NET Framework x86 helper is technically feasible and can exchange bounded JSON records over standard input/output with the x64 GUI. It should separate receive-only monitoring from any later explicitly enabled calibration transport.

Earlier in this session, Windows Application Control blocked direct execution of a newly compiled helper, and Windows PowerShell rejected a `.ps1` because scripts were disabled. This inspection has not changed those policies or demonstrated that a bridge is permitted to execute. An approved/signed helper or another organization-approved hosting route is needed if those blocks persist. Do not bypass Application Control, change execution policy, disable driver-signature protections, or weaken Windows security to make the adapter work.

A 32-bit helper resolves user-mode DLL architecture only. It cannot fix the currently failed Windows kernel driver. That device problem remains the first blocker to live data.
