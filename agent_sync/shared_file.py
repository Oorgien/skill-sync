"""Read, edit and serialise the JSON and TOML files that agent-sync merges entries into.

JSON is parsed with the standard library (key order kept, 2-space indent on write). TOML goes
through ``tomlkit`` so comments and formatting outside the edited entries survive byte for byte.
Nothing here touches the disk except :func:`load_doc` (read) and :func:`write_atomic`.
"""

from __future__ import annotations

import copy
import json
import math
import os
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, MutableMapping, MutableSequence, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import tomlkit
import tomlkit.exceptions

from agent_sync.state import fingerprint_entry


class Lookup(NamedTuple):
    present: bool
    value: Any = None
    blocked_at: str | None = None  # a path part whose value is not a table/list we can enter


class Doc:
    """One parsed file. ``exists`` is False for a file that is not on disk yet."""

    def __init__(self, fmt: str, root: Any, exists: bool) -> None:
        self.fmt = fmt
        self.root = root
        self.exists = exists

    def dumps(self) -> bytes:
        if self.fmt == "json":
            text = json.dumps(self.root, indent=2, ensure_ascii=False, allow_nan=False)
            return (text + "\n").encode("utf-8")
        return tomlkit.dumps(self.root).encode("utf-8")

    def is_empty(self) -> bool:
        if self.fmt == "json":
            return not self.root
        return len(self.root) == 0 and not tomlkit.dumps(self.root).strip()


def format_of(rel: str) -> str:
    suffix = Path(rel).suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix == ".toml":
        return "toml"
    raise ValueError(f"{rel}: only .json and .toml files can hold merged entries")


def load_doc(project: Path, rel: str) -> Doc:
    """Parse the file; a missing file is an empty document. Invalid content is a ValueError."""
    fmt = format_of(rel)
    path = project / rel
    if not os.path.lexists(path):
        return Doc(fmt, {} if fmt == "json" else tomlkit.document(), False)
    if path.is_symlink():
        raise ValueError(f"{rel} is a symlink; agent-sync will not edit it")
    if not path.is_file():
        raise ValueError(f"{rel} exists and is not a file")
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{rel}: not valid UTF-8 ({exc}); fix the file") from exc
    return Doc(fmt, parse_text(fmt, text, rel), True)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise ValueError(f"duplicate key {key!r}")
        found[key] = value
    return found


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def parse_text(fmt: str, text: str, rel: str) -> Any:
    """Parse JSON (no duplicate keys, no NaN/Infinity) or TOML; errors name the file."""
    if fmt == "json":
        try:
            root = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
        except ValueError as exc:
            raise ValueError(f"{rel}: not valid JSON ({exc}); fix the file") from exc
        if not isinstance(root, dict):
            raise ValueError(f"{rel}: the top level must be a JSON object; fix the file")
        return root
    try:
        return tomlkit.parse(text)
    except tomlkit.exceptions.TOMLKitError as exc:
        raise ValueError(f"{rel}: not valid TOML ({exc}); fix the file") from exc


def same_data(a: Any, b: Any) -> bool:
    """Equality of plain data where NaN equals NaN (TOML allows ``nan`` values)."""
    if isinstance(a, float) and isinstance(b, float):
        return a == b or (math.isnan(a) and math.isnan(b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same_data(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_data(x, y) for x, y in zip(a, b))
    return bool(a == b) and type(a) is type(b)


def _comment_lines(text: str) -> Counter[str]:
    return Counter(line.strip() for line in text.splitlines() if line.lstrip().startswith("#"))


def payload_problem(fmt: str, payload: Any) -> str | None:
    """Why ``payload`` cannot be written to a file of this format, or None."""
    value = plain(payload)
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        return str(exc)
    if fmt == "toml" and _has_null(value):
        return "TOML has no null"
    return None


def _has_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_has_null(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_null(v) for v in value)
    return False


def apply_edit(
    doc: Doc, rel: str, mutate: Callable[[Doc], Any], own: str = ""
) -> tuple[bytes, Any]:
    """Run ``mutate`` on the document and return the new bytes and what ``mutate`` returned.

    The edit is also made on a plain-data copy; the new text is parsed again and must hold
    exactly that data, so an edit that would disturb anything else (a TOML dotted key, say)
    raises ``ValueError`` before anything is written. ``mutate`` must make the same decisions on
    both documents. Every whole-line comment of the old text must survive too, except those in
    ``own`` (the text of the entry being replaced or removed).
    """
    shadow = Doc("json", plain(doc.root), doc.exists)
    before = doc.dumps().decode("utf-8") if doc.fmt == "toml" else ""
    result = mutate(doc)
    mutate(shadow)
    try:
        data = doc.dumps()
        again = parse_text(doc.fmt, data.decode("utf-8"), rel)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{rel}: cannot be edited safely ({exc})") from exc
    if not same_data(plain(again), shadow.root):
        raise ValueError(f"{rel}: cannot be edited safely: the edit would change other entries")
    if doc.fmt == "toml":
        lost = _comment_lines(before) - _comment_lines(own) - _comment_lines(data.decode("utf-8"))
        if lost:
            raise ValueError(
                f"{rel}: cannot be edited safely: the edit would drop the comment {next(iter(lost))!r}"
            )
    return data, result


def write_atomic(path: Path, data: bytes) -> None:
    """Write next to the target, then ``os.replace``; an existing file keeps its permissions."""
    tmp = path.with_name(f".{path.name}.agent-sync-{uuid.uuid4().hex[:12]}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def plain(value: Any) -> Any:
    """The value as plain Python data (tomlkit items unwrapped), for fingerprints and JSON."""
    if hasattr(value, "unwrap"):
        return value.unwrap()
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def fingerprint(value: Any) -> str:
    return fingerprint_entry(plain(value))


# --- navigation -----------------------------------------------------------------------------


def lookup(root: Any, path: Sequence[str]) -> Lookup:
    current = root
    for key in path:
        if not isinstance(current, Mapping):
            return Lookup(False, None, key)
        if key not in current:
            return Lookup(False)
        current = current[key]
    return Lookup(True, current)


def _is_toml(doc: Doc) -> bool:
    return doc.fmt == "toml"


def _toml_value(payload: Any) -> Any:
    if isinstance(payload, (tomlkit.items.Item, tomlkit.container.Container)):
        return copy.deepcopy(payload)
    return tomlkit.item(plain(payload))


def _make_parents(
    doc: Doc, path: Sequence[str], *, super_last: bool, created: list[tuple[str, ...]]
) -> Any:
    """Walk to ``path`` creating missing tables (appended to ``created``); return the last."""
    current = doc.root
    for i, key in enumerate(path):
        if key not in current:
            created.append(tuple(path[: i + 1]))
            if _is_toml(doc):
                last = i == len(path) - 1
                current[key] = tomlkit.table(is_super_table=super_last or not last)
            else:
                current[key] = {}
        current = current[key]
        if not isinstance(current, MutableMapping):
            raise TypeError(f"{'.'.join(path[: i + 1])} is not a table")
    return current


def set_key(doc: Doc, locator: Sequence[str], payload: Any, created: list[tuple[str, ...]]) -> None:
    parent = _make_parents(doc, locator[:-1], super_last=True, created=created)
    parent[locator[-1]] = _toml_value(payload) if _is_toml(doc) else copy.deepcopy(plain(payload))


def delete_key(doc: Doc, locator: Sequence[str]) -> None:
    found = lookup(doc.root, locator[:-1])
    if found.present and locator[-1] in found.value:
        del found.value[locator[-1]]


def find_index(items: Sequence[Any], sha: str) -> int | None:
    for index, element in enumerate(items):
        if fingerprint(element) == sha:
            return index
    return None


def get_list(doc: Doc, locator: Sequence[str]) -> Lookup:
    """The list that holds a list entry (``locator[:-1]``); ``blocked_at`` set if not a list."""
    found = lookup(doc.root, locator[:-1])
    if found.present and not isinstance(found.value, MutableSequence):
        return Lookup(False, None, locator[-2])
    return found


def append_element(
    doc: Doc, locator: Sequence[str], payload: Any, created: list[tuple[str, ...]]
) -> None:
    holder = _make_parents(doc, locator[:-2], super_last=False, created=created)
    if locator[-2] not in holder:
        created.append(tuple(locator[:-1]))
        holder[locator[-2]] = []
    holder[locator[-2]].append(copy.deepcopy(plain(payload)))


def replace_element(doc: Doc, locator: Sequence[str], index: int, payload: Any) -> None:
    doc_list = lookup(doc.root, locator[:-1]).value
    doc_list[index] = copy.deepcopy(plain(payload))


def delete_element(doc: Doc, locator: Sequence[str], index: int) -> None:
    del lookup(doc.root, locator[:-1]).value[index]


def has_comment(container: Any) -> bool:
    """True if a TOML table or array still carries a comment of the user's."""
    as_string = getattr(container, "as_string", None)
    return as_string is not None and "#" in as_string()


def prune_empty(
    doc: Doc, path: Sequence[str], may_delete: Callable[[tuple[str, ...], Any], bool]
) -> list[tuple[str, ...]]:
    """Delete empty containers on ``path`` from the deepest up, as far as ``may_delete`` agrees.

    Never the document root. Returns the paths that were deleted.
    """
    parts = list(path)
    deleted: list[tuple[str, ...]] = []
    while parts:
        found = lookup(doc.root, parts)
        if not found.present or not isinstance(found.value, (Mapping, MutableSequence)):
            break
        if len(found.value) != 0 or not may_delete(tuple(parts), found.value):
            break
        parent = lookup(doc.root, parts[:-1]).value
        del parent[parts[-1]]
        deleted.append(tuple(parts))
        parts.pop()
    return deleted


# --- keeping the comments that follow a TOML table ------------------------------------------


class Tail(NamedTuple):
    items: list[Any]  # the comment and blank-line items tomlkit attached to the end of the table
    slots: list[int]  # per path level: where that key sits in its container's body


def _container(node: Any) -> Any:
    return node.value if isinstance(node, tomlkit.items.Table) else node


def _is_trivia(entry: tuple[Any, Any]) -> bool:
    key, item = entry
    return key is None and isinstance(item, (tomlkit.items.Comment, tomlkit.items.Whitespace))


def _tail_items(table: Any) -> list[Any]:
    body = table.value.body
    n = 0
    while n < len(body) and _is_trivia(body[len(body) - 1 - n]):
        n += 1
    return [item for _, item in body[len(body) - n :]]


def capture_tail(doc: Doc, locator: Sequence[str]) -> Tail | None:
    """The trailing comments of the TOML table at ``locator``: tomlkit puts a comment that
    follows the last table of a group into that table's body, so deleting or replacing the
    table would delete the user's comment with it."""
    if doc.fmt != "toml":
        return None
    found = lookup(doc.root, locator)
    if not found.present or not isinstance(found.value, tomlkit.items.Table):
        return None
    items = _tail_items(found.value)
    if not items:
        return None
    slots: list[int] = []
    for i, key in enumerate(locator):
        body = _container(lookup(doc.root, locator[:i]).value).body
        slot = next((n for n, (k, _) in enumerate(body) if k is not None and k.key == key), None)
        if slot is None:
            return None
        slots.append(slot)
    return Tail(items, slots)


def entry_text(doc: Doc, locator: Sequence[str]) -> str:
    """The TOML text of the entry itself, without the comments that follow it."""
    if doc.fmt != "toml":
        return ""
    found = lookup(doc.root, locator)
    if not found.present or not hasattr(found.value, "as_string"):
        return ""
    whole: str = found.value.as_string()
    if isinstance(found.value, tomlkit.items.Table):
        tail = "".join(item.as_string() for item in _tail_items(found.value))
        if tail and whole.endswith(tail):
            return whole[: len(whole) - len(tail)]
    return whole


def restore_tail(doc: Doc, locator: Sequence[str], tail: Tail | None) -> None:
    """Put captured trailing comments back where the table was (or right after its replacement)."""
    if tail is None:
        return
    if lookup(doc.root, locator).present:
        level, offset = len(locator) - 1, 1
    else:
        level, offset = 0, 0
        for i in range(len(locator) - 1, 0, -1):
            if lookup(doc.root, locator[:i]).present:
                level = i
                break
    container = _container(lookup(doc.root, locator[:level]).value)
    index = min(tail.slots[level] + offset, len(container.body))
    for item in tail.items:
        _insert_trivia(container, index, item)
        index += 1


def _insert_trivia(container: Any, index: int, item: Any) -> None:
    for key, value in container._map.items():
        if isinstance(value, tuple):
            container._map[key] = tuple(v + 1 if v >= index else v for v in value)
        elif value >= index:
            container._map[key] = value + 1
    container._body.insert(index, (None, item))
