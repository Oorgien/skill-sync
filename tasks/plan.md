# Implementation plan: Slice 2 — agents, commands, rules

Status: implemented and independently reviewed; commit approval pending. Product specifications approved 2026-10-06; local-only in
`specs/SPEC-agents.md`, `specs/SPEC-commands.md`, `specs/SPEC-rules.md`.
Completed Slice 1 plan/checklist archived locally as `specs/slice-1-*.md`.

## Implementation order

Build complete CLI paths sequentially: agents, commands, rules, then mixed-kind verification
and documentation. Hooks and MCP remain unsupported. Detailed acceptance criteria: [todo.md](todo.md).

1. Discover agent sources safely and validate frontmatter.
2. Copy/convert agents and integrate the first new planner into the CLI.
3. Generate commands as explicit-invocation skills for both harnesses.
4. Integrate command ownership, shared-name conflicts and safe pruning.
5. Copy rule files and recursive named groups with format routing.
6. Verify mixed-kind lifecycle and complete CLI behavior.
7. Update documentation and inspect an isolated manual installation.

## Internal contracts

- New planners return operations, missing entries and diagnostics; only the CLI prints.
- Source discovery remains kind-specific. Reuse small proven source/metadata helpers, not a
  generic plugin framework. Cache writes occur only under the existing apply/update contract.
- Aggregate all kinds before exclusion validation and one engine invocation. A malformed final
  item must prevent all earlier project writes.
- Plan desired skills before legacy-link removals. Compute those removals only after every
  desired target is known. This avoids removing a target selected by commands.
- Missing source inventory suppresses both engine pruning and legacy-link pruning, with a
  diagnostic. Exclusion calculations and summary output use the same effective prune flag.
- Reject an existing skills/commands ownership-kind transition, even with force; require a
  separate removal run. Never let generic engine replacement bypass the shared-name contract.
- Add only the previously approved tomlkit dependency; retain Python >= 3.9 support.

## Delivery and review

Use failing focused tests, minimal implementation, then refactoring with tests green.
Implementer agents build tasks; a separate reviewer checks each checkpoint. The main session
owns integration and acceptance checks. Shared files are edited sequentially.

Checkpoint A follows agents; B follows commands and pruning; C follows rules, integration and
docs. Run full tests, Ruff and strict mypy at each checkpoint and resolve reviewer findings.
No commits without owner approval; specifications remain ignored and are never staged.
Preserve the staged removal of root SPEC-core.md and unrelated TODOS.md/skills.yaml.

## Verification

```bash
uv run pytest -q
uv run ruff check .
uv run mypy agent_sync tests
git diff --exit-code -- tests/test_skills_pinned.py tests/test_skills_pinned_edges.py tests/test_harness.py tests/conftest.py
git diff --check
```

Use local sources and the existing isolated-HOME subprocess harness. For the manual smoke run,
install representative fixtures into a temporary project and verify byte contents and no-op
reapply. Check client discovery/invocation metadata where local tooling permits without a paid
model call. Record client versions and unavailable checks: serializer tests alone do not prove
runtime behavior. Do not execute imported commands or rules.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Claude agent permissions do not translate | Loss diagnostics; native TOML preference; no permission-equivalence claim |
| Command argument/invocation behavior differs | Approved prompt fallback, explicit metadata, runtime limits reported |
| Missing inventory causes deletion | Suppress all pruning while any source is missing |
| New helpers change existing skills | Narrow integration changes and byte-identical pinned tests |
| Recursive rules follow symlinks | Reject selected symlinks, including intermediate path components |

Owner directed continuation with native artifact reuse first. Normalize registered native Codex fragments; support explicit command-to-skill references rather than guessing equivalence.
