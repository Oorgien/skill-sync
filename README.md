# skill-sync

A single command that installs [Agent Skills](https://code.claude.com/docs/en/skills) into a project from a YAML manifest: it fetches sources (GitHub / npx / local) into a shared cache and creates symlinks in `.agents/skills/`.

## How it works

1. Reads the manifest — a list of the skills you need.
2. Downloads each external source into a shared cache (`.skill-sources/`) for reuse across projects.
3. Creates a symlink for each skill: `<project>/.agents/skills/<name>` → the folder containing `SKILL.md` within the source.
4. Links `<project>/.claude/skills` to `../.agents/skills` — Claude Code reads skills from there rather than directly from `.agents/skills/`.
5. Runs in dry-run mode by default (prints the plan). Writes to disk only with `--apply`.

The script does not overwrite real directories or symlinks it does not manage in either `.agents/skills/` or `.claude/skills`.

## Requirements and installation

- Python ≥ 3.9
- The `pyyaml` package
- `git` — for `github.com:…` sources
- `node` + `npx` — only when using `npx/…` sources

The project uses `hatchling` to build (see `pyproject.toml`). Installation options:

```bash
# uv: install as the `skill-sync` command
uv tool install .
skill-sync --project /path/to/project --apply

# Or set up a development environment
uv sync            # Includes the dev group: pytest, ruff, mypy
uv run skill-sync --project /path/to/project --apply

# Or use pip
pip install -e .
skill-sync --project /path/to/project --apply
```

You can also run it without installing the project — it is a single file that depends only on `pyyaml`:

```bash
pip install pyyaml
python3 skill_sync.py --project /path/to/project --apply
```

The `skill-sync` entry point is equivalent to `python3 skill_sync.py`.

## Manifest

A YAML file. By default, the script uses `config.yaml` from the `--project` directory; specify a different path with `--config`.

```yaml
skills:
  common:
    - github.com:addyosmani/agent-skills:all      # All skills from the repository
    - github.com:owner/repo:some-skill            # One specific skill
    - personal:my-skill                           # From custom-skills/my-skill/ next to the script
    - npx/ecc-universal:ecc-guide                 # Selective installation via npx
  codex: []
  claude: []
```

The `common`, `codex`, and `claude` sections are merged into one shared pool (without filtering by agent).

Source formats:

| Format | Meaning |
| --- | --- |
| `github.com:<owner/repo>:<skill>` | One skill from the repository |
| `github.com:<owner/repo>:all` | All skills found in the repository |
| `personal:<name>` | A local skill from `custom-skills/<name>/` next to the script |
| `npx/<package>:<skill>` | Runs `npx <package> install --skills <skill>`; copies only that skill |

An expanded form is also supported:

```yaml
skills:
  common:
    github.com:
      - owner/repo:
          - skill-a
          - skill-b
    personal:
      - my-skill
```

`github` is an alias for `github.com` (only as a key in the expanded form; string
selectors require the full `github.com:` prefix). `all` is not supported with `npx`.

The skill name comes from the name of the folder containing `SKILL.md`. The script scans the source's `skills/` subdirectory if it exists, otherwise it scans from the root. Invalid folder names and duplicate names cause an error.

## Usage

```bash
# Print the plan: no downloads or writes
python3 skill_sync.py --project /path/to/project

# Download sources and create symlinks
python3 skill_sync.py --project /path/to/project --apply

# Update previously downloaded sources (git pull --ff-only / reinstall)
python3 skill_sync.py --project /path/to/project --apply --update

# Remove symlinks no longer listed in the manifest (only those created by this tool)
python3 skill_sync.py --project /path/to/project --apply --prune
```

| Flag | Purpose |
| --- | --- |
| `--project <path>` | Target project; defaults to the current directory |
| `--config <path>` | Path to the manifest; defaults to `<project>/config.yaml` |
| `--apply` | The only flag that enables downloads and disk writes |
| `--update` | Refresh the cache and pick up skills added to the source since the last run |
| `--prune` | Remove managed symlinks from `.agents/skills/` that are no longer in the manifest |
| `--cache-dir <path>` | Location of the source cache (see below) |

## Shared source cache

Downloaded GitHub/npx sources are stored in a single cache and reused across projects.
The location is determined in this order of priority:

1. The `--cache-dir <path>` flag
2. The `SKILL_SYNC_CACHE` environment variable
3. By default, `.skill-sources/` next to `skill_sync.py`

```bash
export SKILL_SYNC_CACHE="$HOME/.cache/agent-skills"
```

## Files created in the project

```text
.agents/skills/<name>       # Symlink to the folder containing SKILL.md within the source
.claude/skills               # Symlink -> ../.agents/skills so Claude Code can find the skills
```

The cache (`.skill-sources/<source>/<owner%2Frepo>/`) lives in a shared location rather than in the project.

Add the following to the target project's `.gitignore`:

```gitignore
.agents/skills/
.claude/skills/
```

## Behavior on subsequent runs

- The required symlink is already in place → skipped (idempotent).
- A real file or a symlink not managed by the tool exists at the target path → error; nothing is changed.
- The source has not been downloaded and the run does not use `--apply` → prints `Missing source (run with --apply)`.
- For `github.com` with `--update`: tries `git pull --ff-only` first; if it fails, clones the repository again.
