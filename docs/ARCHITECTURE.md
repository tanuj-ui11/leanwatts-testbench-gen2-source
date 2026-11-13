# Leanwatts Testbench Gen2: architecture and context

This is a quick-start for engineers and AI agents. It describes the code as of **1.1.11**
(`leanwatts_can/version.py:3`, release date 2026-11-12). All `file:line` references are relative
to the repo root unless they start with a module name, in which case they are under `leanwatts_can/`.

**What it is.** A Windows desktop app (Python 3.12, PySide6) for end-of-line calibration and test of
the JK CAN battery charger. It talks to two instruments:

- a **Maynuo M8437B DC electronic load**, using Modbus RTU over USB-serial. The load is the
  independent measurement reference.
- a **ZLG USB-CAN adapter**, through an x86 SDK bridge subprocess (`vendor_can/CanReceiveBridge.exe`).

There is also a separate **GD-Link firmware-flashing** workspace.

**Hardware status.** No complete 1.1.9+ external-restart test flow has run on hardware yet
(`README.md` "Validation status"). Calibration persistence after an AC power cycle is never verified,
and the code reports it as `persistence_verified: false`.

> Read first: `README.md`, `leanwatts_can/test_programs/README.txt`. That README is the authoritative
> description of the current test procedure. `README-CAN-Calibration.md` and `README-Portable.txt`
> were written for 1.1 and are partly stale; see §7.

---

## 1. Entry points and launch path

| Path | What it does |
|---|---|
| `Start-CAN-Calibration.cmd` | Finds `runtime\python.exe` (or the bundled EoL runtime), sets `PYQTGRAPH_QT_LIB=PySide6`, then runs `launch_calibration.py`. |
| `Start-Original-UI.cmd` | Same, but adds `--original-ui`, which opens on the Manual Calibration tab. |
| `tools/CanCalibrationLauncher.cs:17-23` | Installed-app launcher. Runs `runtime\pythonw.exe launch_calibration.py`. |
| `launch_calibration.py` | Adds the repo root to `sys.path` and calls `leanwatts_can.gui.main()`. |
| `gui.py:302` `main(argv)` | Arguments: `--data` (default `<install>/calibration_data`, `gui.py:305`), `--preview PNG` (grabs a screenshot and quits), `--original-ui`, `--simulation`. Takes two `QLockFile`s, `<root>/.app-running.lock` and `<data>/.station-running.lock` (`gui.py:320-327`), so only one instance runs. Without `--simulation` it selects "Live manual control" on the legacy page (`gui.py:333-334`, mode list at `live_page_support.py:63`). |
| `gui.py:35` `CalibrationWindow` | Main window. Building it never opens an instrument. It creates `SessionStore` (`gui.py:46`), builds the tabs (`gui.py:57-124`) and wires the cross-workspace guards. |

**Tabs** (`gui.py:97-103`) and the code behind each one:

| Tab | Page class | Controller / engine | Notes |
|---|---|---|---|
| Auto Calibration Session | `SessionPage` `session_page.py:83` | `ProductionFlow` (in-page worker) | The production screen. Its live bench is supplied by `CalibrationWindow._operator_live_factory` (`gui.py:222`) and gated by `_operator_live_reason` (`gui.py:184`). |
| Test | `TestProgramPage` `test_program_page.py:38` | `TestProgramController` `test_program_controller.py:19` | Engineering runs driven by data-only JSON programs. Writes to `test_runs/`, **not** to sessions (`production_acceptance=False`). |
| Firmware Programming | `FirmwarePage` (alias `FirmwareProgrammingPage`, `firmware_page.py:177,461`) | `FirmwareController` `firmware_controller.py:32` | GD-Link CLI flashing. Never part of calibration. |
| Variant Recipes | `RecipePage` `recipe_page.py:66` | — | Edits `profiles.json`. Shares the manual CAN controls with the legacy page. |
| History & Reports | `SessionHistoryPage` `session_history.py:41` | — | Lists session reports, plus legacy per-run reports from `data/reports`. |
| Load Setup | `LoadSetupPage` `load_setup.py:25` | own probe executor | COM port, fixed M8437B profile, duty bounds. |
| Manual Calibration | `LegacyChargerPage` `legacy_page.py:103` (+ mixin `LivePageSupport` `live_page_support.py:17`) | own live executor | Original vendor-layout UI. Modes: Simulation / Live receive-only / Live manual. |
| Settings | `QTabWidget` | — | Sub-tabs: `AccountSettings` `account_settings.py:189`, `UpdateSettingsPage` `update_settings.py:17`, `AppearanceSettings` `appearance_settings.py:11`, `AboutPage` `about_page.py:13`. |

**Threading model.** Each workspace has a single-thread `ThreadPoolExecutor` (for example
`session_page.py:106`). A 250 ms `QTimer` polls the current future and submits the next `tick`
(`session_page.py:621-662`). Instrument I/O only runs on that worker. Widget state is frozen on the
Qt thread before the worker starts (`gui.py:222-256`).

Only one workspace may own the instruments at a time. This is enforced by `_workspace_busy_reason`
(`gui.py:165`), by `_operator_active`, which disables tabs (`gui.py:143-153`), and by
`SessionPage.start_guard` (`gui.py:113`).

**Shortcuts** (`gui.py:50-52`):

- **F5** always starts `"calibration"`, even in a Test-only session.
- **Esc** stops whichever workspace is running.
- **Ctrl+L** focuses the serial field.
- **Enter** in the serial field uses the session's default process (`session_page.py:185,472`).

---

## 2. Module map (`leanwatts_can/`, 45 modules plus `test_programs/`, `assets/`, `fonts/`)

**UI pages and shell**

| Module | Role |
|---|---|
| `gui.py` | Main window, tab wiring, live-run factory and readiness gates, CLI entry point. |
| `session_page.py` | Production session screen (login, mode, serial scan, Calibration / Test buttons, stage table). |
| `recipe_page.py` | Variant editor. Persists `profiles.json` (`recipe_page.py:334,474`). |
| `session_history.py` | History browser, HTML export (`export_html_reports` `:21`). |
| `load_setup.py` | Load COM settings. `calibration_profile()` (`:211`) freezes the connection plus `AutomationSettings`. |
| `legacy_page.py`, `live_page_support.py` | Manual CAN UI and live modes (receive / manual), `close_for_automatic_run` handoff. |
| `test_program_page.py`, `test_program_controller.py` | Engineering Test workspace and its worker. |
| `firmware_page.py`, `firmware_controller.py` | Firmware workspace. Profiles in `firmware_profiles.json`, evidence in `firmware_runs/`. |
| `account_settings.py` | Login, password-reset and account-admin dialogs. All password handling is delegated to `OperatorStore`. |
| `update_settings.py`, `appearance_settings.py`, `about_page.py` | Settings sub-tabs. |
| `workspace_identity.py` | Operator sign-in and variant context for the Test and Firmware workspaces. |
| `operator_page.py` | **Not wired into the GUI.** Older compact operator page, imported only by `tests/test_operator_page.py`. |
| `theme.py`, `branding.py` | Light/dark design system, `appearance.json`, logo and icon assets. |

**Production flow and sessions**

| Module | Role |
|---|---|
| `production_flow.py` | `ProductionFlow` (`:32`) runs one charger attempt: calibration engine, then an optional test engine. Also derives the PASS gates and builds `unit_result`. `validate_test_recipe` (`:16`). |
| `session_store.py` | `OperatorStore` (`:165`, PBKDF2 accounts) and `SessionStore` (`:284`, session JSON plus CSV/HTML reports). Also `failure_reason` (`:96`) and `stage_failure_reason` (`:52`). |
| `session_seed.py` | Session-local starting-trim seed taken from the first successful unit of the same recipe (`select_session_seed` `:17`, `apply_session_seed` `:61`). |
| `serial_numbers.py` | `validate_serial`. Standard regex `WO-S\d{5}-(\d+\|BS\d+)-\d{3}` (`:5`). "Other number" override. |
| `run_bundle.py` | Evidence folder and ZIP for Test-tab runs (CAN trace CSV/JSONL, result, summary). |
| `test_programs.py` | Validator and loader for the schema-1 JSON test programs. Programs are data only. |

**Automation engine and domain**

| Module | Role |
|---|---|
| `automation.py` | `AutomationSettings` (`:71`), the `Bench` protocol (`:154`), `DemoBench` simulator (`:191`), and `AutomationEngine` (`:357`), a tick-driven state machine for baseline, calibration and the external-restart tests. |
| `core.py` | `Recipe` dataclass (`:51`) with its validators (`validate_calibration` `:90`, `validate` `:114`, `validate_production` `:226`, `validate_parameter_calibration` `:244`). Also the legacy deterministic `CalibrationEngine` simulator (`:321`). |
| `recipe_defaults.py` | `apply_automatic_settings`: derives the load-test plan from the variant. |

**Bench and hardware adapters**

| Module | Role |
|---|---|
| `bench_session.py` | `BenchSession` (`:31`): the real `Bench` implementation over `MaynuoLoad` and `MonitorClient`. Handles exclusive claims, probe/identity checks, parameter preflight, CAN send gating and trace capture. |
| `can_monitor.py` | `MonitorClient` (`:50`): JSON-lines subprocess client for `vendor_can/CanReceiveBridge.exe`. `validate_frame` (`:27`) whitelists command ID `0x1806E550` and the selectors. |
| `protocol.py` | Offline JK charger codec recovered from the vendor executable: `command`, `encode_config`, `encode_all_config`, `encode_duty`, `poll`, `decode`, `verify`, fault descriptions. |
| `maynuo.py` | `MaynuoLoad` (`:114`): M97-compatible Modbus RTU (CRC16, probe, commission, read, configure, set_input, CC ramp, CV→CC transition). `SerialSettings` (`:58`). |
| `device_diagnostics.py`, `connection_report.py` | Read-only Windows PnP and event-log diagnostics for the ZLG adapter. Neither opens a device. |
| `gdlink.py`, `gdlink_cli.py` | GD-Link V3 firmware images. `gdlink_cli.ProgrammingJob` (`:276`) is the backend the controller uses. `gdlink.py` supplies `FirmwareImage` and the virtual-disk path. |

**Updater and installer**

| Module | Role |
|---|---|
| `updater.py` | Manifest validation (`_release` `:106`), GitHub release check (`check_github` `:174`, `check_configured` `:199`), download, `local_update` (`:273`), SHA-256/MZ verification (`:293`), `launch_installer` (`:307`, staged copy, `--wait-pid`). |
| `github_auth.py` | Reads `release_channel.json` (`:25`). Optional GitHub App device flow for private channels. |
| `update_credentials.py` | DPAPI-protected token and auth files (`update-github-token.dpapi`, `update-github-auth.dpapi`). |
| `version.py`, `release_channel.json` | Release identity and update channel. |

**Persistence helpers:** `session_store.atomic_json` (`:154`) does tmp-file + fsync + `os.replace`.
`recipe_page.atomic_json` (`:59`) is the same pattern and is reused by `firmware_controller`.

---

## 3. Production run sequence (Auto Calibration Session)

| # | Step | Implementation | Gates and safety |
|---|---|---|---|
| 1 | **Operator login, session start** | `SessionPage._start_session` `session_page.py:375` → `LoginDialog` → `OperatorStore.authenticate` `session_store.py:192` → `SessionModeDialog` → `SessionStore.start` `session_store.py:327` | Needs an enabled account. The mode is chosen once from `MODES` (`session_store.py:286`): `calibration_only`, `test_only`, `independent`, `both_required`. Writes `sessions/<id>.json` and `sessions/active.json`. Resume is allowed only for the owner or an admin (`session_page.py:387-390`), and a non-owner admin can only close the session (`session_page.py:435`). |
| 2 | **Variant and serial scan** | `set_profiles` (`:279`) from `RecipePage.profiles_changed`; `_readiness` `session_page.py:430` | `validate_serial` with the custom override. Draft variants are blocked for calibration. `SessionStore.readiness` (`:447`) enforces the session mode and `both_required` pairing (the next unit is blocked until the pending one finishes Calibration then Test). Live gate `_operator_live_reason` `gui.py:184` requires: legacy page in Live manual mode, no queued manual CAN op, valid `AutomationSettings`, a non-`simulation_only` recipe, recipe validation, and limits ≤ 500 V / 120 A / 3700 W (`gui.py:216`). |
| 3 | **Start attempt** | `SessionPage.start` `session_page.py:475` | The two buttons map to `"calibration"` (parameters + calibration only) and `"test_only"`. **A production attempt is never calibration + test together** (`session_page.py:488-489`). Factories are built on the Qt thread (`gui.py:222`). `select_session_seed` picks the starting trims. `SessionStore.begin_unit` (`:481`) writes a `RUNNING` attempt *before* any hardware I/O. The worker then runs `ProductionFlow.start` (`session_page.py:532`). |
| 4 | **Bench open** | `prepare()` in `gui.py:237-255` → `BenchSession(...)` → `probe()` | Waits ≤15 s for the manual CAN handoff to confirm "closed" (`gui.py:238-241`). The load identity must match the M8437B profile (`gui.py:253`; `PROFILE_IDENTITY` `load_setup.py:28`). `BenchSession.acquire` (`bench_session.py:282`) takes the class-wide claims on ("CAN", index) and ("LOAD", port) (`:288-293`), re-probes and commissions the load, opens CAN in **normal mode with ACK** (`listen_only=False`, `:323-327`), and enables load remote. |
| 5 | **Baseline** (calibration only, `baseline_required=True`, `production_flow.py:108-110`) | `AutomationEngine._begin_baseline` `automation.py:747` → `_tick_baseline` `:794` | Load OFF, then CV at the engage voltage, then `charger_start_baseline` (CAN Start, single attempt, `bench_session.py:677-695`). Measures open voltage with the load OFF, then current with the load ON. Ends with `_shutdown_for("baseline")` (`automation.py:876`). The `off_baseline` phase confirms 3 low readings, then turns the load OFF (`:1176-1181`). |
| 6 | **Parameter preflight** | `_prepare_calibration` `automation.py:738` → `BenchSession.prepare_charger_parameters` `bench_session.py:425` | Reads all 16 parameters back and compares them with `recipe.device_settings` + `charger_values()`. A missing required readback fails the attempt. If anything mismatches: Shutdown, wait for 3 measured-low readings, write once (`_send("parameter_write")`), read back and verify (`:446-466`). The result goes into `preflight_result` (`status`, `before`, `desired`, `after`, `updated_fields`, `optional_unavailable`, `action`, `error`, `duration_seconds`). |
| 7 | **Current, then voltage calibration** | `_shutdown_for` `:925`, `_configure_stage` `:937` (CV load at `current_cal_load_voltage`, then `charger_start_calibration`), tuning `_adjust_axis` `:1379` / `_discover_direction` `:1441`, `_save_axis` `:1557` | 0.1 duty resolution with bounded coarse jumps (`AutomationSettings.validate` `:109`). Tuning uses 3 stable samples, final confirmation 8. Each save is attempted exactly once (`_saves`, `:1558-1561`; `BenchSession._save_attempts`). Voltage calibration uses CC at `voltage_cal_load_current`. `off_finish` confirms output is low and the load is OFF, then the engine ends with `"CALIBRATED — PERSISTENCE UNVERIFIED"` (`:1182-1186`). |
| 8 | **Calibration verdict** | `ProductionFlow._consume` `production_flow.py:156`, `_calibration_pass` `:131` | Needs: outcome contains CALIBRATED, baseline PASS, both results `SAVE REQUEST ACCEPTED`, the exact stop outcome "Output below approved shutdown thresholds; load OFF confirmed", and preflight PASS. With no test selected the attempt becomes `CALIBRATED`. |
| 9 | **External-restart tests** (a separate `test_only` attempt in production) | `ProductionFlow._start_tests` `production_flow.py:182` → `AutomationEngine.start_test` → `acquire("test")` → `enter_passive` (`bench_session.py:352`) → `_begin_external_test` `automation.py:643` | Load OFF, CV 49 V armed, then the operator turns AC OFF→ON (window `restart_timeout_seconds`, default 120 s). **OCV** (`_tick_external_restart` `:656`): needs a fresh low→high rise, ≥2 s build, and 3 samples over ≥1 s that are in band, within a 15 s window. **Full current** (`_external_measurement_stage` `:722`): CV 49 V, ≥18 s, then a 3 s plateau. **Cutoff** (`_begin_termination` `:1711`, `_tick_termination` `:1746`): `transition_load_to_cc(5 A)`, then 0.1 A steps every 1 s to a 0.5 A floor; output must stay low for 10 s; 180 s overall cap. Then `_finish_test` (`:1621`) and `_cleanup_test` (`:1640`) call `bench.finish_test()`, which confirms load OFF and that parameters were unchanged. |
| 10 | **Test verdict** | `ProductionFlow._test_pass` `production_flow.py:143` | Needs: outcome PASS, `external_restart`, cleanup verified, restore result PASS with no parameter change, and **every CAN TX counter == 0** (`can_tx_count`, `total_can_tx_count`, startup/setup/cleanup/planned). Exactly 3 results, all PASS. |
| 11 | **Record** | `ProductionFlow._finish` `:285` builds `unit_result` → `SessionPage._snapshot_worker` `session_page.py:564` → `SessionStore.finish_unit` `session_store.py:513` | `finish_unit` refuses PASS/CALIBRATED that don't meet the stage rules or that carry `simulation`, and refuses workflow changes. It then computes `failure_reason`, rewrites the JSON and regenerates CSV/HTML (`_save` `:575`). If the worker raises, `_record_unexpected_failure` (`session_page.py:574`) stores `FAIL`. |
| 12 | **Report and close** | `SessionStore._write_reports` `:585` runs after every save; `SessionStore.close` `:544` | Close is refused while any attempt is RUNNING. Sets `status="CLOSED"`, `closed_at`, and removes `active.json`. A `both_required` session with unfinished units closes as `completion_state="INCOMPLETE"`. |

**Safety gates in one place**

- *No CAN sends in test mode.* `BenchSession.__init__` rejects any test start method other than
  `external_restart` and rejects `post_test_can_start` (`bench_session.py:46-49`). `acquire("test")`
  sets `_passive` (`:296`). `_send` raises unless the operation is calibration and not passive
  (`:621-624`). `begin_post_test` always raises (`:361`). `can_monitor.validate_frame` only allows
  whitelisted command frames (`can_monitor.py:27-48`). The engine re-checks TX counters in
  `_assert_passive` (`automation.py:1042`), in `_cleanup_test` (`:1662-1663`) and in `_finish`
  (`:1943-1946`). `ProductionFlow._test_pass` and `_start_tests` add further checks
  (`production_flow.py:184-188`). `_validate_start` rejects CAN-start test methods (`automation.py:522-523`).
- *Simulation can never be PASS.* `ProductionFlow._finish` forces outcome `SIMULATION`
  (`production_flow.py:303-304`). `calibration_result` and `test_result` become `SIMULATION`. The
  engine prefixes `"SIMULATION — "` (`automation.py:1933`). `finish_unit` rejects PASS/CALIBRATED
  with `simulation` (`session_store.py:524-532`). `summarize` only counts `simulation is False`
  (`:371-375`). Seeds are ignored in simulation (`production_flow.py:104-105`). Simulation-only
  recipes are blocked for live runs (`gui.py:209`). Simulation uses `DemoBench` (`session_page.py:501-503`).
- *Interrupted is never PASS.* On start, `SessionStore.__init__` converts leftover `RUNNING` attempts
  to `INTERRUPTED` / `INCOMPLETE` / `NOT RUN` (`session_store.py:299-305`). Stop gives `STOPPED`
  (`production_flow.py:214-224`).
- *Hardware protection.* Identity and rating checks (`gui.py:216,253`); exclusive claims
  (`bench_session.py:288`); load readback-confirmed writes (`:481-531`); shutdown before parameter
  writes (`:446-461`); no automatic CAN reopen (`:569,626`); duplicate-save prevention.

---

## 4. Data model and persistence

### 4.1 `calibration_data/` layout

The default location is `<install root>/calibration_data` (`gui.py:304-305`). When installed this is
`%LOCALAPPDATA%\Programs\Leanwatts-Testbench-Gen2\…` (`tools/CanCalibrationSetup.cs:26-27`). The
installer preserves and imports it (`:584-596`). It is git-ignored, and the in-repo folder is empty.

| Path | Writer | Content |
|---|---|---|
| `operators.json` | `OperatorStore` `session_store.py:169` | `{schema:1, operators:[{id,name,role(admin\|lead\|operator),enabled,salt,iterations:600000,password_hash,self_managed_password?}]}` |
| `profiles.json` | `RecipePage.save_variant` `recipe_page.py:458-474` | List of `{revision:int, recipe:Recipe.to_dict(), production_ready:bool, validation_error:str}`. Keyed by `recipe.name` (case-insensitive). Revision is incremented on each save. |
| `station-load-setup.json` | `LoadSetupPage` `load_setup.py:33` | Port, baud, address, parity, duty bounds. Probe output goes to `diagnostics/load-probe-latest.json` (`:414`). |
| `appearance.json` | `theme.save_theme` `theme.py:249` | Theme key. |
| `update-settings.json`, `*.dpapi`, `updates/` | `updater.py:323,340,270`; `update_credentials.py:12-13` | Legacy update source, tokens, downloaded installers. |
| `sessions/<session_id>.json` | `SessionStore._save` `session_store.py:575` | **Source of truth** for production results (schema below). |
| `sessions/<id>.csv`, `<id>.html`, `<id>.detail.html` | `_write_reports` `:585-790` | Derived. One CSV row per serial summary, a printable summary, and a detailed per-attempt page. |
| `sessions/active.json` | `SessionStore.start/close` | `{session_id}` pointer to the open session. |
| `test_programs/*.json` | `test_programs.ensure_programs` `:109` | Copied from the bundled `04-ocv-current-fixed-cutoff.json`. |
| `test_runs/<run_id>/` (+ `.zip`) | `RunBundle` `run_bundle.py:33` | `run.json`, `program.json`, `recipe.json`, `can-events.jsonl`, `can-trace.csv`, `result.json` (the `unit_result` with `production_acceptance:false`), `load-readings.csv`, `activity.json`, `summary.csv/html`, `READ-ME.txt`. |
| `firmware_profiles.json`, `firmware_images/`, `firmware_runs/<id>/{activity.jsonl,result.json}` | `firmware_controller.py:39,129,188-235` | Flash evidence: `serial`, `operator`, `variant`, `recipe_revision`, `status`, timestamps (UTC). |
| `reports/<run_id>.{json,csv,html}` | `AutomationEngine.write_reports` `automation.py:2015` (only via the unwired `operator_page.py:379`) | "Earlier unit reports" (schema `leanwatts-automation/1`), read by History (`session_history.py:105`). |

### 4.2 Session record (`schema: "leanwatts-session/1"`)

**Top level** (`session_store.py:337-340`, `:577-580`):

- `schema`, `session_id` (format `YYYYMMDD-HHMMSS-<8 hex>` in IST), `application`, `application_version`
- `operator{id,name,role,enabled}`
- `started_at`, `closed_at`, `status` (OPEN/CLOSED), `timezone:"Asia/Kolkata"`
- `session_mode`, `report_paths{json,csv,html,detail_html}` (absolute paths)
- `units[]`: one entry per attempt (audit trail)
- `unit_summaries[]`: one entry per serial + frozen recipe (derived)
- `completion_state`

**Attempt** (`units[]`). These fields are created by `begin_unit` (`:502-509`) and then merged with
`ProductionFlow.unit_result` (`production_flow.py:311-330`) in `finish_unit`. The identity fields are
protected and cannot be overwritten (`session_store.py:536-538`).

| Group | Fields |
|---|---|
| Identity | `attempt_id` (uuid hex), `sequence`, `serial` (stripped), `serial_override` |
| Recipe / variant | `recipe_name`, `recipe_revision` (int), `recipe` (full `Recipe.to_dict()` frozen at start) |
| Time | `started_at`, `finished_at` (IST ISO-8601 `+05:30`, `now_text` `:150`), `elapsed_seconds` |
| Workflow | `run_calibration`, `run_tests`, `workflow_mode` (`parameters_calibration` \| `test_only` \| `parameters_calibration_test`), `test_skipped`, `test_method`, `test_start_method`, `test_program` (null in production) |
| Verdicts | `outcome`: RUNNING, PASS, CALIBRATED, FAIL, STOPPED, INCOMPLETE, SIMULATION, INTERRUPTED. `calibration_result`: RUNNING, PASS, FAIL, SIMULATION, NOT RUN, INCOMPLETE. `test_result`: NOT RUN, PASS, FAIL, INCOMPLETE, SIMULATION. Also `simulation`, `normal_operation_verified`, `persistence_verified` (always false), `diagnostic_only`. |
| Failure | `detail`, `failure_reason` (multi-line, `failure_reason()` `:96`), optional `communication_diagnostics` (`production_flow.py:239-241`) |
| Evidence | `stage_details[]` (`{name, before, after, unit, status, detail, duration_seconds, started_at?, finished_at?, phase?}`; names come from `SessionPage.STAGES` `session_page.py:91`), `baseline_result{status,detail,open_voltage{…},current{…}}`, `preflight_result` (see step 6), `starting_point`, `startup_voltage_result` |
| Raw engine records | `calibration`, `test`: full `AutomationEngine.snapshot()` (`automation.py:1955`) plus `samples`, `automation_settings`, `can_trace`, `trace_events` (`production_flow.py:121-129`). The pass/fail measurements are in `<record>.results[]` (`automation.py:1545-1555`): `{name, target/required, measured, error, tolerance, unit, status, note/detail, programmed, source, timestamp(UTC), sample_count, before, duration_seconds}`. The cutoff result also carries `observed_interval_a` plus the termination evidence. Calibration result names are `Current calibration` and `Voltage calibration` (status `SAVE REQUEST ACCEPTED`). Test result names are `Open-circuit voltage`, `Full current`, `End current / observed shutdown`. |

**Summary** (`unit_summaries[]`, `SessionStore.summarize` `:351-438`). Attempts are grouped by
sha256(serial, revision, recipe) (`_unit_key` `:346`). Each summary has:

- `group_id`, `sequence`, `serial`, `serial_override`, `recipe`, `recipe_name`, `recipe_revision`
- `started_at`, `finished_at`, `attempt_ids`, `attempt_count`
- `calibration_attempt_id`, `test_attempt_id`, `calibration_completed`, `test_completed`
- `calibration_result`, `test_result` (adds `INVALIDATED` when calibration is re-run after a test)
- `outcome` (adds INCOMPLETE), `last_attempt_outcome`, `completion_state`, `next_step`, `detail`
- `workflow_mode`, `failure_reason`, `simulation`
- with `evidence=True`, also `calibration`, `preflight_result`, `baseline_result`, `starting_point`,
  `test`, and merged `stage_details`

**For per-serial export, use the summary.** In `both_required` mode, calibration and test are
separate attempts, and only the summary combines them into one verdict per charger.

### 4.3 Trimmed example (an attempt as stored; heavy arrays elided)

```json
{
  "schema": "leanwatts-session/1", "session_id": "20261112-101500-1a2b3c4d",
  "application": "Leanwatts Testbench Gen2", "application_version": "1.1.11",
  "operator": {"id": "OP07", "name": "R. Kumar", "role": "operator", "enabled": true},
  "started_at": "2026-11-12T10:15:00+05:30", "closed_at": null, "status": "OPEN",
  "timezone": "Asia/Kolkata", "session_mode": "both_required", "completion_state": "INCOMPLETE",
  "units": [{
    "attempt_id": "9f0c…", "sequence": 1, "serial": "WO-S00430-1-001", "serial_override": false,
    "recipe_name": "48V-20A", "recipe_revision": 3, "recipe": {"name": "48V-20A", "cc_current": 20.0, "end_voltage": 58.4, "...": "..."},
    "started_at": "2026-11-12T10:16:02+05:30", "finished_at": "2026-11-12T10:21:40+05:30", "elapsed_seconds": 338.1,
    "run_calibration": true, "run_tests": false, "workflow_mode": "parameters_calibration", "test_skipped": true,
    "outcome": "CALIBRATED", "calibration_result": "PASS", "test_result": "NOT RUN",
    "simulation": false, "persistence_verified": false, "failure_reason": "", "detail": "Parameters checked and calibration completed; …",
    "preflight_result": {"status": "PASS", "updated_fields": ["endVoltage"], "before": {"...": 0}, "after": {"...": 0}},
    "baseline_result": {"status": "PASS", "open_voltage": {"measured": 57.9}, "current": {"measured": 19.4}},
    "stage_details": [{"name": "Current calibration", "before": 19.4, "after": 20.02, "unit": "A", "status": "PASS", "duration_seconds": 61.2}],
    "calibration": {"schema": "leanwatts-automation/1", "run_id": "…", "results": [{"name": "Current calibration", "measured": 20.02, "status": "SAVE REQUEST ACCEPTED"}], "samples": ["…"], "can_trace": ["…"], "events": ["…"]},
    "test": null
  }],
  "unit_summaries": [{"group_id": "…", "serial": "WO-S00430-1-001", "outcome": "INCOMPLETE",
    "calibration_result": "PASS", "test_result": "NOT RUN", "next_step": "Test", "completion_state": "INCOMPLETE",
    "attempt_ids": ["9f0c…"], "failure_reason": "Test has not been completed for this charger."}]
}
```

### 4.4 Recipes, operators, settings

- `Recipe` fields (`core.py:51-73`):
  - acceptance targets: `name`, `cc_current`, `end_voltage`, `end_current`, and their `*_tolerance`
  - load settings: `current_cal_load_voltage`, `voltage_cal_load_current`
  - bench limits: `max_voltage`, `max_current`, `max_power`
  - `device_settings{…}` (the other charger parameters)
  - `programmed_cc_current`, `programmed_end_voltage`, `programmed_end_current` (firmware values; `null` means use the target)
  - `cc_test_load_voltage`, `voltage_test_load_current`, `termination_test{enabled,…}`, `test_plan{…}`
- Variant identity is **name + revision**. There is no separate SKU or ID.
- The operator never appears on an attempt; it is held only at session level. Every attempt in a
  session is run by the session operator, because `_readiness` enforces this (`session_page.py:435`).

### 4.5 Natural export hook points (for the PDI / Testing-QA integration)

No outbound integration exists today. The only network code is `updater.py` and `github_auth.py`.

1. **Per-attempt completion (primary).** `SessionStore.finish_unit` (`session_store.py:513-542`),
   right after `self._save()`.
   - At that point the stored attempt has its final `outcome` and `failure_reason`, and
     `self._active` carries the operator and the refreshed `unit_summaries`.
   - Find the summary whose `attempt_ids` contains the attempt and post that.
   - **It runs on the production worker thread** (called from `SessionPage._snapshot_worker`,
     `session_page.py:570`, and `_record_unexpected_failure`, `:589`) while it holds the store lock.
     Do not make a blocking network call there. Write to an outbox (e.g. `calibration_data/outbox/`)
     and send from a separate thread with retries.
2. **Crash recovery.** `SessionStore.__init__` (`:299-305`) marks attempts INTERRUPTED without going
   through `finish_unit`. Hook here as well, or reconcile later.
3. **Session close.** `SessionStore.close` (`:544`) is the natural place for a batch or reconciliation
   push, and for `both_required` units left INCOMPLETE.
4. **Sidecar or offline option, no app change needed.** Poll `sessions/*.json` (skip `active.json`).
   Each file is replaced atomically. `SessionStore.list_sessions()` (`:557`) / `summarize()` can be
   imported without Qt. Use `attempt_id` / `group_id` as idempotency keys.
5. **Secondary sources.** `test_runs/<id>/result.json` (engineering runs, `production_acceptance:false`,
   `test_program_controller.py:180-189`) and `firmware_runs/<id>/result.json` (`firmware_controller.py:232`).

When exporting:

- Drop `calibration.samples`, `can_trace`, `trace_events` and `events`. They are large.
- Convert IST/UTC timestamps to a single zone.
- Map `SIMULATION`, `INTERRUPTED`, `STOPPED` and `INCOMPLETE` to non-pass.
- Treat `persistence_verified:false` as a standing caveat on every result.

---

## 5. Configuration and update channel

- **`release_channel.json`.** Contents: `{schema:1, visibility:"public", owner:"pradeep-lw",
  repository:"leanwatts-testbench-gen2", manifest_asset:"update-manifest.json"}`. Validated by
  `github_auth.channel_config` (`:25-54`). A private channel additionally needs `repository_id` and
  `client_id` (GitHub App device flow). Per the publishing README, the repo/release has not been published.
- **Update check is explicit only.** `check_configured` → `check_github` fetches
  `releases/latest`, then the manifest asset, then `_release` (`updater.py:106-149`).
  - `_release` checks: schema 1, `application == "leanwatts-testbench-gen2"`, a `packages[]` list
    (≤30). It prefers a delta whose `base_version` equals the installed version, then falls back to
    a full installer.
  - Each installer must have a SHA-256, a size ≤ max, and an `.exe` name.
  - The asset size must match the manifest (`:192-193`).
  - The update is refused while any workspace, the session, or the manual CAN is busy (`gui.py:130-133`).
  - Changing the update source requires an admin login (`gui.py:135-141`).
- **Local update.** `local_update` (`:273`) takes an EXE plus the adjacent `.update.json`. It blocks
  downgrades and requires an exact base for deltas. A same-version reinstall needs confirmation.
- **Install.** `launch_installer` (`:307`) copies the EXE to a temp dir, re-verifies it, then
  launches it with `--wait-pid`. The installer (C#, `tools/CanCalibrationSetup.cs`) installs per user,
  stages the update, keeps a backup, and preserves `calibration_data`. Delta installers verify the
  exact installed base hashes. Binaries are **unsigned**; checksums detect damage but do not
  authenticate the publisher.
- **Builds** (Windows only; they use `csc.exe`, `tools/build_gen2_delta.py:23`):
  - Scripts are named `tools/build_gen2_<digits>_release.py`. The digits are the version with the
    dots removed: `11` = 1.1, `111` = 1.1.1, `1110` = 1.1.10, `1111` = 1.1.11. Each has hard-coded
    `VERSION` / `BASE_VERSION` constants and an `APP_CHANGES` allowlist that is overlaid on the
    previous full ZIP.
  - `build_gen2_1112_release.py` (1.1.11 → 1.1.12) already exists.
  - Shared helpers: `build_gen2_release.py` (`artifact`, `compile_cs`), `build_gen2_delta.py`
    (`--base --target --version --output`), `build_gen2_source.py`, `build_gen2_web_setup.py`
    (`Install-Latest.exe`), `verify_gen2_package.py`, `smoke_portable.py`.
  - Outputs go to `dist/`: `Leanwatts-Testbench-Gen2-<v>-win64.zip`, `-source.zip`, `Setup-<v>.exe`,
    `Update-<base>-to-<v>.exe`, `.update.json`, `update-manifest-gen2-<v>.json`. Build reports go to
    `artifacts/gen2-<v>-packaging.json`. Both folders are git-ignored.
- **Bundled test program.** `leanwatts_can/test_programs/04-ocv-current-fixed-cutoff.json`
  (`workflow: full_cycle`, `start_method: external_restart`, `procedure: ocv_current_fixed_cutoff_v1`).
  The fixed CV 49 V, CC 5 A, 0.1 A steps and 0.5 A floor are code, not configuration.

---

## 6. Test suite

Layout: `leanwatts_can/tests/` contains 56 `test_*.py` files with about 600 `def test_` functions.
They use `unittest` only and need no pytest. Run them from that folder:

```bash
cd "leanwatts_can/tests"
QT_QPA_PLATFORM=offscreen PYTHONPATH=../.. ../../.venv/bin/python -m unittest discover -s . -p 'test_*.py'
# single module / case
QT_QPA_PLATFORM=offscreen PYTHONPATH=../.. ../../.venv/bin/python -m unittest test_production_sessions -v
```

The venv lives at `.venv`. Its dependencies are listed in `requirements-dev.txt`: PySide6-Essentials
6.12.0, pyqtgraph, pyserial, numpy. On Windows, use `..\..\.venv\Scripts\python` and drop
`QT_QPA_PLATFORM`. Tests never open serial, CAN or network. The top-level `tests/` directory is empty.

| Area | Key files | Fakes / fixtures |
|---|---|---|
| Real bench adapter | `test_bench_session.py`, `test_external_restart_bench.py`, `test_shutdown_priority.py`, `test_bench_trace_program.py` | `test_bench_session.py`: `Station` (`:131`, mutable instrument state plus a `calls` log), `FakeLoad` (`:30`), `FakeCan` (`:95`, records `can.send`, can fail sends or start in the wrong mode), `FakeEvent` (`:14`). |
| Engine | `test_automation.py`, `test_baseline.py`, `test_external_restart_pipeline.py`, `test_normal_test_pipeline.py`, `test_charger_fault_workflow.py`, `test_experimental_automation.py` | `DemoBench` (`automation.py:191`) and deterministic `now` passed to `tick(now)` |
| Sessions / reports | `test_production_sessions.py`, `test_session_pairing.py`, `test_session_workflow_options.py`, `test_simple_session_reports.py`, `test_session_seed*.py`, `test_test_only_flow.py` | Temp data dirs plus `DemoBench` |
| UI | `test_gui.py`, `test_session_ui_v8.py`, `test_live_*_ui.py`, `test_legacy_page.py`, `test_recipe_save.py`, `test_operator_page.py` | Offscreen Qt |
| Adapters | `test_maynuo*.py`, `test_can_monitor.py`, `test_protocol.py`, `test_gdlink*.py`, `test_device_diagnostics.py` | Fake serial/subprocess |
| Release / update | `test_updater.py`, `test_update_settings.py`, `test_github_auth.py`, `test_release_113.py`; also `tools/test_gen2_delta.py`, `tools/test_gen2_installer_permissions.py` | No network |

`tests/render_preview.py` is a manual render script, not a test (see §7).

---

## 7. Known gaps and risks (cited)

1. **Hardware validation.**
   - The 1.1.9+ external-restart test flow has not run on hardware (`README.md`).
   - Persistence is hard-coded unverified (`production_flow.py:330`, `automation.py:2009`).
   - The AC restart is inferred from a DC low→high transition, not from instrumented switching
     (`ac_switch_instrumented=False`, `automation.py:652`).
2. **Stale operator docs.**
   - `README-Portable.txt` / `README-CAN-Calibration.md` (1.1) describe a single "START CALIBRATION
     + TEST" action and CAN-started post-tests.
   - The code has separate *Parameters + Calibration* / *Test* buttons (`session_page.py:195-201`).
     It also rejects CAN-start tests outright (`bench_session.py:46-49`, `automation.py:522-523`).
   - `README.md` lists the parameter read first, but the code runs the baseline first
     (`production_flow.py:109`, `automation.py:625-628`).
3. **Integration-relevant data shape.**
   - The operator is stored only at session level.
   - Variants have no stable ID (they are matched by casefolded name, `recipe_page.py:465`).
   - Timezones are mixed: session/attempt times use a hard-coded IST `+05:30` (`session_store.py:22`),
     while engine records, run bundles and firmware runs use UTC (`automation.py:25`).
   - `report_paths` are absolute and frozen at session start (`session_store.py:570-573`).
4. **Session file growth.**
   - Each attempt embeds full `samples` and the untruncated `can_trace` (`production_flow.py:121-129`).
     By contrast, `write_reports` caps the trace at 10 000 (`automation.py:2034`).
   - Every `begin_unit` / `finish_unit` rewrites the whole JSON plus CSV and two HTML files
     (`session_store.py:575-583`, `:786-790`).
5. **Silent skips.** `SessionStore.list_sessions` drops unreadable or invalid session files without
   reporting them (`session_store.py:563-567`). History and any exporter built on it would miss them.
6. **No export path for interrupted attempts.** These are created in `SessionStore.__init__`
   (`:299-305`), not by `finish_unit`.
7. **Ignored profile flag.** `_operator_live_factory` hard-codes `termination_ramp_approved=True`
   and `exclusive_confirmed=True` (`gui.py:248-249`). The load profile reports
   `"termination_ramp_approved": False` (`load_setup.py:237`), and that value is never used.
8. **Dead or stale code.**
   - `operator_page.py` is not reachable from the GUI. It is the only writer of `data/reports`
     (`operator_page.py:379`).
   - `tests/render_preview.py` calls `window.timer.stop()`, but `CalibrationWindow` has no `timer`
     attribute (only `_close_timer`, `gui.py:53`). The script would fail.
9. **Shortcut mismatch.** F5 always requests `"calibration"` (`gui.py:258-260`). In a `test_only`
   session it is rejected by readiness rather than mapped to Test.
10. **Accounts.** The minimum password length is 4 characters (`session_store.py:208`). Local login
    is not a filesystem control; `README-CAN-Calibration.md` says so explicitly.
11. **Releases.** Installers are unsigned. The publishing README says the configured GitHub repository
    has not been created or published, so online update checks currently fail. Build scripts are
    one file per version with hard-coded constants, and the `111` / `1110` naming is easy to confuse.
