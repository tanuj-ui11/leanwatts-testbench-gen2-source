LEANWATTS TESTBENCH GEN2 1.1 — PRODUCTION SESSIONS

INSTALL / UPDATE
Open Leanwatts-Testbench-Gen2-Setup-1.1.exe. It installs the complete application
for this Windows account and creates the Leanwatts Testbench Gen2 shortcut.
Close any active charger process and both charger GUIs before installation.
For a first installation, select the previous app's calibration_data folder
when offered, to import saved recipes, connection settings and reports.
Updates to the installed app preserve its data and keep the previous app as
a backup. Drivers and Windows protection settings are not changed.
If Gen2 1.0 is already installed, the smaller
Leanwatts-Testbench-Gen2-Update-1.0-to-1.1.exe updates only changed files.
Use the full installer if the installed 1.0 files have been modified.

APPEARANCE IN 1.1
The application header, Windows icon and small page branding use Leanwatts
artwork. Settings provides Light/Dark appearance, three accent colours and
an About page. The selected theme is saved for this station and does not
affect calibration settings. Reports include small matching brand artwork.

FIRST SETUP
1. Settings > Operator accounts: create the first administrator ID/password,
   then add operators and production leads. No default password is supplied.
   Production leads can reset operator passwords only; administrator accounts
   and role management stay with administrators. Users changing their own
   password must enter their current password. Passwords are stored as salted hashes.
2. Load Setup: select the Maynuo COM port, 9600 baud, address 1, parity N;
   save the connection. Existing working settings are retained when imported.
3. Variant Recipes: select an existing variant or create one. Enter all
   original charger parameters, required measured current/voltage/cutoff and
   tolerances, bench limits, and load settings. Save the variant.
   Required results and programmed values are separate. The voltage-test
   current must exceed the required cutoff band. For example, 1 A is too low
   for a required cutoff of 1.1 A; choose a suitable higher test current.

PRODUCTION
1. Auto Calibration Session > START SESSION: log in with operator ID/password.
2. Select the variant, scan a charger serial, and press START CALIBRATION + TEST.
3. The process reads charger parameters, updates mismatches and verifies
   readback, calibrates and saves current, calibrates and saves voltage,
   then checks full current, end voltage and the end-current cutoff ramp.
4. Review this charger's result, connect the next unit and scan its serial.
5. CLOSE SESSION creates one combined session report for all charger attempts.

Progress shows parameter verification, calibration and the three test stages,
with independent load readings, initial/final measurements and elapsed times.
Calibration's initial measurement is the first stable reading after entering
calibration at the starting duty, before automatic adjustment. It is not a
normal-operation measurement taken before entering calibration mode.

Tuning learns the measured response, uses calculated coarse jumps up to
1.0 duty, then aims for 0.1 A / 0.05 V near target. It uses 3 stable readings
while adjusting and 8 at the target before saving. Existing duty bounds apply.
Four unused adjustable-limit readbacks may be absent only when their recipe
values are zero. Missing required parameters or unconfirmed updates stop the run.

Post-tests use CAN Start for normal charger operation. They are labelled
CAN-started tests, not standalone-without-CAN or power-cycle persistence tests.
AC relay/socket control is not included in this release.
Stop cancels remaining actions; unfinished work cannot receive PASS.
A PASS means the required measured checks passed and calibration saves were
accepted, not that storage persistence after power loss has been proven.

SCREENS
Auto Calibration Session | Variant Recipes | History & Reports | Load Setup |
Manual Calibration | Settings
Manual Calibration retains the working translated original control layout.
Variant Recipes also has Open device / Start CAN / Stop CAN and live parameter
readbacks. Saving a recipe does not transmit it; a production run checks it
and updates the charger only as required. Send data now is an explicit action.

REPORTS AND RECOVERY
calibration_data/sessions contains one JSON, CSV and printable HTML report per
session. Results are saved after each charger. An interrupted charger attempt
is retained as interrupted, never as PASS. The original operator can resume
an open session after logging in, or an administrator can close it.
History & Reports also provides earlier individual-unit reports.

SOFTWARE UPDATES
Settings > Software updates provides Check for updates and Install local test
update. The public release repository is configured in the application. The
production laptop does not need a GitHub login, token or repository details.
Check, download and install are explicit actions after the session is closed.
Version 1.1 is supplied locally. Online updates become available when a future
release is published to the configured public channel.
No GitHub repository or release has been published by this package. Local
installation and local updates work without GitHub.

For a local update, keep the supplied setup EXE and matching .update.json file
together. Select the EXE with Install local test update. The application checks
the product, version, size and checksum before offering Install and restart.
A same-version test reinstall asks for confirmation; downgrades are blocked.
The EXE can also be opened directly as a single-file installer.

The first installation is full. Future small updates can contain only changed
application files, provided the installed base version matches. Recipes,
operators, connection settings and reports are preserved. A full installer
remains available when a matching small update is not available.
No download or installation runs automatically. End the session before updating.
The public GitHub repository/release must be published before online updates
can be used. No live update service is bundled or created by the installer.
The installer is not publisher-signed. It does not disable Windows protections.

VALIDATION
UI previews use isolated sample data and never operate the connected equipment.
Software checks use fake instruments. The complete Gen2 production sequence still needs
its first supervised run on the production charger and load bank.
