"""Command line interface: argument parsing, orchestration, and error handling."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Collection, Sequence
from pathlib import Path

from agent_sync import engine
from agent_sync.context import Context
from agent_sync.engine import Change, Op
from agent_sync.gitexclude import sync_exclude
from agent_sync.kinds.agents import plan_agents
from agent_sync.kinds.commands import plan_commands
from agent_sync.kinds.hooks import plan_hooks
from agent_sync.kinds.mcp import plan_mcp
from agent_sync.kinds.rules import plan_rules
from agent_sync.kinds.skills import legacy_removals, plan_skills
from agent_sync.kinds.source_files import KindPlan
from agent_sync.manifest import load_manifest
from agent_sync.sources import check_personal_dir
from agent_sync.state import LOCK_FILE, STATE_FILE, Locator, State, load_state

_CODEX_TRUST_KINDS = ("agents", "rules", "hooks", "mcp")


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
            "Install skills, agents, commands, rules, hooks and MCP servers from a YAML manifest: "
            "sources use a shared cache. A dry run is the default; nothing is "
            "written without --apply."
        ),
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=Path.cwd(),
        help="Project to install artifacts into. Defaults to the current directory.",
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
    parser.add_argument(
        "--apply", action="store_true", help="Download sources and install selected artifacts"
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Refresh already-downloaded sources (git pull / redownload) to pick up new upstream skills",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Remove owned artifacts that the manifest no longer lists",
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
    # Resolve desired content before considering any legacy link deletion.
    skill_plan = plan_skills([e for e in entries if e.kind == "skills"], ctx._replace(prune=False))
    ops = list(skill_plan.ops)
    missing = list(skill_plan.missing)
    diagnostics: list[str] = []
    for kind, planner in (
        ("agents", plan_agents),
        ("commands", plan_commands),
        ("rules", plan_rules),
        ("hooks", plan_hooks),
        ("mcp", plan_mcp),
    ):
        part = planner([e for e in entries if e.kind == kind], ctx)
        ops.extend(part.ops)
        missing.extend(part.missing)
        diagnostics.extend(part.diagnostics)
    _check_shared_names(state, ops)
    effective_prune = args.prune and not missing
    if args.prune and missing:
        diagnostics.append("Pruning skipped: source inventory is incomplete.")
    if effective_prune:
        ops.extend(legacy_removals(ctx, {op.target for op in ops}))
    plan = KindPlan(ops, missing, diagnostics)
    for diagnostic in plan.diagnostics:
        print(diagnostic)
    if any(op.harness == "codex" and op.kind in _CODEX_TRUST_KINDS for op in ops):
        print(
            "Codex project configuration may require project trust and a client restart; "
            "agent-sync does not change trust settings."
        )

    missing_sources = list(dict.fromkeys(f"{e.source}/{e.repo}" for e in plan.missing))
    for source in missing_sources:
        print(f"Missing source (run with --apply): {source}")

    # Check the exclude block before the engine writes anything: a broken block or a bad path
    # then fails the whole run while the project is still untouched.
    would_be = _exclude_paths(
        project_root, state, plan.ops, prune=effective_prune, create_bookkeeping=args.apply
    )
    checked = sync_exclude(project_root, would_be, apply=False)

    result = engine.run(
        project_root, plan.ops, apply=args.apply, prune=effective_prune, force=args.force
    )
    for change in result.changes:
        print(_describe(change, project_root, args.apply))
    if any(
        c.op.kind == "hooks" and c.op.harness == "codex" and c.effect in ("create", "replace", "adopt")
        for c in result.changes
    ):
        print("Codex skips new or changed hooks until you review and trust them (/hooks in Codex)")
    moves = {(op.target, op.locator) for op in plan.ops if op.kind == "hooks" and op.action == "remove"}
    print(_summary(result.changes, len(missing_sources), args.apply, effective_prune, moves))

    if args.apply:
        # List the state and lock files only if the run really left them behind.
        paths = _exclude_paths(
            project_root, result.state, plan.ops, prune=effective_prune, create_bookkeeping=False
        )
        final = sync_exclude(project_root, paths, apply=True)
    else:
        final = checked
    if final.note:
        print(final.note)
    elif final.changed:
        print(f"{'Update' if args.apply else 'Would update'} {final.path}")


def _check_shared_names(state: State, ops: Sequence[Op]) -> None:
    """A command never takes over a skill, including an owned or legacy skill link."""
    seen: dict[str, str] = {}
    for op in ops:
        if op.kind not in ("skills", "commands") or op.target == ".claude/skills":
            continue
        previous = seen.setdefault(op.target.casefold(), op.kind)
        recorded = state.find(op.target)
        if previous != op.kind or (
            recorded is not None
            and recorded.kind in ("skills", "commands")
            and recorded.kind != op.kind
        ):
            raise ValueError(
                f"Collision for {op.target}: cannot switch skills/commands kind; "
                "remove the old item in a separate prune run first"
            )


def _exclude_paths(
    project: Path, state: State, ops: Sequence[Op], *, prune: bool, create_bookkeeping: bool
) -> list[str]:
    """Every whole path agent-sync will own after this run, except folders it merely created."""
    paths = {op.target for op in ops if op.action != "remove" and op.locator is None}
    if not prune:  # without prune every recorded item survives
        removed = {op.target for op in ops if op.action == "remove" and op.locator is None}
        paths |= {
            item.target
            for item in state.items
            if item.kind != "dir" and item.locator is None and item.target not in removed
        }
    # A file that entries are merged into is ours only if agent-sync created it (a "file" item,
    # or it does not exist yet); a file the user already had is never listed.
    created = {i.target for i in state.items if i.kind == "file"}
    # A created file that still exists stays ours with or without --prune (the user may have
    # added content to it), so the block is the same on every run.
    removed_paths = {op.target for op in ops if op.action == "remove" and op.locator is None}
    paths |= {t for t in created - removed_paths if os.path.lexists(project / t)}
    paths |= {
        op.target
        for op in ops
        if op.action == "merge"
        and (op.target in created or not os.path.lexists(project / op.target))
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


_MERGE_VERBS = {
    "create": ("Add", "Would add"),
    "replace": ("Replace", "Would replace"),
    "remove": ("Remove", "Would remove"),
    "adopt": ("Adopt", "Would adopt"),
    "forget": ("Forget", "Would forget"),
}


def _describe_merge(change: Change, apply: bool) -> str:
    """A hook or an MCP server inside a shared file, with the full command for a hook."""
    op, effect = change
    verb = _MERGE_VERBS[effect][0 if apply else 1]
    name = op.locator[-1] if op.locator else op.target
    if op.kind == "hooks" and op.locator:
        line = f"{verb} hook {op.harness} {op.locator[1]}"
        group = op.payload if isinstance(op.payload, dict) else {}
        if isinstance(group.get("matcher"), str):
            line += f" [matcher: {group['matcher']}]"
        line += f" {name}"
        if effect in ("create", "replace", "adopt"):
            commands = [h.get("command") for h in group.get("hooks", []) if isinstance(h, dict)]
            line += ": " + " && ".join(str(c) for c in commands if c is not None)
        return line
    line = f"{verb} MCP server {name} ({op.harness}) -> {op.target}"
    server = op.payload if isinstance(op.payload, dict) and op.action == "merge" else {}
    if isinstance(server.get("url"), str):
        line += f": {server['url']}"
    elif isinstance(server.get("command"), str):
        args = server.get("args")
        parts = [server["command"], *(str(a) for a in args)] if isinstance(args, list) else [server["command"]]
        line += ": " + " ".join(parts)
    return line


def _describe(change: Change, project: Path, apply: bool) -> str:
    op, effect = change
    if op.action in ("merge", "remove") and op.locator is not None and op.kind in ("hooks", "mcp"):
        return _describe_merge(change, apply)
    verb, would = _VERBS[effect]
    if effect in ("create", "replace") and op.action == "write":
        verb, would = "Write", "Would write"
    line = f"{verb if apply else would} {project / op.target}"
    if effect in ("create", "replace") and op.action == "link":
        line += f" -> {op.payload}"
    return line


def _summary(
    changes: Sequence[Change], missing: int, apply: bool, prune: bool,
    moves: Collection[tuple[str, Locator | None]] = (),
) -> str:
    noun = "change" if len(changes) == 1 else "changes"
    text = f"{'Applied' if apply else 'Dry-run'}: {len(changes)} {noun}, {missing} missing"
    pruned = sum(
        1 for change in changes
        if prune and change.effect in ("remove", "forget")
        and (change.op.target, change.op.locator) not in moves  # a planner's move is not a prune
    )
    if pruned:
        text += f", {pruned} pruned"
    return text + "."


def cli() -> None:
    try:
        main()
    except Exception as err:  # noqa: BLE001
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
