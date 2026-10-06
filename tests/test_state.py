"""Ownership state: load/save, fingerprints, classification and the project lock."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_sync.state import (
    LOCK_FILE,
    STATE_FILE,
    Item,
    LockError,
    State,
    Status,
    classify_entry,
    classify_path,
    fingerprint_bytes,
    fingerprint_entry,
    fingerprint_link,
    fingerprint_path,
    fingerprint_tree,
    load_state,
    project_lock,
    save_state,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_item(
    target: str = ".agents/skills/foo",
    locator: tuple[str, ...] | None = None,
    sha256: str = "abc",
    kind: str = "skills",
    harness: str | None = None,
    source: str = "github.com:o/r:foo",
) -> Item:
    return Item(kind, harness, target, locator, source, sha256)


# --- State value object ---------------------------------------------------------------------


def test_find_matches_target_and_locator() -> None:
    whole = make_item("a")
    entry = make_item("b.json", ("mcpServers", "x"))
    state = State((whole, entry))
    assert state.find("a") == whole
    assert state.find("b.json", ("mcpServers", "x")) == entry
    assert state.find("b.json") is None
    assert state.find("a", ("k",)) is None
    assert state.find("missing") is None


def test_with_item_adds_then_replaces_by_target_and_locator() -> None:
    first = make_item("a", sha256="1")
    other = make_item("a", ("k",), sha256="2")
    state = State(()).with_item(first).with_item(other)
    assert len(state.items) == 2
    replaced = state.with_item(make_item("a", sha256="3"))
    assert len(replaced.items) == 2
    found = replaced.find("a")
    assert found is not None and found.sha256 == "3"
    assert replaced.find("a", ("k",)) == other


def test_without_removes_only_the_matching_record() -> None:
    state = State((make_item("a"), make_item("a", ("k",)), make_item("b")))
    assert state.without("a").find("a") is None
    assert state.without("a").find("a", ("k",)) is not None
    assert state.without("a", ("k",)).find("a") is not None
    assert state.without("nope") == state


# --- load / save ----------------------------------------------------------------------------


def test_missing_state_file_loads_empty(tmp_path: Path) -> None:
    assert load_state(tmp_path) == State(())


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    state = State(
        (
            make_item(".agents/skills/foo", sha256="1"),
            make_item(
                ".codex/agents/a.toml", kind="agents", harness="codex", sha256="2", source="p:a"
            ),
            make_item(".mcp.json", ("mcpServers", "srv"), kind="mcp", harness="claude"),
        )
    )
    save_state(tmp_path, state)
    loaded = load_state(tmp_path)
    assert set(loaded.items) == set(state.items)
    assert all(isinstance(i.locator, (tuple, type(None))) for i in loaded.items)


def test_save_creates_agents_dir_and_leaves_no_temp_files(tmp_path: Path) -> None:
    save_state(tmp_path, State((make_item(),)))
    assert (tmp_path / STATE_FILE).is_file()
    assert [p.name for p in (tmp_path / ".agents").iterdir()] == [STATE_FILE.name]


def test_save_output_is_stable_and_sorted(tmp_path: Path) -> None:
    a = make_item("a/z", sha256="1")
    b = make_item("a/b", ("k", "j"), sha256="2")
    c = make_item("a/b", None, sha256="3")
    save_state(tmp_path, State((a, b, c)))
    first = (tmp_path / STATE_FILE).read_text(encoding="utf-8")
    save_state(tmp_path, State((c, a, b)))
    assert (tmp_path / STATE_FILE).read_text(encoding="utf-8") == first

    assert first.endswith("}\n") and not first.endswith("\n\n")
    data = json.loads(first)
    assert data["version"] == 1
    assert [(i["target"], i["locator"]) for i in data["items"]] == [
        ("a/b", None),
        ("a/b", ["k", "j"]),
        ("a/z", None),
    ]
    assert list(data["items"][0]) == sorted(data["items"][0])
    assert '\n  "items"' in first  # 2-space indent


@pytest.mark.parametrize(
    "content, fragment",
    [
        ("{not json", "agent-sync.state.json"),
        ('{"version": 2, "items": []}', "agent-sync.state.json"),
        ('{"items": []}', "agent-sync.state.json"),
        ("[]", "agent-sync.state.json"),
        ('{"version": 1, "items": [{"kind": "skills"}]}', "agent-sync.state.json"),
        ('{"version": 1, "items": "x"}', "agent-sync.state.json"),
        ('{"version": true, "items": []}', "agent-sync.state.json"),
        ('{"version": 1.0, "items": []}', "agent-sync.state.json"),
    ],
)
def test_load_rejects_bad_files_naming_the_file(
    tmp_path: Path, content: str, fragment: str
) -> None:
    path = tmp_path / STATE_FILE
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=fragment):
        load_state(tmp_path)


def test_load_rejects_duplicate_target_and_locator_naming_the_file(tmp_path: Path) -> None:
    item = {
        "kind": "skills",
        "harness": None,
        "target": "a",
        "locator": None,
        "source": "s",
        "sha256": "1",
    }
    path = tmp_path / STATE_FILE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "items": [item, {**item, "sha256": "2"}]}))
    with pytest.raises(ValueError, match=r"agent-sync\.state\.json.*duplicate.*'a'"):
        load_state(tmp_path)


def test_same_target_with_different_locators_is_not_a_duplicate(tmp_path: Path) -> None:
    save_state(tmp_path, State((make_item("f.json", None), make_item("f.json", ("k",)))))
    assert len(load_state(tmp_path).items) == 2


def test_unknown_version_message_mentions_version(tmp_path: Path) -> None:
    path = tmp_path / STATE_FILE
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 99, "items": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        load_state(tmp_path)


def test_failed_serialisation_keeps_existing_file(tmp_path: Path) -> None:
    save_state(tmp_path, State((make_item(sha256="good"),)))
    before = (tmp_path / STATE_FILE).read_bytes()
    bad: Any = make_item(sha256="x")._replace(locator=(object(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        save_state(tmp_path, State((bad,)))
    assert (tmp_path / STATE_FILE).read_bytes() == before
    assert [p.name for p in (tmp_path / ".agents").iterdir()] == [STATE_FILE.name]


def test_failed_replace_keeps_existing_file_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_state(tmp_path, State((make_item(sha256="good"),)))
    before = (tmp_path / STATE_FILE).read_bytes()

    def boom(src: Any, dst: Any) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk on fire"):
        save_state(tmp_path, State((make_item(sha256="new"),)))
    assert (tmp_path / STATE_FILE).read_bytes() == before
    assert [p.name for p in (tmp_path / ".agents").iterdir()] == [STATE_FILE.name]


def test_save_replaces_via_os_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    real = os.replace

    def spy(src: Any, dst: Any) -> None:
        calls.append((str(src), str(dst)))
        real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    save_state(tmp_path, State((make_item(),)))
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == str(tmp_path / STATE_FILE)
    assert os.path.dirname(src) == os.path.dirname(dst)


# --- fingerprints ---------------------------------------------------------------------------


def test_file_fingerprint_is_prefixed_sha_of_bytes(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_bytes(b"hello")
    assert fingerprint_path(f) == hashlib.sha256(b"file:hello").hexdigest()


def test_same_bytes_in_different_files_match_and_change_differs(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"x")
    (tmp_path / "b").write_bytes(b"x")
    (tmp_path / "c").write_bytes(b"y")
    assert fingerprint_path(tmp_path / "a") == fingerprint_path(tmp_path / "b")
    assert fingerprint_path(tmp_path / "a") != fingerprint_path(tmp_path / "c")


def test_absent_path_has_no_fingerprint(tmp_path: Path) -> None:
    assert fingerprint_path(tmp_path / "nope") is None


def test_symlink_fingerprint_is_link_text(tmp_path: Path) -> None:
    (tmp_path / "t1").write_bytes(b"same")
    (tmp_path / "t2").write_bytes(b"same")
    (tmp_path / "l1").symlink_to("t1")
    (tmp_path / "l2").symlink_to("t2")
    (tmp_path / "l1b").symlink_to("t1")
    assert fingerprint_path(tmp_path / "l1") == hashlib.sha256(b"link:t1").hexdigest()
    assert fingerprint_path(tmp_path / "l1") == fingerprint_path(tmp_path / "l1b")
    assert fingerprint_path(tmp_path / "l1") != fingerprint_path(tmp_path / "l2")
    assert fingerprint_path(tmp_path / "l1") != fingerprint_path(tmp_path / "t1")


def test_dangling_symlink_is_fingerprinted(tmp_path: Path) -> None:
    (tmp_path / "dead").symlink_to("gone")
    assert fingerprint_path(tmp_path / "dead") == hashlib.sha256(b"link:gone").hexdigest()


def test_symlink_to_directory_is_not_followed(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_bytes(b"1")
    (tmp_path / "l").symlink_to("d")
    assert fingerprint_path(tmp_path / "l") == hashlib.sha256(b"link:d").hexdigest()


def _make_tree(root: Path, order: list[str]) -> None:
    root.mkdir()
    for name in order:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())


def test_directory_fingerprint_is_independent_of_creation_order(tmp_path: Path) -> None:
    _make_tree(tmp_path / "a", ["x.md", "sub/y.md", "z.md"])
    _make_tree(tmp_path / "b", ["z.md", "sub/y.md", "x.md"])
    assert fingerprint_path(tmp_path / "a") == fingerprint_path(tmp_path / "b")


def test_directory_fingerprint_detects_changes(tmp_path: Path) -> None:
    _make_tree(tmp_path / "base", ["x.md", "sub/y.md"])
    base = fingerprint_path(tmp_path / "base")
    assert base is not None

    _make_tree(tmp_path / "nested", ["x.md", "sub/y.md"])
    (tmp_path / "nested" / "sub" / "y.md").write_bytes(b"changed")
    assert fingerprint_path(tmp_path / "nested") != base

    _make_tree(tmp_path / "renamed", ["x.md", "sub/w.md"])
    (tmp_path / "renamed" / "sub" / "w.md").write_bytes(b"sub/y.md")
    assert fingerprint_path(tmp_path / "renamed") != base

    _make_tree(tmp_path / "emptydir", ["x.md", "sub/y.md"])
    (tmp_path / "emptydir" / "empty").mkdir()
    assert fingerprint_path(tmp_path / "emptydir") != base

    _make_tree(tmp_path / "extra", ["x.md", "sub/y.md", "more.md"])
    assert fingerprint_path(tmp_path / "extra") != base


def test_empty_directory_is_fingerprinted_and_differs_from_file(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "f").write_bytes(b"")
    assert fingerprint_path(tmp_path / "d") is not None
    assert fingerprint_path(tmp_path / "d") != fingerprint_path(tmp_path / "f")


def test_directory_fingerprint_distinguishes_file_link_and_dir_entries(tmp_path: Path) -> None:
    for name in ("asfile", "aslink", "asdir"):
        (tmp_path / name).mkdir()
    (tmp_path / "asfile" / "e").write_bytes(b"t")
    (tmp_path / "aslink" / "e").symlink_to("t")
    (tmp_path / "asdir" / "e").mkdir()
    prints = {fingerprint_path(tmp_path / n) for n in ("asfile", "aslink", "asdir")}
    assert len(prints) == 3


def test_directory_fingerprint_does_not_follow_links_inside(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_bytes(b"1")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "l").symlink_to(outside)
    before = fingerprint_path(tmp_path / "d")
    (outside / "f").write_bytes(b"2")
    assert fingerprint_path(tmp_path / "d") == before
    (tmp_path / "d" / "dead").symlink_to("nowhere")
    assert fingerprint_path(tmp_path / "d") != before


# --- pure fingerprints: what fingerprint_path would return once materialised ----------------


def test_fingerprint_bytes_equals_fingerprint_path_of_a_file(tmp_path: Path) -> None:
    for name, data in (("empty", b""), ("text", b"hello\n"), ("bin", bytes(range(256)))):
        (tmp_path / name).write_bytes(data)
        assert fingerprint_bytes(data) == fingerprint_path(tmp_path / name)


def test_fingerprint_link_equals_fingerprint_path_of_a_link(tmp_path: Path) -> None:
    for i, text in enumerate(("t1", "../.agents/skills", "/abs/dead", "ünï/çode")):
        (tmp_path / f"l{i}").symlink_to(text)
        assert fingerprint_link(text) == fingerprint_path(tmp_path / f"l{i}")


def test_fingerprint_tree_equals_fingerprint_path_of_a_tree(tmp_path: Path) -> None:
    trees = {
        "flat": {"x.md": b"x", "z.md": b"z"},
        "nested": {"x.md": b"x", "sub/y.md": b"y", "sub/deep/w.md": b"w", "a-b": b"", "a.b": b"1"},
        "empty": {},
    }
    for name, files in trees.items():
        root = tmp_path / name
        root.mkdir()
        for rel, data in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(data)
        assert fingerprint_tree(files) == fingerprint_path(root), name


def test_fingerprint_tree_ignores_key_order_and_detects_changes() -> None:
    base = fingerprint_tree({"a": b"1", "d/b": b"2"})
    assert fingerprint_tree({"d/b": b"2", "a": b"1"}) == base
    assert fingerprint_tree({"a": b"1", "d/b": b"3"}) != base
    assert fingerprint_tree({"a": b"1", "d/c": b"2"}) != base
    assert fingerprint_tree({"a": b"1"}) != base


def test_pure_fingerprints_of_different_shapes_differ() -> None:
    assert len({fingerprint_link("x"), fingerprint_bytes(b"x"), fingerprint_tree({"x": b""})}) == 3


def test_entry_fingerprint_is_canonical_json() -> None:
    expected = hashlib.sha256(b'entry:{"a":1,"b":[1,2]}').hexdigest()
    assert fingerprint_entry({"b": [1, 2], "a": 1}) == expected
    assert fingerprint_entry({"a": 1, "b": [1, 2]}) == expected
    assert fingerprint_entry({"a": 1, "b": [2, 1]}) != expected
    assert fingerprint_entry("é") == hashlib.sha256('entry:"é"'.encode()).hexdigest()


# --- classification -------------------------------------------------------------------------


def _owned(project: Path, target: str) -> State:
    fp = fingerprint_path(project / target)
    assert fp is not None
    return State((make_item(target, sha256=fp),))


def test_classify_path_file_matrix(tmp_path: Path) -> None:
    target = ".codex/agents/a.toml"
    assert classify_path(tmp_path, State(()), target) is Status.ABSENT

    path = tmp_path / target
    path.parent.mkdir(parents=True)
    path.write_bytes(b"v1")
    assert classify_path(tmp_path, State(()), target) is Status.NOT_OURS

    state = _owned(tmp_path, target)
    assert classify_path(tmp_path, state, target) is Status.OWNED_UNCHANGED
    path.write_bytes(b"v2")
    assert classify_path(tmp_path, state, target) is Status.OWNED_EDITED
    path.unlink()
    assert classify_path(tmp_path, state, target) is Status.ABSENT  # stale record


def test_classify_path_directory_tree(tmp_path: Path) -> None:
    target = ".agents/skills/foo"
    tree = tmp_path / target
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f.md").write_bytes(b"1")
    assert classify_path(tmp_path, State(()), target) is Status.NOT_OURS
    state = _owned(tmp_path, target)
    assert classify_path(tmp_path, state, target) is Status.OWNED_UNCHANGED
    (tree / "sub" / "f.md").write_bytes(b"2")
    assert classify_path(tmp_path, state, target) is Status.OWNED_EDITED
    (tree / "sub" / "f.md").write_bytes(b"1")
    assert classify_path(tmp_path, state, target) is Status.OWNED_UNCHANGED
    (tree / "extra").mkdir()
    assert classify_path(tmp_path, state, target) is Status.OWNED_EDITED


def test_classify_path_symlink_including_dangling(tmp_path: Path) -> None:
    target = ".agents/skills/foo"
    link = tmp_path / target
    link.parent.mkdir(parents=True)
    link.symlink_to("/cache/foo")  # dangling
    assert classify_path(tmp_path, State(()), target) is Status.NOT_OURS
    state = _owned(tmp_path, target)
    assert classify_path(tmp_path, state, target) is Status.OWNED_UNCHANGED
    link.unlink()
    link.symlink_to("/cache/other")
    assert classify_path(tmp_path, state, target) is Status.OWNED_EDITED
    link.unlink()
    assert classify_path(tmp_path, state, target) is Status.ABSENT


def test_classify_path_ignores_dir_records(tmp_path: Path) -> None:
    # A "dir" record says agent-sync made the folder, not that it owns what is inside.
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "mine").write_bytes(b"user")
    state = State((Item("dir", None, "out", None, "", ""),))
    assert classify_path(tmp_path, state, "out") is Status.NOT_OURS
    (tmp_path / "out" / "mine").unlink()
    (tmp_path / "out").rmdir()
    assert classify_path(tmp_path, state, "out") is Status.ABSENT


def test_classify_path_ignores_entry_records_for_same_target(tmp_path: Path) -> None:
    (tmp_path / "shared.json").write_bytes(b"{}")
    state = State((make_item("shared.json", ("k",), sha256="x"),))
    assert classify_path(tmp_path, state, "shared.json") is Status.NOT_OURS


def test_classify_entry_matrix() -> None:
    loc = ("mcpServers", "srv")
    value = {"command": "npx", "args": ["a"]}
    empty = State(())
    assert classify_entry(empty, "m.json", loc, None, False) is Status.ABSENT
    assert classify_entry(empty, "m.json", loc, value, True) is Status.NOT_OURS

    state = State((make_item("m.json", loc, sha256=fingerprint_entry(value)),))
    assert classify_entry(state, "m.json", loc, {"args": ["a"], "command": "npx"}, True) is (
        Status.OWNED_UNCHANGED
    )
    assert classify_entry(state, "m.json", loc, {"command": "other"}, True) is Status.OWNED_EDITED
    assert classify_entry(state, "m.json", loc, None, False) is Status.ABSENT  # stale record
    assert classify_entry(state, "m.json", ("mcpServers", "x"), value, True) is Status.NOT_OURS
    assert classify_entry(state, "n.json", loc, value, True) is Status.NOT_OURS


def test_classify_entry_present_with_null_value() -> None:
    loc = ("k",)
    state = State((make_item("m.json", loc, sha256=fingerprint_entry(None)),))
    assert classify_entry(state, "m.json", loc, None, True) is Status.OWNED_UNCHANGED
    assert classify_entry(state, "m.json", loc, None, False) is Status.ABSENT


# --- lock -----------------------------------------------------------------------------------

CHILD = """
import sys
from pathlib import Path
from agent_sync.state import LockError, project_lock
try:
    with project_lock(Path(sys.argv[1])):
        print("acquired")
except LockError as exc:
    print("LockError:", exc)
    sys.exit(3)
"""


def run_child(project: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run(
        [sys.executable, "-c", CHILD, str(project)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def test_lock_creates_agents_dir_and_lock_file(tmp_path: Path) -> None:
    with project_lock(tmp_path):
        assert (tmp_path / LOCK_FILE).is_file()


def test_lock_held_elsewhere_fails_fast_in_another_process(tmp_path: Path) -> None:
    with project_lock(tmp_path):
        result = run_child(tmp_path)
    assert result.returncode == 3, result.stderr
    assert "another agent-sync run is in progress" in result.stdout
    assert str(tmp_path / LOCK_FILE) in result.stdout


def test_lock_is_free_in_another_process_after_release(tmp_path: Path) -> None:
    with project_lock(tmp_path):
        pass
    result = run_child(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "acquired" in result.stdout


def test_lock_is_reusable_after_release_and_after_an_error(tmp_path: Path) -> None:
    with project_lock(tmp_path):
        pass
    with pytest.raises(RuntimeError, match="inside"), project_lock(tmp_path):
        raise RuntimeError("inside")
    with project_lock(tmp_path):
        pass


def test_second_acquire_in_same_process_also_fails(tmp_path: Path) -> None:
    with project_lock(tmp_path):  # noqa: SIM117 - the inner call must run while the outer is held
        with pytest.raises(LockError, match="another agent-sync run is in progress"):
            with project_lock(tmp_path):
                pass


def test_lock_does_not_follow_a_symlinked_lock_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".agents").mkdir(parents=True)
    outside = tmp_path / "outside" / "lock"
    outside.parent.mkdir()
    (project / LOCK_FILE).symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"), project_lock(project):
        pass
    assert not outside.exists()
