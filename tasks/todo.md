# Tasks: agent-sync, slice 1 (core + skills)

Plan: [plan.md](plan.md). Spec: [SPEC-core.md](../SPEC-core.md).

Every task is done test-first and must pass `uv run pytest`, `uv run ruff check .` and (from
task 2 on) `uv run mypy agent_sync tests` before it counts as done.

## Task 1: Pin today's skill sync with tests

**Description:** Add the pytest harness and tests that capture what the current `skill_sync.py`
does, run through `main()`. Sources are local git repos; GitHub clones are redirected to them with
`url.<base>.insteadOf`; a fake `npx` sits on `PATH`; `HOME` is an empty temporary folder. No
production code changes.

**Acceptance criteria:**
- [x] Covered: `all` vs a named skill; an unknown skill errors and lists the available ones; the
      `common`/`codex`/`claude` sections are merged; the `.claude/skills` link; a dry run writes
      nothing; a second `--apply` changes nothing; a real folder or unmanaged link at a target
      errors and stays untouched; a stale managed link is relinked; `--prune` removes only managed
      links; a missing source without `--apply` is reported; `--update` picks up a new upstream
      skill; an invalid selector errors; the same name from two sources errors; npx installs one skill.
- [x] All tests pass against the unchanged `skill_sync.py`, and a test proves clones go to the
      local repos, not to github.com.
- [x] `HOME` is still empty after every test.

**Verification:** `uv run pytest -q`; `uv run ruff check tests`.

**Dependencies:** none

**Files likely touched:** `tests/conftest.py`, `tests/test_skills_pinned.py`, `pyproject.toml`
(pytest settings only)

**Estimated scope:** M

## Task 2: Move the code into the `agent_sync` package

**Description:** Split `skill_sync.py` into `agent_sync/` (`__init__.py`, `__main__.py`,
`cli.py`, `sources.py`, `kinds/__init__.py`, `kinds/skills.py`) with identical behavior. Rename
the distribution and the command to `agent-sync`, point the test adapter at the package, and
delete `skill_sync.py`.

**Acceptance criteria:**
- [x] The task 1 tests pass; the only test change is the adapter line in `conftest.py`.
- [x] `uv run agent-sync --help` and `uv run python -m agent_sync --help` work; there is no
      `skill-sync` command any more.
- [x] `uv run mypy agent_sync tests` (strict) and `uv run ruff check .` are clean.

**Verification:** `uv sync && uv run pytest -q && uv run ruff check . && uv run mypy agent_sync tests`

**Dependencies:** Task 1

**Files likely touched:** `agent_sync/*` (new, moved code), `pyproject.toml`, `tests/conftest.py`,
`skill_sync.py` (deleted)

**Estimated scope:** M (mechanical move; more files, little new logic)

## Checkpoint A
- [x] Task 1 and 2 checks pass
- [x] Reviewer agent: behavior unchanged, pinned coverage matches the README and `main()`
- [x] Report to the owner; commit 1 after their OK

## Task 3: Manifest format v2

**Description:** `agent_sync/manifest.py` parses `skills`, `agents`, `commands`, `rules`,
`hooks` and `mcp`, each with `common`/`claude`/`codex`, into typed entries using today's selector
grammar. `hooks` mappings are passed through unparsed.

**Acceptance criteria:**
- [x] Today's manifests, including the repo's `config.yaml`, produce the same skill entries as
      before.
- [x] An unknown top-level key or section, or `npx/` under any kind except `skills`, is an error
      that names it.
- [x] Harness resolution: `common` gives both, `claude`/`codex` give one; `skills` and `commands`
      are one pool.

**Verification:** `uv run pytest tests/test_manifest.py -q`, plus ruff and mypy

**Dependencies:** Task 2

**Files likely touched:** `agent_sync/manifest.py`, `tests/test_manifest.py`

**Estimated scope:** S

## Task 4: Ownership state

**Description:** `agent_sync/state.py` loads and saves `.agents/agent-sync.state.json`
(version 1) atomically, hashes files, folder trees and entries (canonical JSON), takes a
non-blocking `flock` on `.agents/agent-sync.lock`, and classifies a target as absent,
owned-unchanged, owned-edited or not-ours.

**Acceptance criteria:**
- [x] Load/save round-trips; an unknown `version` is an error.
- [x] The classification is tested for files, folder trees, symlinks and entries.
- [x] A second run while the lock is held fails at once with "another agent-sync run is in
      progress"; it doesn't hang.

**Verification:** `uv run pytest tests/test_state.py -q`, plus ruff and mypy

**Dependencies:** Task 2

**Files likely touched:** `agent_sync/state.py`, `tests/test_state.py`

**Estimated scope:** S

## Task 5: Engine: plan validation, apply, prune

**Description:** `agent_sync/engine.py` defines `Op` and `Locator`, validates the whole plan
against state and disk before writing anything, applies `link`, `write` and `remove` atomically,
saves state after every operation, and computes prune operations from items in state that the plan
no longer produces. `merge` raises `NotImplementedError` until the hooks and mcp slices.

**Acceptance criteria:**
- [x] A conflict anywhere in the plan, including the last operation, means nothing is written.
- [x] Applying the same plan twice: the second run executes 0 operations.
- [x] An edited item errors without `--force` and is overwritten or pruned with it; something not
      owned is never touched, even with `--force`.
- [x] A failure injected after operation k leaves a state file that matches the disk. Folders the
      engine created are removed once empty.

**Verification:** `uv run pytest tests/test_engine.py -q`, plus ruff and mypy

**Dependencies:** Task 4

**Files likely touched:** `agent_sync/engine.py`, `tests/test_engine.py`

**Estimated scope:** M

## Task 6: Git exclude block

**Description:** `agent_sync/gitexclude.py` finds the exclude file with
`git rev-parse --git-path info/exclude`, rewrites only the agent-sync block, adds and removes
entries, and skips with a note outside git.

**Acceptance criteria:**
- [x] Lines outside the block are untouched; the block is created, updated, and removed when it
      becomes empty.
- [x] Works in a linked worktree (the test creates one).
- [x] A project that isn't a git repository gets a note, not an error.

**Verification:** `uv run pytest tests/test_gitexclude.py -q`, plus ruff and mypy

**Dependencies:** Task 2

**Files likely touched:** `agent_sync/gitexclude.py`, `tests/test_gitexclude.py`

**Estimated scope:** S

## Checkpoint B
- [x] Tasks 3–6 checks pass; pinned tests still pass
- [x] Reviewer agent: engine matches SPEC-core (all-or-nothing, ownership, `--force`, prune)
- [x] Report to the owner; commit 2 after their OK

## Task 7: Skills on the engine

**Description:** `kinds/skills.py` becomes a planner that turns entries into `link` operations
for `.agents/skills/<name>` and the `.claude/skills` link. `cli.py` runs manifest → planners →
engine → git exclude. Old links without a state record are adopted; prune goes through state.

**Acceptance criteria:**
- [ ] All task 1 tests pass unchanged.
- [ ] The state file lists every skill link. The exclude block lists every link, the state and lock
      files, and `.claude/skills` if agent-sync created it.
- [ ] A project with old links and no state: the first `--apply` adopts them and makes 0 link
      changes. Prune never removes `.claude/skills`. Adoption never takes over a `.claude/skills`
      symlink that points anywhere other than `../.agents/skills` (pinned in task 1).
- [ ] Only `cli.py` prints (SPEC-core "Code style"); `kinds/skills.py` returns operations
      instead of printing (checkpoint A review).
- [ ] From the checkpoint B review: the planner always emits the `.claude/skills` op, even for an
      empty skills list (otherwise `--prune` removes it); `adopt` is set only for symlinks into the
      cache or `custom-skills/` and for dangling skill links (tested, including that a real folder
      at a skill target is still a conflict); `sync_exclude(apply=False)` runs before the engine
      applies, so a broken exclude block fails before anything is written; `dir` items are never
      passed to `sync_exclude`.

**Verification:** `uv run pytest -q`, plus ruff and mypy

**Dependencies:** Tasks 3, 5, 6

**Files likely touched:** `agent_sync/kinds/skills.py`, `agent_sync/cli.py`,
`tests/test_skills_engine.py`

**Estimated scope:** M

## Task 8: CLI options, locations and docs

**Description:** Cache precedence (`--cache-dir`, `$AGENT_SYNC_CACHE`, `$SKILL_SYNC_CACHE`,
`$XDG_CACHE_HOME/agent-sync`, `~/.cache/agent-sync`); `--personal-dir` / `$AGENT_SYNC_PERSONAL`;
`--force`; the summary line; exit codes. Update `README.md` and `config.yaml` for `agent-sync` and
the new flags, covering skills only; the other kinds are documented in their own slices.

**Acceptance criteria:**
- [ ] Each step of the cache precedence is tested; `personal:` without a personal folder gives an
      error that says how to set one.
- [ ] With `HOME` empty and the cache set elsewhere, a full run leaves `HOME` empty; the exit code is
      0 on success and 1 on any error.
- [ ] `README.md` and `config.yaml` mention only `agent-sync`, never `skill_sync.py`, and no
      longer say "next to the script" (checkpoint A review: `README.md:56`, `:70`,
      `config.yaml:47`). `--help` shows `agent-sync` as the program name, also under
      `python -m agent_sync`.

**Verification:** `uv run pytest -q`, plus ruff and mypy; read the README diff

**Dependencies:** Task 7

**Files likely touched:** `agent_sync/cli.py`, `tests/test_cli.py`, `README.md`, `config.yaml`

**Estimated scope:** M

## Checkpoint C
- [ ] Full suite, ruff and mypy pass
- [ ] Manual: on a copy of `test-for-agent-system` with its `skills.yaml` and
      `SKILL_SYNC_CACHE=~/.cache/agent-skills`, the links match today's; a second run reports
      0 changes
- [ ] Reviewer agent: whole slice against SPEC-core's success criteria
- [ ] Report to the owner; commit 3 after their OK
