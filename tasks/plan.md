# Implementation Plan: agent-sync, slice 1 (core + skills)

## Overview

Move skill-sync into the `agent_sync` package and rebuild it on the core engine from
[SPEC-core.md](../SPEC-core.md), without changing what a skills-only manifest installs. Later slices
(agents, commands, rules, hooks, mcp) plug into this engine; see
[CAPABILITY-MAP.md](../CAPABILITY-MAP.md). Tasks with full acceptance criteria are in
[todo.md](todo.md).

## Architecture decisions

- **Pin before moving.** Tests that capture today's behavior run first against the current
  `skill_sync.py`, then against the package. They call the code through one adapter,
  `tests/conftest.py::run_sync`, which is the only line that changes when the code moves.
- **No test hooks in production code.** Tests redirect GitHub clones to local repos with git's
  `url.<base>.insteadOf` (set via `GIT_CONFIG_*` environment variables), and put a fake `npx` on
  `PATH`. This works unchanged for the old and the new code.
- **Move first, then rebuild.** Task 2 is a mechanical move with identical behavior. The engine comes
  after it, so the suite stays green at every step.
- **`merge` operations arrive with their first users.** Core defines the `Op` and locator types
  now; JSON merging is implemented in the hooks slice and TOML merging (with `tomlkit`) in the mcp
  slice.
- **Codex trust reminder arrives with the first slice that writes Codex hooks, MCP servers or
  rules.** Core only provides the place to print it.
- **`.claude/skills` link:** created if missing, as today, but never removed by prune. agent-system
  may own the same link in projects that use it.
- **Intended behavior changes** (not pinned by the tests): where `personal:` items come from
  (`--personal-dir`), the default cache location, all-or-nothing runs (today a conflict halfway
  through can leave earlier links written), the summary line wording, and the command name.

## Who does what

- The implementer agent builds each task test-first: a failing test, then the code, then cleanup.
- The main session checks each task's diff against its acceptance criteria before starting the next.
- The reviewer agent reviews each phase at its checkpoint; at most 2–3 fix rounds per checkpoint.
- At each checkpoint the main session reports to you and waits for your go-ahead, then commits.
  No AI attribution lines in commit messages.
- Tasks 3, 4 and 6 touch separate files and can run in parallel after task 2.

## Task list

### Phase 1: Safety net
- [x] Task 1: Pin today's skill sync with tests (40 tests; 28 mutations tried, all caught after
      two additions)
- [x] Task 2: Move the code into the `agent_sync` package

### Checkpoint A → reviewer → your OK → commit 1
- [x] Pinned tests pass against the package (59 after review fix round 1); `agent-sync --help`
      works; ruff and mypy are clean

### Phase 2: Core
- [ ] Task 3: Manifest format v2
- [ ] Task 4: Ownership state
- [ ] Task 5: Engine: plan validation, apply, prune
- [ ] Task 6: Git exclude block

### Checkpoint B → reviewer → your OK → commit 2
- [ ] Unit and integration tests for core pass; pinned tests still pass

### Phase 3: Skills on core
- [ ] Task 7: Skills on the engine
- [ ] Task 8: CLI options, locations and docs

### Checkpoint C → reviewer → manual run → your OK → commit 3
- [ ] Full suite, ruff and mypy pass
- [ ] Manual: on a copy of `test-for-agent-system` with its `skills.yaml`, the result matches
      today's links; a second run reports 0 changes

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| The pinned tests miss a behavior you rely on | Med | Pin list built from the README's "Behavior on subsequent runs" and every branch in `main()`; the reviewer checks coverage against both |
| The `insteadOf` redirect doesn't apply to `git clone --depth 1` | Low | Verified first thing in task 1 |
| `flock` is POSIX-only | Low | macOS and Linux only, like today's symlinks; noted in the README |
| Adopting old links takes over a link you made by hand into the cache | Low | Same rule today's `--prune` already uses |
| Existing installs break after the rename | Med | `$SKILL_SYNC_CACHE` is still honoured; checked by the manual run at checkpoint C |
| GateGuard stops the implementer on every new file | Med | Your call: exempt the skill-sync path or switch the gate off for this work |

## Open questions

1. `uv.lock` is untracked. Should it be committed in commit 1, where the dependency set changes?
   `TODOS.md` stays untracked unless you say otherwise.
