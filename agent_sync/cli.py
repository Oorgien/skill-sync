"""Command line interface: argument parsing, orchestration, and error handling."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from agent_sync import engine
from agent_sync.context import Context
from agent_sync.engine import Change, Op
from agent_sync.gitexclude import sync_exclude
from agent_sync.kinds.skills import plan_skills
from agent_sync.manifest import load_manifest
from agent_sync.sources import check_personal_dir
from agent_sync.state import LOCK_FILE, STATE_FILE, State, load_state


def _env(name: str) -> str | None:
    """An environment variable, where an empty value counts as unset."""
    return os.environ.get(name) or None


def _cache_root(flag: Path | None) -> Path:
    """--cache-dir, $AGENT_SYNC_CACHE, $SKILL_SYNC_CACHE, $XDG_CACHE_HOME/agent-sync, then
    ~/.cache/agent-sync."""
    if flag is not None:
        chosen = flag
    elif _env("AGENT_SYNC_CACHE"):
        chosen = Path(os.environ["AGENT_SYNC_CACHE"])
    elif _env("SKILL_SYNC_CACHE"):
        chosen = Path(os.environ["SKILL_SYNC_CACHE"])
    elif _env("XDG_CACHE_HOME"):
        chosen = Path(os.environ["XDG_CACHE_HOME"]) / "agent-sync"
    else:
        chosen = Path("~/.cache/agent-sync")
    return chosen.expanduser().resolve()


def _personal_dir(flag: Path | None) -> Path | None:
    """--personal-dir, then $AGENT_SYNC_PERSONAL. There is no default."""
    if flag is not None:
        return flag.expanduser().resolve()
    value = _env("AGENT_SYNC_PERSONAL")
    return Path(value).expanduser().resolve() if value else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-sync",
        description=(
            "Install skills from a YAML manifest into a project: sources are fetched into a "
            "shared cache and linked into .agents/skills. A dry run is the default; nothing is "
            "written without --apply."
        ),
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=Path.cwd(),
        help="Project to install skills into. Defaults to the current directory.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to the YAML manifest. A relative path is resolved against the current "
            "directory (not --project). Defaults to config.yaml in --project."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help=(
            "Shared cache for downloaded sources (github/npx), one for all projects. Order: "
            "--cache-dir, $AGENT_SYNC_CACHE, $SKILL_SYNC_CACHE, $XDG_CACHE_HOME/agent-sync, "
            "~/.cache/agent-sync."
        ),
    )
    parser.add_argument(
        "--personal-dir",
        type=Path,
        default=None,
        help=(
            "Folder with your own skills, used by 'personal:<name>' entries (its skills/ "
            "subfolder if it has one, else its root). Order: --personal-dir, "
            "$AGENT_SYNC_PERSONAL. There is no default."
        ),
    )
    parser.add_argument("--apply", action="store_true", help="Download sources and link skills")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Refresh already-downloaded sources (git pull / redownload) to pick up new upstream skills",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Remove links agent-sync created that the manifest no longer lists",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite or prune items agent-sync created that you edited. "
            "Never touches anything agent-sync did not create."
        ),
    )
    return parser


def main() -> None:
    args = _parser().parse_args()

    project_root = args.project.resolve()
    cache_root = _cache_root(args.cache_dir)
    personal_dir = _personal_dir(args.personal_dir)
    config_path = args.config if args.config is not None else project_root / "config.yaml"
    if not config_path.is_file():
        raise ValueError(
            f"Config not found: {config_path}. "
            f"Create {project_root / 'config.yaml'} or pass --config <path>."
        )

    entries = load_manifest(config_path)
    for entry in entries:
        if entry.kind != "skills":
            raise ValueError(f"{entry.kind} is not supported yet")
    if any(entry.source == "personal" for entry in entries):
        check_personal_dir(personal_dir)  # before any source is downloaded
    state = load_state(project_root)
    ctx = Context(
        project=project_root,
        personal_dir=personal_dir,
        cache_root=cache_root,
        apply=args.apply,
        update=args.update,
        prune=args.prune,
        state=state,
    )
    plan = plan_skills(entries, ctx)

    for entry in plan.missing:
        print(f"Missing source (run with --apply): {entry.source}/{entry.repo}")

    # Check the exclude block before the engine writes anything: a broken block or a bad path
    # then fails the whole run while the project is still untouched.
    would_be = _exclude_paths(
        project_root, state, plan.ops, prune=args.prune, create_bookkeeping=args.apply
    )
    checked = sync_exclude(project_root, would_be, apply=False)

    result = engine.run(project_root, plan.ops, apply=args.apply, prune=args.prune, force=args.force)
    for change in result.changes:
        print(_describe(change, project_root, args.apply))
    print(_summary(result.changes, len(plan.missing), args.apply, args.prune))

    if args.apply:
        # List the state and lock files only if the run really left them behind.
        paths = _exclude_paths(
            project_root, state, plan.ops, prune=args.prune, create_bookkeeping=False
        )
        final = sync_exclude(project_root, paths, apply=True)
    else:
        final = checked
    if final.note:
        print(final.note)
    elif final.changed:
        print(f"{'Update' if args.apply else 'Would update'} {final.path}")


def _exclude_paths(
    project: Path, state: State, ops: Sequence[Op], *, prune: bool, create_bookkeeping: bool
) -> list[str]:
    """Every whole path agent-sync will own after this run, except folders it merely created."""
    paths = {op.target for op in ops if op.action != "remove" and op.locator is None}
    if not prune:  # without prune every recorded item survives
        removed = {op.target for op in ops if op.action == "remove"}
        paths |= {
            item.target
            for item in state.items
            if item.kind != "dir" and item.locator is None and item.target not in removed
        }
    for bookkeeping in (STATE_FILE, LOCK_FILE):
        if create_bookkeeping or os.path.lexists(project / bookkeeping):
            paths.add(bookkeeping.as_posix())
    return sorted(paths)


_VERBS = {
    "create": ("Link", "Would link"),
    "replace": ("Link", "Would link"),
    "adopt": ("Adopt", "Would adopt"),
    "remove": ("Remove", "Would remove"),
    "forget": ("Forget", "Would forget"),
}


def _describe(change: Change, project: Path, apply: bool) -> str:
    op, effect = change
    verb, would = _VERBS[effect]
    if effect in ("create", "replace") and op.action == "write":
        verb, would = "Write", "Would write"
    line = f"{verb if apply else would} {project / op.target}"
    if effect in ("create", "replace") and op.action == "link":
        line += f" -> {op.payload}"
    return line


def _summary(changes: Sequence[Change], missing: int, apply: bool, prune: bool) -> str:
    noun = "change" if len(changes) == 1 else "changes"
    text = f"{'Applied' if apply else 'Dry-run'}: {len(changes)} {noun}, {missing} missing"
    pruned = sum(1 for change in changes if prune and change.effect in ("remove", "forget"))
    if pruned:
        text += f", {pruned} pruned"
    return text + "."


def cli() -> None:
    try:
        main()
    except Exception as err:  # noqa: BLE001
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
