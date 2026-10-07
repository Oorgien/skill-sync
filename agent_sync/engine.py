"""Engine: validate a whole plan against state and disk, then apply it and prune.

Planners (the ``kinds`` modules) turn manifest entries into :class:`Op` records. ``check`` decides
what has to change and raises one :class:`PlanError` listing every conflict, before anything is
written. ``_apply_changes`` carries the changes out in order and saves the state after each one, so
the state file always matches the disk, even after a crash. The engine never prints.
"""

from __future__ import annotations

import contextlib
import copy
import os
import shutil
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NamedTuple

from agent_sync import shared_file
from agent_sync.state import (
    LOCK_FILE,
    STATE_FILE,
    Item,
    Locator,
    State,
    Status,
    classify_entry,
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
Container = Literal["key", "list"]

_ACTIONS = ("link", "write", "merge", "remove")
_DIR_KIND = "dir"
_FILE_KIND = "file"  # a JSON/TOML file agent-sync created; like a folder, it may hold user content
_CONTAINER_KIND = "container"  # a table/list inside a file that the engine created for an entry
_OWNED_CONTAINER_KINDS = (_DIR_KIND, _FILE_KIND)
_NEVER_PRUNED = (_DIR_KIND, _FILE_KIND, _CONTAINER_KIND)
_FILE_OWNED = (_FILE_KIND, _CONTAINER_KIND)
# Kinds whose entries are elements of a list, for the removes that prune_ops builds from the state.
_LIST_KINDS = frozenset({"hooks"})
_BOOKKEEPING = (STATE_FILE, LOCK_FILE)
# The state and lock files and every folder above them (".agents"): never a target.
_RESERVED = frozenset(
    "/".join(f.parts[:i]) for f in _BOOKKEEPING for i in range(1, len(f.parts) + 1)
)


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
    # merge/remove with a locator. "key": the entry is the value at the key path ``locator``.
    # "list": the entry is an element of the list at ``locator[:-1]``; ``locator[-1]`` is a stable
    # name that is only kept in the state, and the element is found by its content fingerprint.
    container: Container = "key"


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


def _check_entry_op(op: Op) -> None:
    where = op.target
    if op.action not in ("merge", "remove"):
        raise ValueError(f"{where}: only merge and remove can have a locator")
    if (
        not isinstance(op.locator, tuple)
        or not op.locator
        or not all(isinstance(k, str) and k for k in op.locator)
    ):
        raise ValueError(f"{where}: {op.action} needs a locator, a non-empty tuple of key names")
    if op.container not in ("key", "list"):
        raise ValueError(f"{where}: unknown container {op.container!r}")
    fmt = shared_file.format_of(op.target)
    if op.container == "list":
        if len(op.locator) < 2:
            raise ValueError(f"{where}: a list entry's locator is the list's path plus a name")
        if fmt != "json":
            raise ValueError(f"{where}: list entries are only supported in JSON files")
    if op.action == "merge" and op.payload is None:
        raise ValueError(f"{where}: merge needs a payload")


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
        if op.kind in (_FILE_KIND, _CONTAINER_KIND):
            raise ValueError(f"{op.target}: kind {op.kind!r} is reserved for the engine")
        if op.action == "merge" or op.locator is not None:
            _check_entry_op(op)
        key = (op.target, op.locator)
        if key in seen:
            raise ValueError(f"{op.target} is targeted twice in the plan")
        seen.add(key)
        _check_payload(op)
        _check_not_through_symlink(project, op.target)
    entry_files = {op.target for op in ops if op.locator is not None}
    for op in ops:
        if op.locator is None and op.target in entry_files:
            raise ValueError(
                f"{op.target}: a whole-path operation and merged entries on the same file "
                "cannot be in one plan"
            )
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
    if (
        item is not None
        and item.kind in _OWNED_CONTAINER_KINDS
        and os.path.lexists(project / op.target)
    ):
        what = "a folder" if item.kind == _DIR_KIND else "a file"
        held = "files" if item.kind == _DIR_KIND else "content"
        return Conflict(
            op,
            Status.NOT_OURS,
            f"{op.target} is {what} agent-sync created; it may hold your {held}",
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


def _label(op: Op) -> str:
    assert op.locator is not None
    return f"{op.target} [{' > '.join(op.locator)}]"


def _foreign_entry(project: Path, op: Op, why: str) -> Conflict:
    text = (
        f"Target already exists: {project / op.target} [{' > '.join(op.locator or ())}] "
        f"{why}; move or delete it (--force never touches it)"
    )
    return Conflict(op, Status.NOT_OURS, f"{text}. {op.hint}" if op.hint else text)


def _check_entry(
    project: Path, state: State, op: Op, force: bool, doc: shared_file.Doc
) -> Change | Conflict | None:
    outcome = _decide_entry(project, state, op, force, doc)
    if isinstance(outcome, Change) and outcome.effect in ("create", "replace", "remove"):
        return _simulate(state, outcome, doc) or outcome
    return outcome


def _own_text(doc: shared_file.Doc, op: Op) -> str:
    """Comments inside the entry itself may go with it; all others must stay."""
    assert op.locator is not None
    return shared_file.entry_text(doc, op.locator) if op.container == "key" else ""


def _simulate(state: State, change: Change, doc: shared_file.Doc) -> Conflict | None:
    """Make the edit on a copy of the document; a layout that cannot take it is a Conflict."""
    op, effect = change
    assert op.locator is not None
    if effect != "remove":
        problem = shared_file.payload_problem(doc.fmt, op.payload)
        if problem is not None:
            return Conflict(
                op,
                Status.NOT_OURS,
                f"{_label(op)} has a payload that cannot be written to a {doc.fmt} file: {problem}",
            )
    mutate = _remove_mutation(op, effect, state) if effect == "remove" else _put_mutation(op, state)
    try:
        shared_file.apply_edit(copy.deepcopy(doc), op.target, mutate, _own_text(doc, op))
    except Exception as exc:  # noqa: BLE001 (tomlkit raises many types for unsupported layouts)
        parent = ".".join(op.locator[:-1] if op.container == "key" else op.locator[:-2])
        advice = ""
        if doc.fmt == "toml" and parent:
            advice = f" If {parent} is written with dotted keys or inline, use a [{parent}] table header."
        return Conflict(
            op,
            Status.NOT_OURS,
            f"{_label(op)} cannot be edited safely in this layout ({exc}).{advice}",
        )
    return None


def _decide_entry(
    project: Path, state: State, op: Op, force: bool, doc: shared_file.Doc
) -> Change | Conflict | None:
    if op.container == "list":
        return _check_list_entry(project, state, op, doc)
    assert op.locator is not None
    found = shared_file.lookup(doc.root, op.locator[:-1])
    if found.blocked_at is not None or (found.present and not isinstance(found.value, Mapping)):
        return Conflict(
            op, Status.NOT_OURS, f"{_label(op)} cannot be used: a parent of it is not a table"
        )
    current = shared_file.lookup(doc.root, op.locator)
    status = classify_entry(
        state, op.target, op.locator, shared_file.plain(current.value), current.present
    )
    recorded = state.find(op.target, op.locator)
    if op.action == "remove":
        if status is Status.ABSENT:
            return Change(op, "forget") if recorded is not None else None
        if status is Status.OWNED_UNCHANGED or (status is Status.OWNED_EDITED and force):
            return Change(op, "remove")
        if status is Status.OWNED_EDITED:
            return Conflict(op, status, _edited(_label(op)))
        return _foreign_entry(project, op, "(not created by agent-sync)")
    desired = shared_file.fingerprint(op.payload)
    if status is Status.ABSENT:
        return _check_creatable(project, op, doc)
    if status is Status.OWNED_UNCHANGED:
        return (
            None if recorded is not None and recorded.sha256 == desired else Change(op, "replace")
        )
    if status is Status.OWNED_EDITED:
        if shared_file.fingerprint(current.value) == desired:
            return Change(op, "adopt")
        if force:
            return Change(op, "replace")
        return Conflict(op, status, _edited(_label(op)))
    return _foreign_entry(project, op, "(not created by agent-sync)")


def _check_creatable(project: Path, op: Op, doc: shared_file.Doc) -> Change | Conflict:
    if not doc.exists:
        blocker = _blocking_parent(project, op.target)
        if blocker is not None:
            return Conflict(
                op, Status.NOT_OURS, f"{op.target} cannot be created: {blocker} is not a folder"
            )
    return Change(op, "create")


def _owned_elsewhere(state: State, op: Op, sha: str) -> bool:
    return any(
        i.target == op.target
        and i.locator is not None
        and i.locator != op.locator
        and i.kind not in _NEVER_PRUNED
        and i.sha256 == sha
        for i in state.items
    )


def _check_list_entry(
    project: Path, state: State, op: Op, doc: shared_file.Doc
) -> Change | Conflict | None:
    assert op.locator is not None
    recorded = state.find(op.target, op.locator)
    holder = shared_file.get_list(doc, op.locator)
    if holder.blocked_at is not None or (holder.present and isinstance(holder.value, Mapping)):
        return Conflict(
            op, Status.NOT_OURS, f"{_label(op)} cannot be used: {op.locator[-2]} is not a list"
        )
    items = holder.value if holder.present else []
    index = shared_file.find_index(items, recorded.sha256) if recorded is not None else None
    if op.action == "remove":
        if recorded is None:
            return None
        return Change(op, "remove" if index is not None else "forget")
    desired = shared_file.fingerprint(op.payload)
    if recorded is not None and index is not None:
        return None if recorded.sha256 == desired else Change(op, "replace")
    if shared_file.find_index(items, desired) is not None:
        if recorded is not None and not _owned_elsewhere(state, op, desired):
            return Change(op, "adopt")  # a crash after the write left the old record: refresh it
        return _foreign_entry(project, op, "(an identical entry already exists)")
    return _check_creatable(project, op, doc)


def check(project: Path, state: State, ops: Sequence[Op], *, force: bool) -> list[Change]:
    """Decide what has to change. Pure: reads the disk, never writes.

    Raises ``ValueError`` for a malformed plan or an unreadable JSON/TOML target, and one
    :class:`PlanError` listing every conflict. Operations that need no change are dropped.
    """
    _validate_shape(project, ops)
    changes: list[Change] = []
    conflicts: list[Conflict] = []
    docs: dict[str, shared_file.Doc] = {}
    for op in ops:
        if op.locator is not None:
            if op.target not in docs:
                docs[op.target] = shared_file.load_doc(project, op.target)
            outcome = _check_entry(project, state, op, force, docs[op.target])
        elif op.action == "remove":
            outcome = _check_remove(project, state, op, force)
        else:
            outcome = _check_write(project, state, op, force)
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

    Folders (kind ``dir``) and files (kind ``file``) the engine created are never pruned; a
    folder goes when it is empty, a file when its last entry is removed and it is empty.
    """
    planned = {(op.target, op.locator) for op in ops}
    return [
        Op(
            "remove",
            i.target,
            i.kind,
            i.harness,
            i.source,
            None,
            i.locator,
            container="list" if i.kind in _LIST_KINDS else "key",
        )
        for i in state.items
        if i.kind not in _NEVER_PRUNED and (i.target, i.locator) not in planned
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


# --- apply: entries inside JSON/TOML files --------------------------------------------------


def _save(project: Path, state: State) -> State:
    save_state(project, state)
    return state


def _put_mutation(op: Op, state: State) -> Callable[[shared_file.Doc], list[tuple[str, ...]]]:
    """Insert or replace the entry; returns the containers the edit created."""
    assert op.locator is not None
    locator = op.locator
    recorded = state.find(op.target, locator)

    def mutate(doc: shared_file.Doc) -> list[tuple[str, ...]]:
        created: list[tuple[str, ...]] = []
        if op.container == "list":
            holder = shared_file.get_list(doc, locator)
            index = None
            if recorded is not None and holder.present:
                index = shared_file.find_index(holder.value, recorded.sha256)
            if index is None:
                shared_file.append_element(doc, locator, op.payload, created)
            else:
                shared_file.replace_element(doc, locator, index, op.payload)
        else:
            tail = shared_file.capture_tail(doc, locator)
            shared_file.set_key(doc, locator, op.payload, created)
            shared_file.restore_tail(doc, locator, tail)
        return created

    return mutate


class _Removal(NamedTuple):
    changed: bool
    pruned: list[tuple[str, ...]]


def _remove_mutation(op: Op, effect: Effect, state: State) -> Callable[[shared_file.Doc], _Removal]:
    """Delete the entry and the containers the engine created that are now empty."""
    assert op.locator is not None
    locator = op.locator
    recorded = state.find(op.target, locator)
    decided: dict[tuple[str, ...], bool] = {}

    def may_delete(path: tuple[str, ...], container: Any) -> bool:
        if path not in decided:  # decided on the real document, reused for the plain copy
            item = state.find(op.target, path)
            decided[path] = (
                item is not None
                and item.kind == _CONTAINER_KIND
                and not shared_file.has_comment(container)
            )
        return decided[path]

    def mutate(doc: shared_file.Doc) -> _Removal:
        if effect != "remove":
            return _Removal(False, [])
        if op.container == "list":
            holder = shared_file.get_list(doc, locator)
            index = None
            if recorded is not None and holder.present:
                index = shared_file.find_index(holder.value, recorded.sha256)
            if index is None:
                return _Removal(False, [])
            shared_file.delete_element(doc, locator, index)
            tail = None
        elif shared_file.lookup(doc.root, locator).present:
            tail = shared_file.capture_tail(doc, locator)
            shared_file.delete_key(doc, locator)
        else:
            return _Removal(False, [])
        pruned = shared_file.prune_empty(doc, locator[:-1], may_delete)
        shared_file.restore_tail(doc, locator, tail)
        return _Removal(True, pruned)

    return mutate


def _write_entry(project: Path, state: State, change: Change) -> State:
    op, effect = change
    assert op.locator is not None
    _check_not_through_symlink(project, op.target)
    path = project / op.target
    doc = shared_file.load_doc(project, op.target)
    item = Item(
        op.kind, op.harness, op.target, op.locator, op.source, shared_file.fingerprint(op.payload)
    )
    created_dirs: list[str] = []
    base = state
    early = False
    try:
        if not doc.exists:
            base, created_dirs = _ensure_parents(project, state, op.target)
        data, created = shared_file.apply_edit(
            doc, op.target, _put_mutation(op, state), _own_text(doc, op)
        )
        state = base
        if not doc.exists:
            state = state.with_item(Item(_FILE_KIND, None, op.target, None, "", ""))
        for container in created:
            state = state.with_item(Item(_CONTAINER_KIND, None, op.target, container, "", ""))
        if effect == "create" or created or created_dirs:
            # Recorded before it is written, like a new whole path: a crash after the write then
            # leaves a record that matches the disk, not an entry nobody owns.
            early = True
            state = _save(project, state.with_item(item))
        shared_file.write_atomic(path, data)
    except BaseException:
        if early or created_dirs:
            _rollback_folders(project, base, created_dirs)
        raise
    return _save(project, state.with_item(item))


def _adopt_entry(project: Path, state: State, change: Change) -> State:
    op = change.op
    assert op.locator is not None
    doc = shared_file.load_doc(project, op.target)
    sha = shared_file.fingerprint(op.payload)
    if op.container == "list":
        holder = shared_file.get_list(doc, op.locator)
        found = holder.present and shared_file.find_index(holder.value, sha) is not None
    else:
        current = shared_file.lookup(doc.root, op.locator)
        found = current.present and shared_file.fingerprint(current.value) == sha
    if not found:
        raise RuntimeError(
            f"{_label(op)} changed since the plan was checked; nothing was recorded, run again"
        )
    item = Item(op.kind, op.harness, op.target, op.locator, op.source, sha)
    return _save(project, state.with_item(item))


def _remove_entry(project: Path, state: State, change: Change) -> State:
    op, effect = change
    assert op.locator is not None
    _check_not_through_symlink(project, op.target)
    path = project / op.target
    doc = shared_file.load_doc(project, op.target)
    data, removal = shared_file.apply_edit(
        doc, op.target, _remove_mutation(op, effect, state), _own_text(doc, op)
    )
    owned = state.find(op.target)
    drop_file = owned is not None and owned.kind == _FILE_KIND
    if drop_file and doc.exists and not doc.is_empty():
        drop_file = False
    if drop_file and doc.exists:
        path.unlink()
    elif removal.changed:
        shared_file.write_atomic(path, data)
    state = state.without(op.target, op.locator)
    for container in removal.pruned:
        state = state.without(op.target, container)
    if drop_file:
        state = State(
            tuple(i for i in state.items if not (i.target == op.target and i.kind in _FILE_OWNED))
        )
    state = _save(project, state)
    return _save(project, _prune_empty_dirs(project, state, op.target))


def _apply_change(project: Path, state: State, change: Change) -> State:
    """Execute one change; the state on disk matches the project when it returns or raises."""
    op, effect = change
    if op.locator is not None:
        if effect in ("create", "replace"):
            return _write_entry(project, state, change)
        if effect == "adopt":
            return _adopt_entry(project, state, change)
        return _remove_entry(project, state, change)
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
