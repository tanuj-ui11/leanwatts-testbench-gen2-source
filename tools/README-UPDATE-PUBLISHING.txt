Leanwatts Testbench Gen2 1.1 - PUBLISHING ONLINE UPDATES

PUBLIC GITHUB RELEASES
The user-approved public repository is recorded in
leanwatts_can/release_channel.json. The publisher signs in to GitHub to create
releases and upload assets. Production laptops use Check for updates without
a GitHub account, token or repository entry. No credentials belong in a release.
Check, download and install are explicit actions; the app does not install
updates during an open operator session.
Version 1.1 is delivered locally. Publish a subsequent version to enable online
updates; creating the local 1.1 packages does not require a GitHub session.
At this build's handover, the configured repository has not been created and
no GitHub release has been published. The preconfigured channel is for later use.

The repository can be transferred to the factory account later. Preserve old
release links during migration and update the configured release channel in a
subsequent app release. Do not silently change a public channel to a private
repository without an authentication migration for the production stations.

FIRST INSTALLATION
Build 1.1 with tools/build_gen2_11_release.py after source edits are final.
The original 1.0 archive is preserved as the base for the small update.
The full installer contains the complete application/runtime.
Publish its setup EXE and the matching .update.json contents, renamed to
update-manifest.json, as assets on a non-draft, non-prerelease GitHub release.
Keep installer.asset_name equal to the uploaded EXE name, and keep its generated
size and SHA-256 unchanged. Publication happens only after the binary upload.
The application's default manifest asset name is update-manifest.json.
For 1.1, update-manifest-gen2-1.1.json lists the small 1.0-to-1.1 installer and
the full-installer fallback. Upload both EXEs and rename that combined manifest
to update-manifest.json. The generated adjacent .update.json files are also
available for local/offline update selection.

NORMAL SUBSEQUENT UPDATES: CHANGED FILES
Keep the complete ZIP for each released version as a build reference. Make
the next complete ZIP with a higher numeric version (for example 1.1.1), then
run tools/build_gen2_delta.py with --base BASE.zip --target TARGET.zip
--version TARGET_VERSION --output UPDATE.exe. This produces a small standalone
installer that carries changed application files instead of reshipping the
unchanged runtime. Its adjacent .update.json contains product, base version,
target version and installer hash. Publish this installer and manifest.

The small installer requires the exact installed base version and base file
hashes. It stages the update, checks the full target manifest, retains saved
station data, and keeps the prior installation for rollback. If a laptop has
a different base, supply the appropriate delta or the full installer.

For several installed bases, a published manifest may contain a packages list
with one package per supported delta plus a full-installer fallback. Each
entry contains package_type, base_version (delta only), and installer. The
top-level application, schema, version and notes are shared. The updater
prefers a delta matching the installed version, then a full installer.

LOCAL TEST VERSION UPDATE
Keep the test installer EXE and its adjacent .update.json together. In Settings,
click Install local test update, select the EXE, and select its matching
manifest if it was not found beside it. The app checks product, version,
filename, size and checksum before offering Install and restart. A same-version
full test reinstall is allowed with explicit confirmation. Older versions are
rejected. Online production updates are offered only for a higher version.
Future delta releases must increment the version; no fake future release is
created by the first-install build.

INTEGRITY AND PUBLICATION STATUS
The configured HTTPS release channel identifies the publisher location.
Checksums detect incorrect/damaged binaries but do not independently
authenticate the publisher. The locally generated EXE is currently unsigned.
If signing is added, regenerate the manifest hash after signing. Do not publish
a token, station data, operator account files or private diagnostics.
Building this package does not create a repository or publish a release.

INSTALLER BEHAVIOR
Installation is per user in LocalAppData\Programs\Leanwatts-Testbench-Gen2.
The desktop and Start menu shortcuts are named Leanwatts Testbench Gen2.
An old CAN Calibration data folder can be imported during first installation.
Install/update requires the application and operator session to be closed.
Recipes, accounts, settings and reports are retained. Drivers and Windows
protections are not modified; the installer makes no hardware calls.

OFFICIAL REFERENCES
https://docs.github.com/en/rest/releases/releases#get-the-latest-release
https://docs.github.com/en/rest/releases/assets#get-a-release-asset
