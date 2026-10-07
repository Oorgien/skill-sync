"""Ownership state: what agent-sync wrote, how to tell if it was edited, and the project lock.

The state lives in ``.agents/agent-sync.state.json`` (version 1). Each item records a target and
the fingerprint of what agent-sync wrote there; comparing it with the current fingerprint tells
whether the user edited it. POSIX only: the project lock uses ``fcntl.flock``.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple

STATE_FILE = Path(".agents/agent-sync.state.json")
LOCK_FILE = Path(".agents/agent-sync.lock")
STATE_VERSION = 1

Locator = tuple[str, ...]
"""Key path inside a shared JSON/TOML file."""


class Item(NamedTuple):
    """One thing agent-sync owns: a whole path (``locator`` None) or an entry inside a file."""

    kind: str
    harness: str | None
    target: str
    locator: Locator | None
    source: str
    sha256: str


class State(NamedTuple):
    items: tuple[Item, ...]

    def find(self, target: str, locator: Locator | None = None) -> Item | None:
        for item in self.items:
            if item.target == target and item.locator == locator:
                return item
        return None

    def with_item(self, item: Item) -> State:
        """Add the item, replacing any record with the same target and locator."""
        kept = self.without(item.target, item.locator).items
        return State((*kept, item))

    def without(self, target: str, locator: Locator | None = None) -> State:
        return State(
            tuple(i for i in self.items if not (i.target == target and i.locator == locator))
        )


# --- load / save ----------------------------------------------------------------------------


def _sort_key(item: Item) -> tuple[str, bool, Locator]:
    return (item.target, item.locator is not None, item.locator or ())


def _item_to_json(item: Item) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "harness": item.harness,
        "target": item.target,
        "locator": None if item.locator is None else list(item.locator),
        "source": item.source,
        "sha256": item.sha256,
    }


def _item_from_json(raw: Any, path: Path) -> Item:
    if not isinstance(raw, dict):
        raise ValueError(  # noqa: TRY004
            f"{path}: every item must be an object; delete the file to start over"
        )
    try:
        kind, harness, target = raw["kind"], raw["harness"], raw["target"]
        locator, source, sha256 = raw["locator"], raw["source"], raw["sha256"]
    except KeyError as exc:
        raise ValueError(f"{path}: item is missing {exc}; delete the file to start over") from exc
    ok = (
        isinstance(kind, str)
        and (harness is None or isinstance(harness, str))
        and isinstance(target, str)
        and (
            locator is None
            or (isinstance(locator, list) and all(isinstance(k, str) for k in locator))
        )
        and isinstance(source, str)
        and isinstance(sha256, str)
    )
    if not ok:
        raise ValueError(f"{path}: item has a field of the wrong type; delete the file")
    return Item(kind, harness, target, None if locator is None else tuple(locator), source, sha256)


def load_state(project: Path) -> State:
    """Read the state file; a missing file is an empty state."""
    path = project / STATE_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return State(())
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path}: not valid JSON ({exc}); fix or delete the file") from exc
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(data, dict) or type(version) is not int or version != STATE_VERSION:
        found = version
        raise ValueError(
            f"{path}: unsupported state version {found!r}, expected {STATE_VERSION}; "
            "use a matching agent-sync or delete the file"
        )
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError(f"{path}: 'items' must be a list; fix or delete the file")  # noqa: TRY004
    loaded = tuple(_item_from_json(raw, path) for raw in items)
    seen: set[tuple[str, Locator | None]] = set()
    for item in loaded:
        key = (item.target, item.locator)
        if key in seen:
            where = f" {list(item.locator)}" if item.locator is not None else ""
            raise ValueError(
                f"{path}: duplicate item for {item.target!r}{where}; fix or delete the file"
            )
        seen.add(key)
    return State(loaded)


def save_state(project: Path, state: State) -> None:
    """Write the state atomically: temp file in the same folder, then ``os.replace``."""
    path = project / STATE_FILE
    document = {
        "version": STATE_VERSION,
        "items": [_item_to_json(i) for i in sorted(state.items, key=_sort_key)],
    }
    # Serialise first so a failure here never touches the disk.
    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


# --- fingerprints ---------------------------------------------------------------------------


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _walk(root: Path, rel: str = "") -> Iterator[tuple[str, Path]]:
    """Yield (relative POSIX path, path) for everything below root, without following links."""
    with os.scandir(root) as it:
        children = sorted(it, key=lambda e: e.name)
    for child in children:
        child_rel = f"{rel}/{child.name}" if rel else child.name
        child_path = Path(child.path)
        yield child_rel, child_path
        if not child.is_symlink() and child.is_dir(follow_symlinks=False):
            yield from _walk(child_path, child_rel)


def _record(rel: str, marker: bytes, payload: bytes) -> bytes:
    name = rel.encode("utf-8", "surrogateescape")
    return b"%d:%s%s%d:%s" % (len(name), name, marker, len(payload), payload)


def fingerprint_link(text: str) -> str:
    """Fingerprint of a symlink whose text is ``text``, without creating it."""
    return _sha(b"link:" + os.fsencode(text))


def fingerprint_bytes(data: bytes) -> str:
    """Fingerprint of a file holding ``data``, without creating it."""
    return _sha(b"file:" + data)


def _tree_digest(records: Iterable[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256(b"dir:")
    for _, record in sorted(records, key=lambda r: r[0]):
        digest.update(record)
    return digest.hexdigest()


def fingerprint_tree(files: Mapping[str, bytes]) -> str:
    """Fingerprint of a folder holding ``files`` (relative POSIX path -> bytes).

    Folders implied by the paths count as ``dir`` records, as in the walk of a real folder.
    """
    records: dict[str, bytes] = {}
    for rel, data in files.items():
        parts = rel.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            records[parent] = _record(parent, b"D", b"")
        records[rel] = _record(rel, b"F", data)
    return _tree_digest(records.items())


def fingerprint_path(path: Path) -> str | None:
    """Fingerprint a link, file or folder tree; None if nothing is there.

    A symlink is checked first, so a dangling link still has a fingerprint (of its text).
    """
    if path.is_symlink():
        return fingerprint_link(os.readlink(path))
    if path.is_file():
        return fingerprint_bytes(path.read_bytes())
    if path.is_dir():
        records: list[tuple[str, bytes]] = []
        for rel, child in _walk(path):
            if child.is_symlink():
                record = _record(rel, b"L", os.fsencode(os.readlink(child)))
            elif child.is_dir():
                record = _record(rel, b"D", b"")
            elif child.is_file():
                record = _record(rel, b"F", child.read_bytes())
            else:
                record = _record(rel, b"O", b"")
            records.append((rel, record))
        return _tree_digest(records)
    if path.exists():  # sockets, fifos and the like
        return _sha(b"other:")
    return None


def fingerprint_entry(value: Any) -> str:
    """Fingerprint an entry of a shared file from its canonical JSON."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha(b"entry:" + canonical.encode("utf-8"))


# --- classification -------------------------------------------------------------------------


class Status(Enum):
    ABSENT = "absent"
    OWNED_UNCHANGED = "owned-unchanged"
    OWNED_EDITED = "owned-edited"
    NOT_OURS = "not-ours"


def _compare(item: Item | None, current: str) -> Status:
    if item is None:
        return Status.NOT_OURS
    return Status.OWNED_UNCHANGED if item.sha256 == current else Status.OWNED_EDITED


def classify_path(project: Path, state: State, target: str) -> Status:
    """Classify a whole path (``locator`` None) in the project."""
    current = fingerprint_path(project / target)
    if current is None:
        return Status.ABSENT
    item = state.find(target)
    if item is not None and item.kind in ("dir", "file"):
        item = None  # a folder or file agent-sync created says nothing about what is inside it
    return _compare(item, current)


def classify_entry(
    state: State, target: str, locator: Locator, current: Any | None, present: bool
) -> Status:
    """Classify an entry inside a shared file; the caller reads ``current`` and ``present``."""
    if not present:
        return Status.ABSENT
    return _compare(state.find(target, locator), fingerprint_entry(current))


# --- lock -----------------------------------------------------------------------------------


class LockError(RuntimeError):
    """Another agent-sync run holds the project lock."""


@contextmanager
def project_lock(project: Path) -> Iterator[None]:
    """Hold an exclusive, non-blocking ``flock`` on the project's lock file."""
    path = project / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"{path} is a symlink; agent-sync will not follow it") from exc
        raise
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockError(f"another agent-sync run is in progress ({path})") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
