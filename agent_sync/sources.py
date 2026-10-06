"""Source entries, manifest selectors, and fetching/discovering skill sources."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

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
        entries: list[Entry] = []
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
    assert entry.repo is not None
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
        pulled = subprocess.run(["git", "-C", str(cache), "pull", "--ff-only"], check=False).returncode == 0
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
