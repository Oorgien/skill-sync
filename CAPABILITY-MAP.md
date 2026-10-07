# Capability Map: agent-sync

agent-sync grows skill-sync from skills to agents, commands, rules, hooks and MCP servers,
for both Claude Code and Codex.

**Key principle: everything is installed per project.** agent-sync never writes to `~/.claude`,
`~/.codex` or any other user-level configuration. The shared source cache is the only thing it
keeps outside the project.

| Module id | Responsibility | Depends on |
|---|---|---|
| `core` | Manifest format v2 and its sections, source cache, ownership state with hashes, plan/apply engine, prune, `.git/info/exclude`, Codex trust reminder | — |
| `skills` | Today's skill sync, moved onto `core` without behavior change | core |
| `agents` | Copy Claude `.md` agents; convert `.md` to Codex `.toml`; copy native Codex `.toml` agents | core |
| `commands` | Turn commands into user-invoked skills in the shared skill pool | core, skills |
| `rules` | Copy `.md` rules to `.claude/rules/` and `.rules` files to `.codex/rules/` | core |
| `hooks` | Hooks declared in the manifest, merged into `.claude/settings.local.json` and `.codex/hooks.json` | core |
| `mcp` | Servers picked by name from a source's `.mcp.json`, or declared inline, merged into `.mcp.json` and `.codex/config.toml` | core |

Build order: `core` → `skills` → `agents`, `commands`, `rules` → `hooks`, `mcp`.

Later, after the modules above: `prefixes`, an optional per-source name prefix (from `TODOS.md`),
such as `ecc-search-first`. It touches every kind's naming, and prefixed skills must be copied and
rewritten instead of symlinked.

Module specifications and implementation plans are local working documents (`specs/`,
`tasks/`), not in Git. This map is the committed overview of the design.

## Where each kind goes

| Kind | Claude Code | Codex | How |
|---|---|---|---|
| skills | `.claude/skills` → `../.agents/skills` | `.agents/skills/<name>` | symlink per skill (unchanged) |
| agents | `.claude/agents/<name>.md` | `.codex/agents/<name>.toml` | copy; convert for Codex |
| commands | `.agents/skills/<name>/SKILL.md` | the same file | generate a user-invoked skill |
| rules | `.claude/rules/…` (`.md`) | `.codex/rules/…` (`.rules`) | copy, routed by file format |
| hooks | `.claude/settings.local.json` | `.codex/hooks.json` | merge declared entries |
| mcp | `.mcp.json` | `.codex/config.toml` | merge by server name |

## Decisions (2026-10-05)

- **Per project only.** No user-level install target for any kind or harness.
- **Commands become user-invoked skills** (`disable-model-invocation: true`). Codex has no
  project-level commands: its custom prompts live only in `~/.codex/prompts` and are deprecated in
  favour of skills. One generated skill serves both harnesses: `/name` in Claude, `$name` in Codex.
- **Only skills are symlinked.** Everything else is copied or generated. Claude treats a
  `.claude/rules/` symlink that points outside the project as an external import: it doesn't load
  until external imports are approved, and path-scoped rules never load through it.
- **Rules are routed by format, never converted.** Claude rules are Markdown instructions; Codex
  rules are a Starlark command-approval policy.
- **Hooks** (2026-10-07, supersedes the 2026-10-05 rule): declared in the manifest with a `{source}`
  placeholder, or picked one handler at a time from a source's native hook file (by `id`, or by
  event + text match); a pick must match exactly one handler. A whole hook file is never imported.
  If git tracks the target hook file (`.codex/hooks.json`, `.claude/settings.local.json`), the run
  fails: agent-sync's hook commands contain paths into the local cache.
- **MCP servers** (2026-10-07): native definitions first (`.mcp.json` for Claude,
  `.codex/config.toml` for Codex), conversion only as a warned fallback, plus servers declared
  inline. Environment values and auth headers must be variable references; a literal is an error.
- **tomlkit** edits `.codex/config.toml`, keeping comments and formatting.
- **Git:** every path agent-sync creates is listed in the repository's `info/exclude`.
- **Codex trust:** Codex reads hooks, MCP servers and rules from `.codex/` only in trusted projects.
  Trust is stored in `~/.codex/config.toml`, so agent-sync prints a reminder instead of setting it.
- **Names:** package `agent_sync`, command and distribution `agent-sync`; the `skill-sync` command
  is removed.
- **Locations:** the cache defaults to `~/.cache/agent-sync` (`$SKILL_SYNC_CACHE` is still honoured);
  `personal:` items come from `--personal-dir` or `$AGENT_SYNC_PERSONAL`.
- **`--force`** overwrites or prunes owned items you edited; it never touches anything else.
- **Delivery:** branch `feature/agent-sync`, one commit per finished module, no AI attribution lines
  in commit messages.
