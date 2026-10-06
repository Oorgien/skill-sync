"""Skills: managed links in <project>/.agents/skills and the .claude/skills link."""

from __future__ import annotations

import os
from pathlib import Path


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


def link_skills(
    links: dict[Path, Path],
    hub_root: Path,
    cache_root: Path,
    apply: bool,
) -> None:
    """Print and (with apply) create the planned skill symlinks."""
    prefix = "Link" if apply else "Would link"
    managed_roots = [cache_root.resolve(), (hub_root / "custom-skills").resolve()]
    for target, source in links.items():
        print(f"{prefix} {target} -> {source}")
        if not apply:
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
