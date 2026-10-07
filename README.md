# agent-sync

A single command that installs [Agent Skills](https://code.claude.com/docs/en/skills) into a project from a YAML manifest. It fetches sources (GitHub, npx or your own folder) into a shared cache and creates symlinks in `.agents/skills/`, so Claude Code and Codex both find them.

Skills, agents, commands and rules are supported. Hooks and MCP servers remain planned; non-empty
`hooks` or `mcp` sections fail with `<kind> is not supported yet`. See [CAPABILITY-MAP.md](CAPABILITY-MAP.md).

## How it works

1. Reads the manifest, a list of the skills you need.
2. Downloads each external source into a shared cache for reuse across projects.
3. Plans every change first. If anything conflicts with your files, it reports it and writes nothing.
4. Creates a symlink per skill: `<project>/.agents/skills/<name>` points to the folder containing `SKILL.md` in the source.
5. Links `<project>/.claude/skills` to `../.agents/skills`, because Claude Code reads skills from there.
6. Records what it created in `.agents/agent-sync.state.json` and lists those paths in the repository's `.git/info/exclude`.

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

# Also remove links the manifest no longer lists
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
| `--personal-dir <path>` | Folder with your own skills (see Locations) |

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

**Personal skills.** `personal:<name>` entries are read from your personal folder, scanned like any other source: its `skills/` subfolder if it has one, otherwise its root (so a folder of `<name>/SKILL.md` works as is). The first one that is set wins; there is no default:

1. `--personal-dir <path>`
2. `$AGENT_SYNC_PERSONAL`

A `personal:` entry without either is an error that says how to set one (checked before any source is downloaded).

Point `--personal-dir` at your skills folder only. Links into it count as agent-sync's own for adoption and `--prune` (see below), so a folder that also holds other things would make unrelated links look like agent-sync's.

Nothing is written outside the project, except the source cache.

## What is written in the project

```text
.agents/skills/<name>            symlink to the folder containing SKILL.md in the source
.claude/skills                   symlink -> ../.agents/skills (Claude Code reads skills there)
.agents/agent-sync.state.json    what agent-sync created, with a fingerprint of each item
.agents/agent-sync.lock          lock file: two runs in one project cannot overlap
```

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

## Behavior on subsequent runs

- Everything already matches: nothing is changed or rewritten (the summary says `0 changes`), including the state file and the exclude block.
- A link was deleted by hand: it is created again.
- A real file, folder or foreign symlink is at a target path: error, and nothing is changed.
- A link you edited: error naming it; `--force` restores it.
- A source has not been downloaded and the run does not use `--apply`: prints `Missing source (run with --apply): <source>/<repo>`, and no link for it is planned.
- A named skill is missing from the cached copy of its source: with `--apply` the source is refreshed once per run (`git pull --ff-only`, else cloned again; `npx` is run again) and looked at again, so a skill added upstream is picked up without `--update`. If it is still missing, the error lists the available skills and says the source was just refreshed, so check the name.
- `--update` refreshes every source up front, even when all its skills are already cached.
- Skills dropped from the manifest stay installed until you run with `--prune`.

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
