# Tasks: Slice 2 — agents, commands, rules

Status: implementation and independent review complete; final validation recorded below. Commit requires owner approval. See [plan.md](plan.md) and approved local specs.
Each behavior change starts with a failing test; boxes mean verified completion.

## Task 1: Discover agent definitions safely
- [x] Resolve GitHub/personal sources and discover approved native formats; named/all selection, stable ordering, duplicate detection and missing-source reporting.
- [x] Reject unsafe names, selected symlinks including parents, malformed UTF-8 and ambiguity. Parse frontmatter with duplicate-key rejection and source-path diagnostics.
- [x] Record red then green: `uv run pytest tests/test_kind_sources.py -q`.

Dependencies: none. Scope: medium. Likely files: `agent_sync/kinds/source_files.py`, `agent_sync/kinds/metadata.py`, `tests/test_kind_sources.py`.

## Task 2: Install and convert agents through the CLI
- [x] Copy native files, prefer native TOML, serialize Markdown conversion and validate required metadata/name matching; report all conversion losses.
- [x] Add tomlkit and a combined CLI plan. Cover dry run, apply, second-run no-op, conflicts, force, prune and HOME isolation. Keep remaining unsupported-kind guards.
- [x] Record red then green: `uv run pytest tests/test_agents.py -q`.

Dependencies: 1. Scope: medium. Likely files: `agent_sync/kinds/agents.py`, `agent_sync/cli.py`, `pyproject.toml`, `uv.lock`, `tests/test_agents.py`.

## Checkpoint A

- [x] Full pytest, Ruff, strict mypy and diff checks pass; pinned tests unchanged.
- [x] Independent reviewer checks completed tasks against the approved specs; findings resolved.
- [x] Report verified results and limitations. Commit only with owner approval; no specs staged.

## Task 3: Generate commands as shared skills
- [x] Discover immediate command files and validate optional metadata/body. Generate SKILL.md plus agents/openai.yaml with explicit-invocation settings.
- [x] Preserve body and approved metadata, report losses and add the approved argument guidance when needed. Deduplicate identical selections and produce deterministic output.
- [x] Record red then green: `uv run pytest tests/test_commands.py -q`.

Dependencies: 1–2. Scope: medium. Likely files: `agent_sync/kinds/commands.py`, `agent_sync/kinds/source_files.py`, `agent_sync/kinds/metadata.py`, `tests/test_commands.py`.

## Task 4: Integrate command ownership and safe pruning
- [x] Command-only manifests create the Claude link. Skills/commands collisions, case collisions and owned kind transitions fail before project writes.
- [x] Missing inventories suppress all pruning. Legacy adoption/removal cannot replace commands; test edited trees, extra files, force, prune and Git exclusions.
- [x] Record red then green: `uv run pytest tests/test_commands.py tests/test_slice2_cli.py -q`.

Dependencies: 3. Scope: medium. Likely files: `agent_sync/cli.py`, `agent_sync/kinds/skills.py`, `tests/test_commands.py`, `tests/test_slice2_cli.py`.

## Checkpoint B

- [x] Full pytest, Ruff, strict mypy and diff checks pass; pinned tests unchanged.
- [x] Independent reviewer checks completed tasks against the approved specs; findings resolved.
- [x] Report verified results and limitations. Commit only with owner approval; no specs staged.

## Task 5: Install rule files and recursive groups
- [x] Discover approved roots and named files/groups; preserve nested paths, route extensions, reject ambiguous selectors and target collisions.
- [x] Copy bytes per file, reject selected symlinks, filter formats and emit a single Codex trust reminder. Test ownership, overlapping selections, empty-directory cleanup and neighboring user files.
- [x] Record red then green: `uv run pytest tests/test_rules.py -q`.

Dependencies: 1–4. Scope: medium. Likely files: `agent_sync/kinds/rules.py`, `agent_sync/kinds/source_files.py`, `agent_sync/cli.py`, `tests/test_rules.py`.

## Task 6: Verify mixed-kind lifecycle
- [x] Mixed subprocess tests prove complete validation precedes project writes; verify dry run, updates, no-op reapply, force, prune and exclusions.
- [x] Cover CLI text, remaining hooks/MCP errors, source identities, kind transitions and HOME isolation.
- [x] Record red then green: `uv run pytest tests/test_slice2_cli.py tests/test_cli.py -q`.

Dependencies: 5. Scope: medium. Likely files: `tests/test_slice2_cli.py`, `tests/test_cli.py`, `agent_sync/cli.py`.

## Task 7: Document and inspect installed artifacts
- [x] Document new kinds, conversion limits, shared commands, rule groups and unsupported features. Validate manifest examples for all supported kinds.
- [x] Apply representative fixtures to an isolated temporary project and verify no-op reapply. Record client versions, available discovery/policy checks and runtime validation limits.
- [x] Record red then green: `uv run pytest tests/test_docs.py -q`.

Dependencies: 6. Scope: medium. Likely files: `README.md`, `config.yaml`, `pyproject.toml`, `tests/test_docs.py`.

## Checkpoint C

- [x] Full pytest, Ruff, strict mypy and diff checks pass; pinned tests unchanged.
- [x] Independent reviewer checks completed tasks against the approved specs; findings resolved.
- [x] Report verified results and limitations. Commit only with owner approval; no specs staged.

## Final validation and limits

- Final full suite: **507 passed in 28.60s** on 2026-10-07.

- Independent reviewer approved the combined implementation after fixing cached GitHub source
  resolution; six regression cases prove no skill-name lookup or network call for cached new kinds.
- Added the specified empty-command inventory diagnostic with a failing test first.
- Ruff passes; strict mypy reports no issues in 32 source files (installed mypy warns that its
  configured Python 3.9 target is unsupported). The four pinned test files are byte-identical.
- ECC ef648e01 isolated personal-source smoke: planner (Claude + converted Codex), native
  docs-researcher, plan command, common/python rules: 21 changes, then 0 on reapply; HOME empty.
- Available clients: codex-cli 0.160.1 and Claude Code 2.1.291. Client discovery and implicit
  invocation enforcement were NOT exercised; no model calls or user configuration changes made.
  Installation/serialization checks do not establish those runtime behaviors.
- Reviewer gates were completed as a combined Slice 2 review; no intermediate commits made.
- Owner approval to commit is pending. Specs remain ignored; root SPEC-core.md removal was
  already staged per the owner's instruction. Unrelated local files remain untouched.
