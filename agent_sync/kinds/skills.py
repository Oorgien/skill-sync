"""Skills planner: manifest entries -> `link` operations for .agents/skills and .claude/skills.

Nothing here prints or writes, except that ``resolve_source`` downloads sources with ``--apply``.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from agent_sync.context import Context
from agent_sync.engine import Op
from agent_sync.manifest import ManifestEntry
from agent_sync.sources import Entry, discover_skills, resolve_source, will_refresh

SKILLS_DIR = ".agents/skills"
CLAUDE_LINK = ".claude/skills"
CLAUDE_LINK_TEXT = "../.agents/skills"

OLD_INSTALL_HINT = (
    "If it is a link from an older skill-sync install, pass --cache-dir and/or --personal-dir "
    "pointing at the old cache or personal folder, so it can be adopted."
)
LOOP_HINT = "It is a symlink loop; remove it and run again."
CLAUDE_FOLDER_HINT = (
    "agent-sync links .claude/skills to .agents/skills: move what is inside .claude/skills "
    "into .agents/skills, delete the folder, and run again."
)


class SkillsPlan(NamedTuple):
    ops: list[Op]
    missing: list[ManifestEntry]  # sources that are not downloaded yet (dry run only)


def plan_skills(entries: Sequence[ManifestEntry], ctx: Context) -> SkillsPlan:
    """One ``link`` per skill, the ``.claude/skills`` link, and (with prune) old-install removals."""
    links, missing = _resolve(entries, ctx)
    ops = [
        Op(
            "link",
            f"{SKILLS_DIR}/{name}",
            "skills",
            None,
            selector,
            str(source),
            adopt=_may_adopt(ctx, f"{SKILLS_DIR}/{name}"),
            hint=_skill_hint(ctx, f"{SKILLS_DIR}/{name}"),
        )
        for name, (source, selector) in links.items()
    ]
    claude = _claude_link(ctx)
    if claude is not None:
        ops.append(claude)
    if ctx.prune:
        ops += _prune_old_installs(ctx, {op.target for op in ops})
    return SkillsPlan(ops, missing)


# --- sources --------------------------------------------------------------------------------


def _selector(entry: ManifestEntry, name: str) -> str:
    if entry.source == "personal":
        return f"personal:{name}"
    if entry.source == "npx":
        return f"npx/{entry.repo}:{name}"
    return f"{entry.source}:{entry.repo}:{name}"


def unknown_skill_message(
    name: str, src_root: Path, available: Sequence[str], *, refreshed: bool, personal: bool
) -> str:
    """The error for a skill that is not in its source, with a hint that is true for this run."""
    listing = ", ".join(available) or "none"
    message = f"Skill '{name}' not in {src_root}. Available skills: [{listing}]."
    if personal:
        return message
    if refreshed:  # pulled or cloned in this run: refreshing again would not help
        return message + " The source was just refreshed, so it has no skill with that name."
    return message + " Re-run with --apply --update to refresh the cached source."


def _resolve(
    entries: Sequence[ManifestEntry], ctx: Context
) -> tuple[dict[str, tuple[Path, str]], list[ManifestEntry]]:
    """Map skill name -> (source folder, selector); list the entries whose source is missing."""
    links: dict[str, tuple[Path, str]] = {}
    missing: list[ManifestEntry] = []

    # The same source (GitHub repository / npm package) may appear multiple times
    # in the manifest, once for each requested skill. Resolve it only once per run
    # to avoid a separate `git pull` on the same clone for every entry with --update.
    # For npx, the skill affects the download, so it is part of the key;
    # for GitHub, it is not.
    resolved: dict[tuple[str, str | None, str | None], Path | None] = {}
    refreshed: set[tuple[str, str | None, str | None]] = set()  # pulled or cloned in this run

    for entry in entries:
        cache_key = (entry.source, entry.repo, entry.name if entry.source == "npx" else None)
        as_entry = Entry(entry.source, entry.repo, entry.name)
        personal = entry.source == "personal"
        if cache_key not in resolved:
            if not personal and will_refresh(as_entry, ctx.cache_root, ctx.apply, ctx.update):
                refreshed.add(cache_key)
            resolved[cache_key] = resolve_source(
                as_entry, ctx.personal_dir, ctx.cache_root, ctx.apply, ctx.update
            )
        src_root = resolved[cache_key]
        if not src_root:
            missing.append(entry)
            continue

        found = discover_skills(src_root)
        absent = entry.name != "all" and entry.name not in found
        if absent and ctx.apply and not personal and cache_key not in refreshed:
            # The cached clone was reused for an earlier skill of this source and is out of
            # date: refresh it once (pull, else clone again) and look again.
            refreshed.add(cache_key)
            again = resolve_source(as_entry, ctx.personal_dir, ctx.cache_root, True, True)
            if again is not None:
                src_root = resolved[cache_key] = again
                found = discover_skills(src_root)
        if entry.name == "all":
            targets = found
        elif entry.name in found:
            targets = {entry.name: found[entry.name]}
        else:
            raise ValueError(
                unknown_skill_message(
                    entry.name,
                    src_root,
                    sorted(found),
                    refreshed=cache_key in refreshed,
                    personal=personal,
                )
            )

        for name, src_path in targets.items():
            if name in links and links[name][0] != src_path:
                raise ValueError(f"Collision for skill: {name}")
            links.setdefault(name, (src_path, _selector(entry, name)))
    return links, missing


# --- adoption and old installs --------------------------------------------------------------


def _managed_roots(ctx: Context) -> list[Path]:
    roots = [ctx.cache_root.resolve()]
    if ctx.personal_dir is not None:
        roots.append(ctx.personal_dir.resolve())
    return roots


def _resolve_path(path: Path) -> Path | None:
    """``path.resolve()``, or None for a symlink loop (which older Pythons raise on)."""
    try:
        return path.resolve()
    except (RuntimeError, OSError):
        return None


def _destination(link: Path) -> Path | None:
    """Where the symlink points (the target need not exist); None for a loop."""
    raw = Path(os.readlink(link))
    return _resolve_path(raw if raw.is_absolute() else link.parent / raw)


def _points_into(link: Path, roots: Sequence[Path]) -> bool:
    destination = _destination(link)
    if destination is None:
        return False
    return any(destination == root or destination.is_relative_to(root) for root in roots)


def _may_adopt(ctx: Context, target: str) -> bool:
    """An unrecorded symlink may be taken over only if it is dangling or points into a managed
    root: a real folder or a link anywhere else is the user's, and stays a conflict. A loop
    cannot be judged, so it is never taken over."""
    if ctx.state.find(target) is not None:
        return False
    path = ctx.project / target
    if not path.is_symlink() or _destination(path) is None:
        return False
    return not path.exists() or _points_into(path, _managed_roots(ctx))


def _skill_hint(ctx: Context, target: str) -> str:
    """Advice for the conflict an unrecorded symlink at a skill target will cause."""
    path = ctx.project / target
    if ctx.state.find(target) is not None or not path.is_symlink():
        return ""
    return LOOP_HINT if _destination(path) is None else OLD_INSTALL_HINT


def _prune_old_installs(ctx: Context, planned: set[str]) -> list[Op]:
    """``remove`` (adopt) for unrecorded links into a managed root that the plan no longer makes.

    Recorded items are pruned by the engine; real folders and links elsewhere are never touched.
    """
    skills_dir = ctx.project / SKILLS_DIR
    if not skills_dir.is_dir():
        return []
    roots = _managed_roots(ctx)
    ops: list[Op] = []
    for child in sorted(skills_dir.iterdir()):
        target = f"{SKILLS_DIR}/{child.name}"
        if "\\" in child.name or "\n" in child.name:
            continue  # cannot be an operation target; not ours to judge
        if target in planned or ctx.state.find(target) is not None or not child.is_symlink():
            continue
        if _points_into(child, roots):
            ops.append(Op("remove", target, "skills", adopt=True))
    return ops


# --- .claude/skills -------------------------------------------------------------------------


def _claude_link(ctx: Context) -> Op | None:
    """Claude Code reads <project>/.claude/skills; keep it linked to the shared pool.

    Always considered, so a recorded link is part of the plan and prune never removes it.
    """
    link = ctx.project / CLAUDE_LINK
    create = Op("link", CLAUDE_LINK, "skills", None, "", CLAUDE_LINK_TEXT)
    if not os.path.lexists(link):
        return create
    if not link.is_symlink():
        hint = CLAUDE_FOLDER_HINT if link.is_dir() else ""
        return create._replace(hint=hint)  # a real folder or file: not ours, the engine reports
    where = _resolve_path(link)
    if where is None:
        return create._replace(hint=LOOP_HINT)
    if where == _resolve_path(ctx.project / SKILLS_DIR):
        if ctx.state.find(CLAUDE_LINK) is None:
            return None  # someone else's link to the right place: leave it alone
        return create._replace(payload=os.readlink(link))  # ours: refresh the record only
    return create  # a link elsewhere is not ours: the engine reports "Target already exists"
