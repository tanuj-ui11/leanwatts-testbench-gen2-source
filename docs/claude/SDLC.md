# SDLC workflow

Standing instruction from the project owner: **major tasks run as multiple parallel agents**,
using every relevant skill and tool. Minor tasks run inline. The orchestrator (the main session)
plans, dispatches, reviews and integrates; it does not write feature code itself on major tasks.

Tool and skill choices per phase: [SKILLS-AND-MCPS.md](SKILLS-AND-MCPS.md).

## 0. Classify

| Class | Test | Path |
|---|---|---|
| **Major** | New feature, behaviour change, bug with an unknown cause, change touching 2+ modules or anything the operator sees, release, external integration | Phases 1–8, parallel agents |
| **Minor** | One-file fix with a known cause, wording, docs, a question | Inline: change → focused tests → full suite → commit |

Done when the class and path are stated to the user in one line.

## 1. Discover — parallel read-only agents

Fan out 2–4 read-only agents (Explore / general-purpose), one per question: affected modules,
existing patterns, current test coverage, hardware/protocol constraints, external system
(e.g. ops dashboard via the Leanwatts MCP). Each returns facts with `file:line`.
UI work also runs `ui-consistency:finding-patterns`.

Done when every module the change touches is named with its owning tests, and open questions
are listed.

## 2. Specify — with the user

Write `docs/specs/<yyyy-mm-dd>-<slug>.md`: problem, acceptance criteria (each one checkable),
out of scope, safety impact (CAN/load/firmware/persistence), data-model changes, rollout
(full + delta installer). Use `mattpocock-skills:grilling` on the user for unclear intent,
`engineering:architecture` for an ADR when choosing between designs, and
`zizi-skills:adversarial-review` for anything touching bench safety or the updater.

Done when the user approves the spec (plan mode → approval).

## 3. Plan — independent tasks

Split the spec into tasks with **disjoint file sets**; each task names its files, its tests,
and its completion criterion. Shared files (e.g. `automation.py`, `session_store.py`) get one
owner task; others wait on it. UI tasks follow `ui-consistency:planning`.

Done when every acceptance criterion maps to at least one task and no two parallel tasks
write the same file.

## 4. Build — parallel agents in worktrees

One agent per task, `isolation: "worktree"`, each on its own branch `feat/<slug>-<task>`.
Each agent works **test-first** (`mattpocock-skills:tdd`): a failing test that encodes the
acceptance criterion, then the code, then the focused tests green. Hardware paths are tested
through the existing fakes (`Station`, `FakeLoad`, `FakeCan`, `DemoBench`).
Run them as a `Workflow` (pipeline: build → review per task) when there are 3+ tasks.

Done when each task branch has green focused tests and a short report of what changed.

## 5. Verify — independent reviewers

The orchestrator merges task branches into `feat/<slug>` and runs the **full suite**
(command in `README.md`). Then, in parallel and unanchored (give reviewers the diff, not the
author's conclusions):
- `code-review` at `high` for correctness;
- `security-review` when the updater, credentials, operator auth, network or file I/O changed;
- `ui-consistency:verifying` for screens;
- `simplify` for cleanup.
Fix confirmed findings, re-run the suite.

Done when the full suite is green and every confirmed finding is fixed or explicitly deferred
with the user's agreement.

## 6. Bench validation — the user, supervised

Agents prepare a bench checklist from the spec (steps, expected readings, pass/fail per
acceptance criterion, what evidence to capture). The user runs it on the Windows bench;
the orchestrator analyses the captured run folder (`result.json`, `load-readings.csv`,
`can-trace.csv`) against the checklist.

Done when each hardware-facing acceptance criterion has captured evidence, or is listed as
unverified in the release notes.

## 7. Release

Bump `leanwatts_can/version.py`, write `tools/build_gen2_<ver>_release.py` from the previous one,
build full + delta installers on the Windows machine, run the artifact checker
(`artifacts/check_gen2_<ver>_artifacts.py`), apply `engineering:deploy-checklist`.
Open a PR into `main`; after merge, tag `v<ver>` and attach installers as GitHub Release assets.

Done when the tag exists, assets are attached with SHA-256 files, and release notes state
what was and was not verified on hardware.

## 8. Close

Update `docs/ARCHITECTURE.md` for anything structural, record durable decisions as ADRs,
`zizi-skills:status-update` for the user, `zizi-skills:handoff` if work continues later.

Done when docs match the shipped code.

## Git conventions

- `main` is protected by convention: all work lands through PRs from `feat/…` / `fix/…` branches.
- One PR per spec; commits small and descriptive.
- Never commit station data, built installers, vendor binaries or personal paths.
