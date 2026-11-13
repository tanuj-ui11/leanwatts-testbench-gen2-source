# Connected adapter check — 10 November 2026

The charger is connected through the user's USBCAN-I (C+) adapter. Windows reports hardware ID `USB\VID_0471&PID_1200`, instance `USB\VID_0471&PID_1200\6&5788826&0&1`, as **ZLG USBCAN**.

## Confirmed blocker

- Device Manager / PnP: Code 39, `CM_PROB_DRIVER_FAILED_LOAD`; problem status `0xC000007B`.
- Installed package: `oem18.inf`, version `1.94.0.2`, dated 27 November 2017.
- Installed `usbcan.sys` is x64 (`0x8664`), with a valid Microsoft Windows Hardware Compatibility Publisher signature. This is **not simply a 32-bit driver on a 64-bit OS**.
- Windows Code Integrity event 3111 identifies this exact `usbcan.sys` as incompatible with hypervisor enforcement (failure bitmap `0x2`, status `0xC0000220`). Memory Integrity is enabled.
- Kernel-PnP event 219 records failure to load `ZLGCAN` for the same device.

Raw evidence is in `adapter-driver-check.json` and `pnp-status.txt`. Those are observations at the recorded time, not permanent device state.

The user confirmed charger AC power on and DC connected; the load input is understood to be off. No vendor DLL was loaded, no CAN channel was opened, and no charger command was sent during this check. No drivers or Windows security settings were changed.

## Next dependency

Obtain a supplier-approved driver for this exact adapter that supports Windows with Memory Integrity enabled. Alternatively, use an adapter with a verified compatible driver. The existing SDK files expose a listen-only mode, but that cannot be used until Windows successfully loads the driver. SDK bitness and adapter behaviour must then be verified before capture.

The official ZLG driver collection page was located, but its download endpoint timed out from this machine. No replacement package was obtained or installed. A signed package alone does not establish Memory Integrity compatibility, as the current failure demonstrates.

References: [Microsoft Code 39 documentation](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/cm-prob-driver-failed-load), [official ZLG CAN downloads](https://www.zlg.cn/can/down/down/id/22.html).

## Retry requested by user

The subsequent check still found the same connected USB-CAN device with Code 39. An attempt to restart only this adapter through Windows PnP was denied for lack of administrator access, including after tool permission escalation. No device restart occurred. This was an operating-system access denial, not an automatic approval-review rejection. `gui-adapter-check.json` records the latest independent native Windows inventory and matching Code Integrity evidence. CAN was never opened.

## Security-setting request and portable handoff

The user subsequently requested disabling protections. The change was limited to the identified blocker: `HypervisorEnforcedCodeIntegrity\Enabled`. Direct registry modification was denied by Windows; a normal Windows administrator approval request then succeeded in setting the value from 1 to 0. Before any restart, the user proposed transferring the GUI to a laptop where the adapter already worked. The same setting was restored to 1 through Windows administrator approval and independently read back as 1. No reboot, CAN access, driver installation or change to other security settings occurred. The original configuration is restored; the brief configuration change was not tested as a driver workaround. A timestamped before-change JSON snapshot is retained here.

The portable GUI ZIP contains its own isolated runtime, does not install drivers or change security settings, and remains an offline engineering preview. Driver readiness must be checked on the destination laptop; this laptop's diagnostic state is not evidence about that machine.
