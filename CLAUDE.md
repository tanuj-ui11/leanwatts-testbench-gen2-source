# Leanwatts Testbench Gen2 — agent instructions

End-of-line calibration & test station for the JK CAN charger (Windows, Python 3.12 + PySide6).
Start every task by classifying it, then follow the matching path in
[docs/claude/SDLC.md](docs/claude/SDLC.md): **major** work runs as parallel agents through
every phase; **minor** work runs inline.

Read before changing code:
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module map, run sequence, data model, hook points.
- [docs/claude/SKILLS-AND-MCPS.md](docs/claude/SKILLS-AND-MCPS.md) — which skill or tool each phase uses
  (local file; ask the user if it is missing).
- `README.md` — test command and environment.

## Hard rules

- **Bench safety.** Agents run fakes only. Opening a serial port, CAN adapter or GD-Link, flashing
  firmware, or driving the Maynuo load happens only in a supervised bench run the user performs.
  Any new hardware path ships behind the existing gates (simulation never PASS, test mode sends
  zero application CAN frames, unfinished work never PASS).
- **Evidence over claims.** A calibration PASS means "save accepted", not "persisted"; keep that
  distinction in code, reports and summaries.
- **Station data stays local.** `calibration_data/`, operators, sessions and bench artifacts never
  enter git or any external service unless the user names the destination.
- **Releases are immutable.** A shipped version's zips/installers are inputs, never edited. Every
  behaviour change bumps `leanwatts_can/version.py` and gets its own `tools/build_gen2_<ver>_release.py`
  (Windows build machine only).
- **Retired behaviour stays retired.** CAN-start tests and temporary parameter changes were removed
  in 1.1.9; tests assert their rejection.
- The repo is **public**: keep secrets, personal paths, vendor binaries, manuals and decompiled
  vendor code out of commits (see `.gitignore`).
