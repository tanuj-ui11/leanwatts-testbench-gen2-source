# Leanwatts Testbench Gen2 1.1

A Windows production workstation with operator sessions, variant-controlled
parameter verification, automatic current/voltage calibration and post-tests.
The existing native CAN bridge, Maynuo driver, manual controls, theme and
adaptive 7.5 calibration behavior are retained.

## Install and run

Use `Leanwatts-Testbench-Gen2-Setup-1.1.exe` for the complete per-user installation and
desktop shortcut. Choose the previous `calibration_data` directory to import
an older portable installation. The installer preserves existing installed
data and retains the previous installation as a backup. A portable ZIP and
small update from an installed Gen2 1.0 station are also provided. See `README-Portable.txt` for
operator setup and operation.

Version 1.1 adds Leanwatts logo branding and Windows icons, Light/Dark appearance
with three accent colours in Settings, and an About page. Appearance preferences
are saved per station, and reports gain subtle branding. The charger calibration
and test behavior is unchanged from Gen2 1.0.

## Production flow

An authenticated operator opens a session. Each scanned serial creates a new
attempt with its recipe revision and timestamps. The single start action:

1. Reads charger parameters and compares them with all 16 recipe values.
2. If mismatched, requests shutdown, verifies low output, writes the settings
   once and verifies actual readback before calibration. Missing optional
   adjustable limits are tolerated only for zero-valued recipe entries.
3. Calibrates and saves full current, then voltage using the Maynuo reference.
4. Starts normal operation through CAN and performs full-current, end-voltage
   and cutoff-current tests. Measurements are made during listen-only intervals.
5. Saves the unit result into the active session's combined report.

Required physical results are separate from programmed charger values. The
voltage-test load current must exceed the required cutoff tolerance band.
Calibration initial values are first stable readings after calibration entry,
before tuning; they are not pre-calibration normal-operation baselines.
Post-tests are CAN-started and do not establish CAN-independent operation or
calibration persistence after an AC power cycle. AC switching is deferred.

## Data and access

`calibration_data/operators.json` contains salted PBKDF2 password hashes.
The first account is an administrator; creating accounts, changing roles and
installing software require administrator authentication. Session records retain operator identity.
Local operator login is not a substitute for Windows filesystem permissions.
Administrators can create Production Lead accounts. Leads may reset operator
passwords only; they cannot manage roles or reset administrator/lead passwords.
Changing one's own password requires the current password. No default password
or production account is distributed with the application.

`calibration_data/sessions` contains atomic JSON records plus CSV and printable
HTML reports. Every charger attempt is checkpointed. Interrupted attempts
remain incomplete rather than becoming PASS. The application prevents concurrent
use of its station data. Earlier unit reports remain available in History.

## Updates

Settings supports the application's preconfigured public GitHub release channel
and local test updates. Production laptops do not need a GitHub login, token
or repository details. The updater checks nothing in the background and verifies
product, version, bounded size and SHA-256 before offering installation.

For local updates, select a setup EXE with its adjacent matching `.update.json`.
Same-version full test reinstalls require explicit confirmation; downgrades are
blocked. Full installers remain available. Delta installers carry only
changed files, verify the exact installed base, preserve station data and retain
a backup. A full installer remains the fallback for other starting versions.
The public repository and release must be published before online checking
can return a release. The EXE is unsigned. See
`tools/README-UPDATE-PUBLISHING.txt` for publication instructions.
Version 1.1 is delivered locally; GitHub is reserved for subsequent published updates.
The configured repository has not been created and no GitHub release is published
as part of this package. Local installation and local updates work independently.

## Verification boundary

The GUI has been rendered without device access, and focused software tests use
fake instruments. The complete new sequence has not yet run on the production
bench. API acceptance of a save is recorded distinctly from persistent storage
proof. Simulation-labelled data cannot produce a physical PASS.
