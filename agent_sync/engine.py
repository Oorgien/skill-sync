"""Engine: validate a whole plan against state and disk, then apply it and prune.

Planners (the ``kinds`` modules) turn manifest entries into :class:`Op` records. ``check`` decides
what has to change and raises one :class:`PlanError` listing every conflict, before anything is
written. ``_apply_changes`` carries the changes out in order and saves the state after each one, so
the state file always matches the disk, even after a crash. The engine never prints.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NamedTuple

from agent_sync.state import (
    LOCK_FILE,
    STATE_FILE,
    Item,
    Locator,
    State,
    Status,
    classify_path,
    fingerprint_bytes,
    fingerprint_link,
    fingerprint_path,
    fingerprint_tree,
    load_state,
    project_lock,
    save_state,
)

Action = Literal["link", "write", "merge", "remove"]
Effect = Literal["create", "replace", "remove", "adopt", "forget"]

_ACTIONS = ("link", "write", "merge", "remove")
_DIR_KIND = "dir"
_BOOKKEEPING = (STATE_FILE, LOCK_FILE)
# The state and lock files and every folder above them (".agents"): never a target.
_RESERVED = frozenset(
    "/".join(f.parts[:i]) for f in _BOOKKEEPING for i in range(1, len(f.parts) + 1)
)
_NOT_IMPLEMENTED = "merge operations arrive with the hooks and mcp slices"


class Op(NamedTuple):
    """One change a planner wants. Planners build these; only ``_apply_changes`` writes."""

    action: Action
    target: str  # project-relative POSIX path
    kind: str  # "skills", "agents", ...
    harness: str | None = None
    source: str = ""  # manifest selector that produced it, recorded in the state
    payload: Any = None  # link: link text; write: bytes or {relative POSIX path: bytes}
    locator: Locator | None = None  # entry inside a shared file; None = the whole path
    # adopt: the planner vouches for an unrecorded SYMLINK (link: record or relink it; remove:
    # delete it). It never applies to a real file or folder, with or without --force.
    adopt: bool = False
    hint: str = ""  # planner's extra advice, appended to a "Target already exists" conflict


class Change(NamedTuple):
    """An operation that is actually needed, and what it will do."""

    op: Op
    effect: Effect


class Conflict(NamedTuple):
    op: Op
    status: Status
    reason: str  # what is wrong and what to do about it


class PlanError(ValueError):
    """The plan cannot be applied; ``str()`` lists every conflict, one per line."""

    def __init__(self, conflicts: Sequence[Conflict]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__("\n".join(c.reason for c in self.conflicts))


class RunResult(NamedTuple):
    changes: tuple[Change, ...]
    state: State


# --- validation of the shape of a plan ------------------------------------------------------


def _check_relative(path: str, what: str) -> None:
    """Reject anything but a canonical relative POSIX path that stays below its root."""
    if not isinstance(path, str) or not path:
        raise ValueError(f"{what} must be a non-empty relative path, got {path!r}")
    if "\\" in path or "\n" in path:
        raise ValueError(f"{what} {path!r} must not contain a backslash or a newline")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or "/".join(pure.parts) != path:
        raise ValueError(
            f"{what} {path!r} must be a relative path like 'a/b' with no '..', '.', "
            "empty parts or trailing slash"
        )


def _check_tree(files: Mapping[str, Any], target: str) -> None:
    seen: set[str] = set()
    for rel, data in files.items():
        _check_relative(rel, f"file name inside {target}")
        if not isinstance(data, bytes):
            raise ValueError(f"{target}/{rel}: file content must be bytes")  # noqa: TRY004
        seen.add(rel)
    for rel in seen:
        parts = PurePosixPath(rel).parts
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in seen:
                raise ValueError(f"{target}: {rel} is inside {'/'.join(parts[:i])}, a file")
    _check_case(seen, target)


def _check_case(paths: Iterable[str], where: str) -> None:
    """Reject two paths (or folders above them) that differ only by case.

    They are different on Linux and the same on a default macOS or Windows disk, so a run there
    would fail half way.
    """
    seen: dict[str, str] = {}
    for path in paths:
        parts = PurePosixPath(path).parts
        for i in range(1, len(parts) + 1):
            name = "/".join(parts[:i])
            other = seen.setdefault(name.casefold(), name)
            if other != name:
                raise ValueError(
                    f"{where}: {name!r} and {other!r} differ only by case; "
                    "they are the same path on a case-insensitive disk"
                )


def _check_payload(op: Op) -> None:
    if op.action == "link":
        if not isinstance(op.payload, str) or not op.payload:
            raise ValueError(f"{op.target}: a link needs its text as a non-empty str")
    elif op.action == "write":
        if isinstance(op.payload, Mapping):
            _check_tree(op.payload, op.target)
        elif not isinstance(op.payload, bytes):
            raise ValueError(f"{op.target}: write needs bytes or a mapping of path to bytes")


def _check_not_through_symlink(project: Path, target: str) -> None:
    parts = PurePosixPath(target).parts
    for i in range(1, len(parts)):
        rel = "/".join(parts[:i])
        path = project / rel
        if path.is_symlink():
            raise ValueError(
                f"{target}: {rel} is a symlink; agent-sync will not write through a symlink"
            )
        if not os.path.lexists(path):
            return


def _check_bookkeeping(project: Path) -> None:
    """The folders holding the state and lock files must be real folders inside the project."""
    for bookkeeping in _BOOKKEEPING:
        for i in range(1, len(bookkeeping.parts)):
            rel = "/".join(bookkeeping.parts[:i])
            path = project / rel
            if path.is_symlink():
                raise ValueError(
                    f"{rel} is a symlink; agent-sync keeps its state there and will not follow it"
                )
            if os.path.lexists(path) and not path.is_dir():
                raise ValueError(
                    f"{rel} exists and is not a folder; agent-sync keeps its state there"
                )
        file = project / bookkeeping
        if file.is_symlink():
            raise ValueError(
                f"{bookkeeping.as_posix()} is a symlink; agent-sync will not follow it"
            )
        if os.path.lexists(file) and not file.is_file():
            raise ValueError(f"{bookkeeping.as_posix()} exists and is not a file")


def _validate_shape(project: Path, ops: Sequence[Op]) -> None:
    _check_bookkeeping(project)
    seen: set[tuple[str, Locator | None]] = set()
    for op in ops:
        if op.action not in _ACTIONS:
            raise ValueError(f"unknown operation {op.action!r} for {op.target!r}")
        _check_relative(op.target, "target")
        if op.target in _RESERVED:
            raise ValueError(f"{op.target} is agent-sync's own bookkeeping and cannot be a target")
        if op.kind == _DIR_KIND:
            raise ValueError(f"{op.target}: kind {_DIR_KIND!r} is reserved for folders")
        if op.action == "merge" or op.locator is not None:
            raise NotImplementedError(_NOT_IMPLEMENTED)
        key = (op.target, op.locator)
        if key in seen:
            raise ValueError(f"{op.target} is targeted twice in the plan")
        seen.add(key)
        _check_payload(op)
        _check_not_through_symlink(project, op.target)
    whole = {op.target for op in ops}
    _check_case(sorted(whole), "plan")
    for target in whole:
        parts = PurePosixPath(target).parts
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in whole:
                raise ValueError(
                    f"targets overlap: {target} is inside {'/'.join(parts[:i])}, "
                    "which is also in the plan"
                )


# --- check ----------------------------------------------------------------------------------


def _desired(op: Op) -> str | None:
    if op.action == "link":
        return fingerprint_link(op.payload)
    if op.action == "write":
        if isinstance(op.payload, Mapping):
            return fingerprint_tree(op.payload)
        return fingerprint_bytes(op.payload)
    return None


def _blocking_parent(project: Path, target: str) -> str | None:
    """The first existing parent of target that is not a folder, if any."""
    parts = PurePosixPath(target).parts
    for i in range(1, len(parts)):
        rel = "/".join(parts[:i])
        path = project / rel
        if os.path.lexists(path) and not path.is_dir():
            return rel
    return None


def _edited(target: str) -> str:
    return f"{target} was changed since agent-sync wrote it; delete it or re-run with --force"


def _foreign(project: Path, op: Op) -> str:
    text = (
        f"Target already exists: {project / op.target} (not created by agent-sync); "
        "move or delete it (--force never touches it)"
    )
    return f"{text}. {op.hint}" if op.hint else text


def _may_adopt(project: Path, op: Op) -> bool:
    """Adoption is only ever for a symlink; a real file or folder is the user's."""
    return op.adopt and (project / op.target).is_symlink()


def _folder_conflict(project: Path, state: State, op: Op) -> Conflict | None:
    """A folder agent-sync created may hold the user's files: never replaced, never removed."""
    item = state.find(op.target)
    if item is not None and item.kind == _DIR_KIND and os.path.lexists(project / op.target):
        return Conflict(
            op,
            Status.NOT_OURS,
            f"{op.target} is a folder agent-sync created; it may hold your files",
        )
    return None


def _check_write(project: Path, state: State, op: Op, force: bool) -> Change | Conflict | None:
    folder = _folder_conflict(project, state, op)
    if folder is not None:
        return folder
    status = classify_path(project, state, op.target)
    desired = _desired(op)
    if status is Status.ABSENT:
        blocker = _blocking_parent(project, op.target)
        if blocker is not None:
            return Conflict(
                op, Status.NOT_OURS, f"{op.target} cannot be created: {blocker} is not a folder"
            )
        return Change(op, "create")
    if status is Status.OWNED_UNCHANGED:
        recorded = state.find(op.target)
        if recorded is not None and recorded.sha256 == desired:
            return None
        return Change(op, "replace")
    if status is Status.OWNED_EDITED:
        if fingerprint_path(project / op.target) == desired:
            return Change(op, "adopt")  # already what we want; only the record is stale
        if force:
            return Change(op, "replace")
        return Conflict(op, status, _edited(op.target))
    if not _may_adopt(project, op):
        return Conflict(op, status, _foreign(project, op))
    if fingerprint_path(project / op.target) == desired:
        return Change(op, "adopt")
    return Change(op, "replace")


def _check_remove(project: Path, state: State, op: Op, force: bool) -> Change | Conflict | None:
    folder = _folder_conflict(project, state, op)
    if folder is not None:
        return folder
    status = classify_path(project, state, op.target)
    if status is Status.ABSENT:
        return Change(op, "forget") if state.find(op.target) is not None else None
    if status is Status.OWNED_UNCHANGED or (status is Status.OWNED_EDITED and force):
        return Change(op, "remove")
    if status is Status.OWNED_EDITED:
        return Conflict(op, status, _edited(op.target))
    if _may_adopt(project, op):
        return Change(op, "remove")  # the planner vouched for it (an old install's link)
    return Conflict(op, status, _foreign(project, op))


def check(project: Path, state: State, ops: Sequence[Op], *, force: bool) -> list[Change]:
    """Decide what has to change. Pure: reads the disk, never writes.

    Raises ``ValueError`` for a malformed plan, ``NotImplementedError`` for ``merge``, and one
    :class:`PlanError` listing every conflict. Operations that need no change are dropped.
    """
    _validate_shape(project, ops)
    changes: list[Change] = []
    conflicts: list[Conflict] = []
    for op in ops:
        outcome = (
            _check_remove(project, state, op, force)
            if op.action == "remove"
            else _check_write(project, state, op, force)
        )
        if isinstance(outcome, Conflict):
            conflicts.append(outcome)
        elif outcome is not None:
            changes.append(outcome)
    if conflicts:
        raise PlanError(conflicts)
    return changes


# --- prune ----------------------------------------------------------------------------------


def prune_ops(state: State, ops: Sequence[Op]) -> list[Op]:
    """One ``remove`` per owned item that no operation in the plan produces.

    Folders the engine created (kind ``dir``) are never pruned; they go when they are empty.
    """
    planned = {(op.target, op.locator) for op in ops}
    return [
        Op("remove", i.target, i.kind, i.harness, i.source, None, i.locator)
        for i in state.items
        if i.kind != _DIR_KIND and (i.target, i.locator) not in planned
    ]


# --- apply: filesystem primitives -----------------------------------------------------------


def _sibling(dest: Path) -> Path:
    """A fresh, unused name next to dest, for temporary files and folders."""
    return dest.with_name(f".{dest.name}.agent-sync-{uuid.uuid4().hex[:12]}")


def _discard(path: Path) -> None:
    """Best-effort delete of a link, file or folder; never follows a symlink."""
    if path.is_symlink() or not path.is_dir():
        try:
            path.unlink()
        except OSError:
            pass
    else:
        shutil.rmtree(path, ignore_errors=True)


def _write_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _build(tmp: Path, payload: Any) -> None:
    if isinstance(payload, Mapping):
        os.mkdir(tmp)
        for rel, data in payload.items():
            path = tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_file(path, data)
    elif isinstance(payload, str):
        os.symlink(payload, tmp)
    else:
        _write_file(tmp, payload)


def _install(dest: Path, payload: Any) -> None:
    """Make ``dest`` hold the payload: build it next to dest, then swap it in."""
    tmp = _sibling(dest)
    try:
        _build(tmp, payload)
        existing = os.path.lexists(dest)
        real_dir = existing and dest.is_dir() and not dest.is_symlink()
        if not existing or not (isinstance(payload, Mapping) or real_dir):
            os.replace(tmp, dest)  # atomic for file/link over file/link, or onto nothing
            return
        old = _sibling(dest)  # a folder is involved: park the old thing, move the new one in
        os.rename(dest, old)
        try:
            os.rename(tmp, dest)
        except BaseException:
            os.rename(old, dest)
            raise
        _discard(old)
    except BaseException:
        _discard(tmp)
        raise


def _delete(dest: Path) -> None:
    if dest.is_symlink() or not dest.is_dir():
        dest.unlink(missing_ok=True)
        return
    old = _sibling(dest)
    os.rename(dest, old)  # one atomic step; the slow delete happens after the path is gone
    _discard(old)


# --- apply: folders -------------------------------------------------------------------------


def _ensure_parents(project: Path, state: State, target: str) -> tuple[State, list[str]]:
    """Create missing folders above target, each recorded (and saved) before it is created."""
    created: list[str] = []
    parts = PurePosixPath(target).parts
    for i in range(1, len(parts)):
        rel = "/".join(parts[:i])
        path = project / rel
        if path.is_symlink():
            raise ValueError(f"{target}: {rel} is a symlink; refusing to write through it")
        if path.is_dir():
            continue
        state = state.with_item(Item(_DIR_KIND, None, rel, None, "", ""))
        try:
            save_state(project, state)
            os.mkdir(path)  # raises if something that is not a folder is in the way
        except BaseException:
            # Exactly what this call recorded or created, deepest first (rel may not exist yet).
            _rollback_folders(project, state, [*created, rel])
            raise
        created.append(rel)
    return state, created


def _rollback_folders(project: Path, state: State, rels: Sequence[str]) -> None:
    """After a failure: drop the given folders (deepest first) and their records, then save.

    A folder is removed only if it exists and is empty; its record is dropped either way.
    """
    for rel in reversed(rels):
        path = project / rel
        with contextlib.suppress(OSError):
            if not path.is_symlink() and path.is_dir() and not any(path.iterdir()):
                os.rmdir(path)
        state = state.without(rel)
    with contextlib.suppress(OSError):
        save_state(project, state)


def _prune_empty_dirs(project: Path, state: State, target: str) -> State:
    """Delete folders above target that the engine created and that are now empty."""
    parts = PurePosixPath(target).parts
    for i in range(len(parts) - 1, 0, -1):
        rel = "/".join(parts[:i])
        item = state.find(rel)
        if item is None or item.kind != _DIR_KIND:
            break
        path = project / rel
        if os.path.lexists(path):
            if path.is_symlink() or not path.is_dir():
                state = state.without(rel)
                break
            if any(path.iterdir()):
                break
            os.rmdir(path)
        state = state.without(rel)
    return state


# --- apply ----------------------------------------------------------------------------------


def _record(project: Path, state: State, op: Op) -> State:
    """Record what is now on disk at the target, and save."""
    sha = fingerprint_path(project / op.target)
    if sha is None:
        raise RuntimeError(f"{op.target} is missing right after it was written")
    state = state.with_item(Item(op.kind, op.harness, op.target, op.locator, op.source, sha))
    save_state(project, state)
    return state


def _write(project: Path, state: State, change: Change) -> State:
    op, effect = change
    dest = project / op.target
    if effect == "create" and os.path.lexists(dest):
        raise FileExistsError(f"{op.target} appeared since the plan was checked")
    state, created = _ensure_parents(project, state, op.target)
    # A new item is recorded before it is written: a crash right after the write then leaves a
    # record that matches the disk, not an unowned file. (A replaced item keeps its old record
    # until the write is done; the next run sees "edited, but already what the plan wants".)
    planned = state
    try:
        if effect == "create":
            sha = _desired(op)
            assert sha is not None
            planned = state.with_item(
                Item(op.kind, op.harness, op.target, op.locator, op.source, sha)
            )
            save_state(project, planned)
        _install(dest, op.payload)
    except BaseException:
        if created or planned is not state:
            _rollback_folders(project, state, created)
        raise
    if planned is not state and fingerprint_path(dest) == _desired(op):
        return planned
    return _record(project, planned, op)


def _apply_change(project: Path, state: State, change: Change) -> State:
    """Execute one change; the state on disk matches the project when it returns or raises."""
    op, effect = change
    if effect in ("create", "replace"):
        return _write(project, state, change)
    if effect == "adopt":
        if fingerprint_path(project / op.target) != _desired(op):
            raise RuntimeError(
                f"{op.target} changed since the plan was checked; nothing was recorded, run again"
            )
        return _record(project, state, op)
    if effect == "remove":
        _check_not_through_symlink(project, op.target)
        _delete(project / op.target)
    state = state.without(op.target, op.locator)  # remove, forget
    save_state(project, state)
    state = _prune_empty_dirs(project, state, op.target)
    save_state(project, state)
    return state


def _apply_changes(project: Path, state: State, changes: Sequence[Change]) -> State:
    """Execute the changes in order, saving the state after each one."""
    for change in changes:
        state = _apply_change(project, state, change)
        save_state(project, state)
    return state


def _plan(state: State, ops: Sequence[Op], prune: bool) -> list[Op]:
    plan = list(ops)
    if prune:
        plan += prune_ops(state, plan)
    return plan


def run(project: Path, ops: Sequence[Op], *, apply: bool, prune: bool, force: bool) -> RunResult:
    """Plan, validate and (with ``apply``) execute.

    The plan is checked first without the lock, so a bad plan, a conflict, a dry run and a run
    with nothing to do write nothing at all (not even ``.agents/``). Only when there is work is
    the lock taken; the state is then read again and the plan re-checked before anything is
    written.
    """
    _check_bookkeeping(project)
    state = load_state(project)
    changes = check(project, state, _plan(state, ops, prune), force=force)
    if not apply or not changes:
        return RunResult(tuple(changes), state)
    with project_lock(project):
        state = load_state(project)
        changes = check(project, state, _plan(state, ops, prune), force=force)
        state = _apply_changes(project, state, changes)
    return RunResult(tuple(changes), state)
