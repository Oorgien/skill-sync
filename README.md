# agent-sync

A single command that installs [Agent Skills](https://code.claude.com/docs/en/skills) into a project from a YAML manifest. It fetches sources (GitHub, npx or your own folder) into a shared cache and creates symlinks in `.agents/skills/`, so Claude Code and Codex both find them.

All six kinds are supported: skills, agents, commands, rules, hooks and MCP servers. Skills, agents,
commands and rules are symlinked or copied into the project; hooks and MCP servers are merged into
the harnesses' own config files. See [CAPABILITY-MAP.md](CAPABILITY-MAP.md).

## How it works

1. Reads the manifest, a list of the skills, agents, commands, rules, hooks and MCP servers you need.
2. Downloads each external source into a shared cache for reuse across projects.
3. Plans every change first. If anything conflicts with your files, it reports it and writes nothing.
4. Creates a symlink per skill: `<project>/.agents/skills/<name>` points to the folder containing `SKILL.md` in the source.
5. Links `<project>/.claude/skills` to `../.agents/skills`, because Claude Code reads skills from there.
6. Records what it created in `.agents/agent-sync.state.json` and lists those paths in the repository's `.git/info/exclude`.

Hooks and MCP servers work differently from the steps above: they are entries merged into files
such as `.claude/settings.local.json` or `.mcp.json`, not links (see Hooks and MCP servers below).

It is a dry run by default (prints the plan). Nothing is downloaded or written without `--apply`.

## Requirements and installation

- Python 3.9 or newer
- POSIX only (Linux, macOS): the project lock uses `flock`
- `git`, for `github.com:...` sources and for the exclude step
- `node` and `npx`, only for `npx/...` sources

```bash
# uv: install the `agent-sync` command
uv tool install .
agent-sync --project /path/to/project --apply

# Or a development environment (pytest, ruff, mypy)
uv sync
uv run agent-sync --project /path/to/project --apply

# Or pip
pip install -e .
agent-sync --project /path/to/project --apply

# Or without installing the project
pip install pyyaml tomlkit
python -m agent_sync --project /path/to/project --apply
```

`agent-sync` and `python -m agent_sync` are the same program.

## Manifest

A YAML file. By default `config.yaml` in the `--project` directory is used; pass another path with `--config`. A relative `--config` is resolved against the current directory, not against `--project`. [config.yaml](config.yaml) in this repository is an annotated sample.

```yaml
skills:
  common:
    - github.com:addyosmani/agent-skills:all      # All skills from the repository
    - github.com:owner/repo:some-skill            # One specific skill
    - personal:my-skill                           # From your personal folder (see below)
    - npx/ecc-universal:ecc-guide                 # Selective installation via npx
  codex: []
  claude: []
```

The `common`, `codex` and `claude` sections are merged into one pool: every skill is installed once, for both harnesses. An unknown top-level key or section is an error, so a typo cannot silently do nothing.

| Format | Meaning |
| --- | --- |
| `github.com:<owner/repo>:<skill>` | One skill from the repository |
| `github.com:<owner/repo>:all` | All skills found in the repository |
| `personal:<name>` | A skill from your personal folder |
| `npx/<package>:<skill>` | Runs `npx <package> install --skills <skill>` and keeps only that skill |

An expanded form is also supported:

```yaml
skills:
  common:
    - github.com:
        - owner/repo:
            - skill-a
            - skill-b
    - personal:
        - my-skill
```

`github` is an alias for `github.com` (only as a key in the expanded form; string selectors need the full `github.com:` prefix). `all` is not supported with `npx`.

The skill name is the name of the folder that contains `SKILL.md`. A source's `skills/` subfolder is scanned if it exists, otherwise the whole source. Invalid folder names, duplicate names and two sources providing the same name are errors.

## Usage

```bash
# Print the plan: no downloads, no writes
agent-sync --project /path/to/project

# Download sources and create the links
agent-sync --project /path/to/project --apply

# Refresh downloaded sources first (git pull --ff-only, else clone again; npx is reinstalled)
agent-sync --project /path/to/project --apply --update

# Also remove what the manifest no longer lists (links, copies, hook groups, MCP entries)
agent-sync --project /path/to/project --apply --prune

# Restore or remove items you edited by hand
agent-sync --project /path/to/project --apply --force
```

| Flag | Purpose |
| --- | --- |
| `--project <path>` | Target project; defaults to the current directory |
| `--config <path>` | Manifest; defaults to `<project>/config.yaml`. A relative path is resolved against the current directory |
| `--apply` | Enables downloads and disk writes. Without it, the run only prints the plan |
| `--update` | Refresh downloaded sources and pick up skills added upstream. Needs `--apply` to take effect |
| `--prune` | Remove what agent-sync created and the manifest no longer lists (see Prune) |
| `--force` | Overwrite or prune items agent-sync created that you edited. Never touches anything agent-sync did not create |
| `--cache-dir <path>` | Shared source cache (see Locations) |
| `--personal-dir <path>` | Folder with your own sources, for `personal:` entries of every kind (see Locations) |

Exit code: 0 on success, 1 on any error or conflict, with a message saying what to do. Wrong command-line usage exits with 2 (argparse's own code).

Sample output:

```text
Link /path/to/project/.agents/skills/alpha -> /home/me/.cache/agent-sync/github.com/owner%2Frepo/skills/alpha
Link /path/to/project/.claude/skills -> ../.agents/skills
Applied: 2 changes, 0 missing.
Update /path/to/project/.git/info/exclude
```

## Locations

**Source cache.** Downloaded GitHub and npx sources live in one cache, shared by all projects. The first one that is set wins; an empty value counts as unset:

1. `--cache-dir <path>`
2. `$AGENT_SYNC_CACHE`
3. `$SKILL_SYNC_CACHE` (kept so existing setups keep working)
4. `$XDG_CACHE_HOME/agent-sync`
5. `~/.cache/agent-sync`

**Personal sources.** `personal:<name>` entries (of any kind, including `source: personal` for hooks) are read from your personal folder, scanned like any other source: its `skills/` subfolder if it has one, otherwise its root (so a folder of `<name>/SKILL.md` works as is). The first one that is set wins; there is no default:

1. `--personal-dir <path>`
2. `$AGENT_SYNC_PERSONAL`

A `personal:` entry without either is an error that says how to set one (checked before any source is downloaded).

Point `--personal-dir` at your own sources folder only. Links into it count as agent-sync's own for adoption and `--prune` (see below), so a folder that also holds other things would make unrelated links look like agent-sync's.

Nothing is written outside the project, except the source cache.

## What is written in the project

```text
.agents/skills/<name>            symlink to the folder containing SKILL.md in the source
.claude/skills                   symlink -> ../.agents/skills (Claude Code reads skills there)
.agents/agent-sync.state.json    what agent-sync created, with a fingerprint of each item
.agents/agent-sync.lock          lock file: two runs in one project cannot overlap
.claude/settings.local.json      hooks for Claude Code (only agent-sync's groups are added)
.codex/hooks.json                hooks for Codex
.mcp.json                        MCP servers for Claude Code (mcpServers.<name>)
.codex/config.toml               MCP servers for Codex ([mcp_servers.<name>])
```

The last four are shared files: agent-sync adds and removes only its own entries and leaves
everything else (your hooks, servers, other keys, TOML comments) as it is. Your comments after,
between or at the end of agent-sync's TOML tables are kept when it replaces or prunes them; an edit
that would drop a comment is refused before anything is written. A shared file that agent-sync
created is listed in `.git/info/exclude` for as long as it exists (also after its last agent-sync
entry was pruned while your own content remains), and is deleted once it is empty. A file you
already had is never listed or deleted.

A manifest with only hooks or MCP servers still creates the `.claude/skills -> ../.agents/skills`
link, as always; with no skills installed it points at a folder that may not exist.

Folders agent-sync has to create (such as `.agents/skills`) are recorded and removed again once they are empty. Folders it did not create are never removed.

**Git exclude.** Inside a git repository, agent-sync keeps one marked block in the repository's `.git/info/exclude` (found with `git rev-parse --git-path info/exclude`, so worktrees work). The block belongs to this checkout, named by the project's absolute path; other checkouts and projects keep their own blocks, and every other line is left as it is. It lists exactly the paths agent-sync currently owns: every link, the state and lock files, and `.claude/skills` if agent-sync created it. It never lists a folder, so your own files in `.agents/skills` stay visible to git. Blocks of checkouts that no longer exist are deleted. Outside git the step is skipped with a one-line note. You no longer need to edit `.gitignore`.

## Safety: all-or-nothing runs

- The whole plan is checked before anything is written. A conflict makes the run fail with exit code 1 and write nothing in the project: not even `.agents/` or the exclude block.
- agent-sync never changes or removes something it did not create. A real folder, a file or a symlink to somewhere else at a target path is reported as `Target already exists`, and left alone.
- A real `.claude/skills/` **folder** blocks every run with `Target already exists`, because agent-sync links `.claude/skills` to `.agents/skills`. Move what is inside it into `.agents/skills`, delete the folder, and run again. A `.claude/skills` symlink that points anywhere else is refused the same way, and so is a symlink loop (remove it).
- A link it created but you changed is reported as changed since agent-sync wrote it. Delete it, or run with `--force` to restore it (or, with `--prune`, to remove it).
- `--force` only applies to items recorded in the state file. It never takes over a folder or link that agent-sync did not create, and never removes a folder it created, which may hold your files.
- The state file is saved after each applied change, so a crash leaves it true to disk; run again to finish. If `.agents` is a symlink, agent-sync refuses to run rather than write through it.

## Old installs

Installs made by the earlier skill-sync have symlinks but no state file. On the next `--apply`:

- a skill symlink that points into the cache or your personal folder (or is dangling) is adopted: it is recorded, and relinked only if it points somewhere other than the manifest says. Nothing is rewritten when it already matches;
- a `.claude/skills` symlink that already reaches `.agents/skills` is left alone and not recorded. One that points anywhere else is an error;
- real folders and symlinks to other places in `.agents/skills` are never adopted: they stay an error if they collide with a manifest entry, and are otherwise ignored. Adoption only ever applies to symlinks, never to a real file or folder.

**Migrating from the old default locations.** Skill-sync kept its cache in `.skill-sources/` and `personal:` skills in `custom-skills/`, both next to its own checkout. Links into those folders are not under the new cache or personal folder, so every skill would be refused with `Target already exists`. On the first run, either point agent-sync at the old folders:

```bash
agent-sync --project /path/to/project --apply \
    --cache-dir /path/to/old-checkout/.skill-sources \
    --personal-dir /path/to/old-checkout/custom-skills
```

(or set `SKILL_SYNC_CACHE` as before), or delete the old links in `.agents/skills` and let agent-sync create them again in the new cache. Once the links are adopted, they are recorded and later runs may use any cache location: a recorded link is simply relinked.

## Prune

`--prune` removes links that agent-sync recorded and the manifest no longer produces, if you did not edit them (edited ones need `--force`). For old installs without a record it also removes symlinks in `.agents/skills` that point into the cache or the personal folder and are no longer in the manifest (the previous `--prune` rule). Real folders and other symlinks are kept. `.claude/skills` is never pruned.

For hooks and MCP servers, `--prune` removes only the groups and entries agent-sync recorded (and
that you did not edit). Of the containers around them (an event list, `mcpServers`, `[mcp_servers]`),
it removes only those agent-sync created and that are now empty; containers you created, and any
comments, are kept. Files agent-sync created are deleted once empty.

If you edited an MCP entry, `--prune` fails with a conflict until you pass `--force`. If you edited
a hook group, agent-sync no longer recognises it: it forgets the record, your copy keeps running, and
`--force` cannot remove it, so delete it by hand. A hook moved to another event is not counted as
pruned.

## Behavior on subsequent runs

- Everything already matches: nothing is changed or rewritten (the summary says `0 changes`), including the state file and the exclude block.
- A link was deleted by hand: it is created again.
- A real file, folder or foreign symlink is at a target path: error, and nothing is changed.
- A link you edited: error naming it; `--force` restores it.
- A source has not been downloaded and the run does not use `--apply`: prints `Missing source (run with --apply): <source>/<repo>`, and no link for it is planned.
- A named skill is missing from the cached copy of its source: with `--apply` the source is refreshed once per run (`git pull --ff-only`, else cloned again; `npx` is run again) and looked at again, so a skill added upstream is picked up without `--update`. If it is still missing, the error lists the available skills and says the source was just refreshed, so check the name.
- `--update` refreshes every source up front, even when all its skills are already cached.
- Skills dropped from the manifest stay installed until you run with `--prune`.

## Hooks

Hooks run code on your machine, so the dry run prints every hook to add or change with its **full
command**, exactly as it will be written. Nothing is written without `--apply`.

```yaml
hooks:
  common:                                   # installed for both Claude and Codex
    - name: audit-log                       # unique per harness; agent-sync's identity for the hook
      event: Stop                           # must be an event of both harnesses
      command: "echo stopped >> .agent-audit.log"
  claude:
    - name: edit-guard                      # a declared hook
      source: github.com:acme/tools         # optional: needed for {source}
      event: PreToolUse
      matcher: "Edit|Write"                 # optional
      command: "node {source}/scripts/guard.js"   # {source} becomes the shell-quoted cache path
      timeout: 30                           # optional, seconds
    - name: desktop-notify                  # a native pick, by text
      source: github.com:acme/tools
      pick: { event: Stop, contains: "stop:desktop-notify" }
  codex:
    - name: session-start                   # a native pick, by group id
      source: github.com:acme/tools
      pick: { id: "session:start" }
```

**Declared hooks.** `name`, `event` and `command` are required; `matcher`, `timeout` (a positive
integer) and `source` are optional. Only `type: command` hooks exist; unknown keys are errors.
`source` is `github.com:<owner/repo>` or `personal`, and is required when the command uses
`{source}`. agent-sync replaces `{source}` with the shell-quoted absolute path of the source, so do
not wrap it in quotes yourself. Events are checked per harness against the events that harness
documents; in `common` the event must be valid for both.

**Native picks.** A pick copies one handler out of the source's own hook file into a new group
(with the source group's `matcher`); `id`, `description` and the other groups' content are not
copied, and the handler is kept as it is (`type`, `command`, `timeout`, `statusMessage`, ...).
The file read is, for Claude, the `hooks` file named in `.claude-plugin/plugin.json`, else
`hooks/hooks.json`; for Codex, the one named in `.codex-plugin/plugin.json`, else
`.codex/hooks.json`. A Claude file serves Claude only and a Codex file Codex only; nothing is
converted. Pick either `id` (a group's `id` field) or `event` + `contains` (a substring of the
handler's command), optionally narrowed with `matcher`. A pick must match **exactly one** handler;
with none or several, the run fails and lists the candidates (event, id, matcher and the end of each
command). A pick by `id` that matches nothing lists only the group ids found in the file. Picks are harness-specific, so a pick in `common` is an error. A picked handler that uses
`args` (exec form) or a `shell` other than `bash`, `sh` or `zsh` is refused, because the prefix below
needs a POSIX shell; declare the hook in the manifest instead.

**Plugin root prefix.** Native commands expect their plugin folder in an environment variable, so a
picked command is written with a prefix: `export CLAUDE_PLUGIN_ROOT=<quoted source path>; ` for Claude
and `export PLUGIN_ROOT=<quoted source path>; ` for Codex. `--update` brings in upstream changes.

**`name` is the identity.** A hook is found again by its `name`, so changing its `event` moves it
in one run (the group under the old event is removed, a new one is added). Names must be unique per
harness.

**Where hooks are written.** Claude: `.claude/settings.local.json`; Codex: `.codex/hooks.json`.
Each hook is one group appended to `hooks.<Event>`; your groups, their order and every other key
stay as they are (JSON is rewritten with 2-space indentation). Invalid JSON is an error and the file
is not rewritten. If git **tracks** either file, the run fails with a conflict before writing
anything: hook commands contain paths into your local cache, so agent-sync will not put them into a
file you commit. Untrack the file (or add it to `.gitignore` and `git rm --cached` it) and run again.

Sample dry-run lines:

```text
Would add hook claude PreToolUse [matcher: Edit|Write] edit-guard: node /home/me/.cache/agent-sync/github.com/acme%2Ftools/scripts/guard.js
Would add hook codex SessionStart [matcher: startup] session-start: export PLUGIN_ROOT=/home/me/.cache/agent-sync/github.com/acme%2Ftools; node ${PLUGIN_ROOT}/s.js
```

**Codex trust.** Codex project configuration may need project trust and a restart, and Codex
records trust **per hook**: a new or changed hook (for example after `--update` changes a picked
command, or the cache moves) is skipped until you review and trust it with `/hooks` in Codex.
agent-sync prints a reminder and never changes trust settings.

**Editing a group.** agent-sync finds its group by a fingerprint of what it wrote. If you edit one of
its groups, it no longer recognises it and adds its own again. To customise a hook, copy it into your
own entry and drop it from the manifest.

## MCP servers

```yaml
mcp:
  common:
    - github.com:acme/tools:docs            # a server by name, from the source's native files
    - name: context7                        # an inline server (command form)
      command: npx
      args: ["-y", "@upstash/context7-mcp"]
      env: [CONTEXT7_API_KEY]               # variable names only
  claude:
    - name: figma                           # an inline server (url form)
      url: https://mcp.figma.com/mcp
      bearer_token_env: FIGMA_OAUTH_TOKEN
      headers: { X-Team: platform }         # non-secret headers only
```

```yaml
mcp:
  claude:
    - github.com:acme/tools:all             # every server natively defined for Claude
```

Sample dry-run lines:

```text
Would add MCP server context7 (claude) -> .mcp.json: npx -y @upstash/context7-mcp
Would add MCP server figma (claude) -> .mcp.json: https://mcp.figma.com/mcp
```

**Native first.** A named server is installed for each harness of its section from that harness's
native definition in the source, copied as it is: Claude from `.mcp.json` (`mcpServers.<name>`),
Codex from `.codex/config.toml` (active `[mcp_servers.<name>]` tables; commented ones do not
exist). If the server exists on one side only, it is **converted** from the other side, with a
printed warning that lists every field that could not be carried over (for example Codex
`startup_timeout_sec` or `enabled`, which Claude has no equivalent for). Converted definitions
can behave differently, which is why native ones win. `all` installs every natively defined server
of the section's harness and **never converts**.

**Inline servers.** A mapping with a `name` declares a server in the manifest, installed for the
harnesses of its section (converted for Codex as needed). Use either:

- command form: `command`, with optional `args` (list of strings), `cwd` and `env` (a list of variable names);
- url form: `url`, with optional `bearer_token_env` (a variable name) and `headers`.

**References only: secrets never land in a config file.** Every case below fails before anything is
written, naming the server and the key:

- Claude `env` values must be exactly `${VAR}`. Inline `env: [NAME]` writes `NAME: ${NAME}`.
- Claude headers with a secret-like name must be `${VAR}` or `Bearer ${VAR}`. Names are split into
  whole words on `-`, `_`, `.` and camelCase (any case); the secret words are `auth`,
  `authorization`, `token`, `key`, `apikey`, `secret`, `cookie`, `session`, `password`, `passwd`,
  `credential`, `credentials`, `signature`, `sig`, `csrf`, `xsrf` and `bearer`. It fails closed:
  `X-Session-Region` and `Cache-Key` are rejected, while `X-Keyboard-Layout` and `X-Author` pass.
- Codex has no literal `env`; use `env_vars = ["NAME"]`. Authentication-like `http_headers` are
  refused; use `bearer_token_env_var = "NAME"` or `env_http_headers = { Header = "NAME" }`.
- A `url` with credentials (`https://user:pass@host`) is rejected; so is a url query or fragment
  parameter with a secret-like name (same words) whose value is not a `${VAR}` reference.
- A Codex `url` containing `${` is an error, because Codex is not documented to expand it; use
  `bearer_token_env_var` or `env_http_headers`. Claude urls may contain `${VAR}`.
- A reference that renames a variable (`X: ${Y}`) cannot be expressed as Codex `env_vars`, and is an
  error when converting. `args` are not checked.

Native Codex form:

```toml
[mcp_servers.docs]
url = "https://example.com/mcp"
bearer_token_env_var = "DOCS_TOKEN"
env_http_headers = { X-Org = "ORG_HEADER" }
```

Native Claude form:

```json
{
  "mcpServers": {
    "docs": {
      "type": "http",
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer ${DOCS_TOKEN}" }
    }
  }
}
```

**`${CLAUDE_PLUGIN_ROOT}`.** A native definition that uses it is written with the cache path
substituted. That path is specific to your machine, so such a server is allowed only if its target
file is untracked; otherwise the run fails (keep the file untracked or define the server inline).
Other servers may live in tracked files, since they hold only references.

**Where servers are written.** Claude: `.mcp.json`, key `mcpServers.<name>`; Codex:
`.codex/config.toml`, table `[mcp_servers.<name>]` (edited with a TOML library that keeps your
comments and other tables). A server you already defined under the same name is a **conflict** and is
never overwritten. If your `mcp_servers` is written as dotted keys (`mcp_servers.x.command = ...`) or an
inline table, agent-sync cannot add a table next to it safely; that is a conflict too, and the advice
is to write it with a `[mcp_servers]` table header. (That advice appears only for layout problems; a
payload that cannot be written, such as NaN/Infinity or `null` in TOML, is reported as a different conflict.)

**Approval and trust.** Claude asks you to approve project servers from `.mcp.json` on first use;
that stays Claude's decision. Writing `.codex/config.toml` prints the Codex trust reminder; Codex
project configuration may need project trust and a restart, and agent-sync never changes trust.

## Limitations

- agent-sync never writes `~/.claude`, `~/.claude.json`, `~/.codex` or any Codex trust setting, and
  never imports a whole hook file (only individual handlers).
- Event names are validated per harness, but matchers are not translated (Codex matchers are regular expressions).
- Hooks and MCP definitions are not converted between Claude and Codex, except MCP servers when
  one side is missing (with a warning). Converting a Codex server with `enabled = false` installs it
  enabled for Claude (only warned).
- A Codex `url` with a `${...}` reference is refused, since Codex is not documented to expand it.
- Not verified in live Claude Code or Codex apps. What was checked: an end-to-end run in a scratch
  project, and installed ECC hook commands run with `sh -c` and a sample payload. Per Claude's docs,
  Claude runs hook commands with `sh -c` on macOS/Linux when a handler has no `args`, so the export
  prefix works there; Codex's docs do not say how it runs them. That Claude expands `${VAR}` in every
  field agent-sync writes, and that Codex passes `env_vars` as documented, is not verified either.
- TOML whitespace around inserted tables, and the line endings of CRLF files, may change; the
  content outside agent-sync's tables is kept, not necessarily the exact whitespace. A file agent-sync
  created stays git-excluded while it exists, even if only your content remains after a prune.

## Development

```bash
uv run pytest -q
uv run ruff check .
uv run mypy agent_sync tests
```

Roadmap: [CAPABILITY-MAP.md](CAPABILITY-MAP.md). Working specifications are kept locally in
`specs/` and are not included in the repository.

## Agents, commands and rules

```yaml
agents:
  common:
    - github.com:affaan-m/ECC:planner
  codex:
    - github.com:affaan-m/ECC:docs-researcher
commands:
  common:
    - github.com:affaan-m/ECC:plan
rules:
  claude:
    - github.com:affaan-m/ECC:common
    - github.com:affaan-m/ECC:python
```

All kinds support GitHub and personal sources; npx remains skills-only. `all` selects all
compatible items. Agents/rules sections target the named harness; commands and skills share
one pool regardless of section. Missing inventories suppress pruning until sources are available.

**Agents:** discover `agents/*.md`, `.claude/agents/*.md`, `agents/*.toml` and
`.codex/agents/*.toml`. Claude receives native Markdown. Codex prefers native TOML and falls
back to converting Markdown instructions. Legacy native fragments registered through the source's
`.codex/config.toml` receive their role name and description as standalone metadata. Other native
settings are retained. Conversion does not translate Claude model names or enforce Claude tool
restrictions: diagnostics list omitted keys, and Codex inherits its own settings. Supply native
TOML when exact Codex controls matter. Referenced resources that cannot be safely relocated are
reported rather than silently producing a broken installation.

**Commands:** discover immediate Markdown files in `commands/` or `.claude/commands/` and
generate explicit-invocation skills for both harnesses. A command can explicitly declare
`skill: existing-skill-name` in frontmatter to reuse a skill from the same source, including its
resources. Matching names alone do not establish equivalence. Generated skills preserve command
argument placeholders and include prompt-level guidance where substitution is unavailable;
identical cross-client argument processing is not guaranteed. Claude-specific tools, models and
dynamic context are not translated for Codex. A skill and command cannot share a target name;
changing the kind requires a separate prune run removing the previous item.

**Rules:** discover `rules/`, `.claude/rules/` and `.codex/rules/`. Route Markdown to Claude
and `.rules` policies to Codex; never convert between them. Select a top-level file by stem or
a group directory by name. Groups retain their relative paths and contents. Native project
configuration may require Codex trust and a restart; agent-sync never changes user trust.

New copied artifacts reject selected source symlinks and unsafe paths. Synchronization does
not execute imported instructions, scripts or rules. Generated command directories are owned
as whole trees: extra files count as edits and require `--force` before replacement or pruning.
Ordinary skills remain linked to their complete source directories, retaining all resources.
