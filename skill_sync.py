#!/usr/bin/env python3
"""Sync skills from YAML manifest to project .agents/skills."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, NamedTuple

import yaml

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
GITHUB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?$")
NPM_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")


class Entry(NamedTuple):
    source: str
    repo: str | None
    skill: str


def parse_entry(val: Any) -> list[Entry]:
    """Expand a string or a tree (dict/list) into a list of Entry objects."""
    if isinstance(val, str):
        parts = val.split(":")
        if val.startswith("personal:") and len(parts) == 2:
            return [Entry("personal", None, parts[1])]
        if val.startswith("github.com:") and len(parts) == 3 and GITHUB_RE.fullmatch(parts[1]):
            return [Entry("github.com", parts[1], parts[2])]
        if val.startswith("npx/") and len(parts) == 2 and NPM_RE.fullmatch(val[4:].split(":")[0]):
            return [Entry("npx", val[4:].split(":")[0], parts[1])]
        raise ValueError(f"Invalid string selector: {val}")

    if isinstance(val, dict):
        entries = []
        for src, targets in val.items():
            src = "github.com" if src in ("github.com", "github") else src
            if src == "personal":
                entries.extend(Entry("personal", None, str(s)) for s in (targets if isinstance(targets, list) else [targets]))
            elif src in ("github.com", "npx"):
                for item in (targets if isinstance(targets, list) else [targets]):
                    if isinstance(item, str):
                        # Handle simple string entries like "user/repo:skill"
                        parts = item.split(":")
                        if len(parts) != 2:
                            raise ValueError(f"Invalid {src} entry: {item}")
                        repo_s, s = parts[0], parts[1]
                        if src == "github.com" and not GITHUB_RE.fullmatch(repo_s):
                            raise ValueError(f"Invalid GitHub repo: {repo_s}")
                        if src == "npx" and not NPM_RE.fullmatch(repo_s):
                            raise ValueError(f"Invalid npm package: {repo_s}")
                        entries.append(Entry(src, repo_s, s))
                    else:
                        # Handle dict entries like {"user/repo": ["skill1", "skill2"]}
                        for repo, skills in item.items():
                            repo_s = str(repo)
                            if src == "github.com" and not GITHUB_RE.fullmatch(repo_s):
                                raise ValueError(f"Invalid GitHub repo: {repo_s}")
                            if src == "npx" and not NPM_RE.fullmatch(repo_s):
                                raise ValueError(f"Invalid npm package: {repo_s}")
                            for s in (skills if isinstance(skills, list) else [skills]):
                                entries.append(Entry(src, repo_s, str(s)))
            else:
                raise ValueError(f"Unsupported source: {src}")
        return entries
    raise ValueError(f"Unsupported entry format: {val}")


def discover_skills(root: Path) -> dict[str, Path]:
    """Find folders containing SKILL.md."""
    scan = (root / "skills") if (root / "skills").is_dir() else root
    found = {}
    for skill_file in scan.rglob("SKILL.md"):
        folder = skill_file.parent
        rel_parts = folder.relative_to(scan).parts
        if any(part.startswith(".") or part == "node_modules" for part in rel_parts):
            continue
        name = folder.name
        if not NAME_RE.fullmatch(name) or name in found:
            raise ValueError(f"Invalid or duplicate skill: {name} in {folder}")
        found[name] = folder
    return found


def _download_github(entry: Entry, cache: Path) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dl-", dir=cache.parent) as tmp:
        tpath = Path(tmp)
        subprocess.run(
            ["git", "clone", "--depth", "1", f"https://github.com/{entry.repo}.git", str(tpath / "src")],
            check=True,
        )
        if cache.is_dir():
            shutil.rmtree(cache)
        (tpath / "src").rename(cache)


def _download_npx(entry: Entry, cache: Path) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dl-", dir=cache.parent) as tmp:
        tpath = Path(tmp)
        subprocess.run(
            ["npx", "--yes", entry.repo, "install", "--skills", entry.skill, "--target", "claude-project"],
            cwd=tpath,
            check=True,
        )
        skills = [p.parent for p in tpath.rglob("SKILL.md") if p.parent.name == entry.skill]
        if len(skills) != 1:
            raise ValueError(f"Expected 1 skill '{entry.skill}', found {len(skills)}")
        dest = cache / "skills" / entry.skill
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skills[0], dest, dirs_exist_ok=True)


def resolve_source(
    entry: Entry,
    hub_root: Path,
    cache_root: Path,
    apply: bool,
    update: bool = False,
) -> Path | None:
    """Return the path to the skill source, downloading or updating it if needed."""
    if entry.source == "personal":
        return hub_root / "custom-skills"
    assert entry.repo is not None

    cache = cache_root / entry.source / entry.repo.replace("/", "%2F")
    has_skill = cache.is_dir() and (entry.skill == "all" or entry.skill in discover_skills(cache))

    if has_skill and not update:
        return cache
    if not apply:
        return cache if has_skill else None
    if entry.source == "npx" and entry.skill == "all":
        raise ValueError("npx sources require explicit skill name")

    if entry.source == "github.com" and cache.is_dir():
        # Already cloned: try pulling new commits (which may include new skills)
        # instead of downloading the repository again.
        pulled = subprocess.run(["git", "-C", str(cache), "pull", "--ff-only"]).returncode == 0
        still_missing = entry.skill != "all" and entry.skill not in discover_skills(cache)
        if pulled and not still_missing:
            return cache
        # git pull failed (for example, history was rewritten by a force push),
        # or the skill is still missing (for example, files were deleted locally).
        # Download the repository again.
        _download_github(entry, cache)
        return cache

    if entry.source == "github.com":
        _download_github(entry, cache)
    else:
        _download_npx(entry, cache)
    return cache


def prune_managed_links(
    project_root: Path,
    hub_root: Path,
    cache_root: Path,
    keep: set[str],
    apply: bool,
) -> int:
    """Remove tool-managed symlinks from .agents/skills that are no longer in the manifest.

    Only links pointing into the source cache or the hub's custom-skills/ are affected;
    real directories and symlinks not managed by this tool are left untouched.
    """
    skills_dir = project_root / ".agents" / "skills"
    if not skills_dir.is_dir():
        return 0

    managed_roots = [
        cache_root.resolve(),
        (hub_root / "custom-skills").resolve(),
    ]

    pruned = 0
    for child in sorted(skills_dir.iterdir()):
        if child.name in keep or not child.is_symlink():
            continue
        raw = Path(os.readlink(child))
        target = (raw if raw.is_absolute() else child.parent / raw).resolve()
        if not any(target == root or target.is_relative_to(root) for root in managed_roots):
            continue
        print(f"{'Remove' if apply else 'Would remove'} {child} -> {target}")
        if apply:
            child.unlink()
        pruned += 1
    return pruned


def ensure_claude_skills_link(project_root: Path, apply: bool) -> None:
    """Claude Code reads skills from <project>/.claude/skills, not .agents/skills.

    Maintain a symlink to the shared pool: <project>/.claude/skills -> ../.agents/skills.
    """
    agents_skills = project_root / ".agents" / "skills"
    claude_dir = project_root / ".claude"
    target_link = claude_dir / "skills"
    rel_source = Path("..") / ".agents" / "skills"

    if target_link.is_symlink() and target_link.resolve() == agents_skills.resolve():
        return  # Already in place; nothing to print.

    print(f"{'Link' if apply else 'Would link'} {target_link} -> {rel_source}")
    if not apply:
        return
    if target_link.exists() or target_link.is_symlink():
        raise ValueError(f"Target already exists: {target_link}")
    claude_dir.mkdir(parents=True, exist_ok=True)
    target_link.symlink_to(rel_source, target_is_directory=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Skill sync tool")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to the YAML manifest. Defaults to config.yaml in --project (the current project directory).",
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=Path.cwd(),
        help="Project to install skills into. Defaults to the current directory.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help=(
            "Shared cache for downloaded sources (github/npx). Defaults to $SKILL_SYNC_CACHE, "
            "or .skill-sources/ next to this script (the hub). One cache for all projects."
        ),
    )
    parser.add_argument("--apply", action="store_true", help="Download and symlink skills")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Refresh already-downloaded sources (git pull / redownload) to pick up new upstream skills",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Remove tool-managed symlinks in .agents/skills that are no longer in the manifest",
    )
    args = parser.parse_args()

    hub_root = Path(__file__).resolve().parent
    project_root = args.project.resolve()
    if args.cache_dir is not None:
        cache_root = args.cache_dir.expanduser().resolve()
    elif os.environ.get("SKILL_SYNC_CACHE"):
        cache_root = Path(os.environ["SKILL_SYNC_CACHE"]).expanduser().resolve()
    else:
        cache_root = hub_root / ".skill-sources"
    config_path = args.config if args.config is not None else project_root / "config.yaml"
    if not config_path.is_file():
        raise ValueError(
            f"Config not found: {config_path}. Copy config.yaml from the hub to {project_root}, "
            "or specify its path with --config."
        )
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    skills_sec = data.get("skills") or {}

    links: dict[Path, Path] = {}
    missing: list[Entry] = []

    # The same source (GitHub repository / npm package) may appear multiple times
    # in the manifest, once for each requested skill. Resolve it only once per run
    # to avoid a separate `git pull` on the same clone for every entry with --update.
    # For npx, the skill affects the download, so it is part of the key;
    # for GitHub, it is not.
    resolved: dict[tuple[str, str | None, str | None], Path | None] = {}

    for agent in ("common", "codex", "claude"):
        for item in (skills_sec.get(agent) or []):
            for entry in parse_entry(item):
                cache_key = (
                    entry.source,
                    entry.repo,
                    entry.skill if entry.source == "npx" else None,
                )
                if cache_key not in resolved:
                    resolved[cache_key] = resolve_source(
                        entry, hub_root, cache_root, args.apply, args.update
                    )
                src_root = resolved[cache_key]
                if not src_root:
                    missing.append(entry)
                    continue

                disc = discover_skills(src_root)
                targets = disc if entry.skill == "all" else {entry.skill: disc[entry.skill]} if entry.skill in disc else None
                if targets is None:
                    available = ", ".join(sorted(disc.keys())) or "none"
                    hint = " Re-run with --update to refresh the cached source." if not args.update else ""
                    raise ValueError(f"Skill '{entry.skill}' not in {src_root}. Available skills: [{available}].{hint}")

                for name, src_path in targets.items():
                    target_link = project_root / ".agents" / "skills" / name
                    if target_link in links and links[target_link] != src_path:
                        raise ValueError(f"Collision for skill: {name}")
                    links[target_link] = src_path

    for entry in missing:
        print(f"Missing source (run with --apply): {entry.source}/{entry.repo}")

    pruned = 0
    if args.prune:
        pruned = prune_managed_links(
            project_root, hub_root, cache_root, {t.name for t in links}, args.apply
        )

    prefix = "Link" if args.apply else "Would link"
    managed_roots = [cache_root.resolve(), (hub_root / "custom-skills").resolve()]
    for target, source in links.items():
        print(f"{prefix} {target} -> {source}")
        if not args.apply:
            continue
        if target.is_symlink():
            if target.resolve() == source.resolve():
                continue
            raw = Path(os.readlink(target))
            cur = (raw if raw.is_absolute() else target.parent / raw).resolve()
            # Recreate a managed symlink into the source cache or custom-skills/
            # if it points to the wrong location (the cache directory changed)
            # or is broken. Leave symlinks not managed by this tool untouched.
            if not target.exists() or any(
                cur == r or cur.is_relative_to(r) for r in managed_roots
            ):
                target.unlink()
            else:
                raise ValueError(f"Target already exists: {target}")
        elif target.exists():
            raise ValueError(f"Target already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source, target_is_directory=True)

    summary = f"{'Applied' if args.apply else 'Dry-run:'} {len(links)} links, {len(missing)} missing"
    if args.prune:
        summary += f", {pruned} pruned"
    print(summary + ".")

    ensure_claude_skills_link(project_root, args.apply)


def cli() -> None:
    try:
        main()
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
