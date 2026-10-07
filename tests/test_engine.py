"""Engine: plan validation, apply, prune. Direct API calls on a temporary project."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_sync import engine
from agent_sync.engine import (
    Change,
    Conflict,
    Op,
    PlanError,
    _apply_changes,
    _ensure_parents,
    check,
    prune_ops,
    run,
)
from agent_sync.state import (
    LOCK_FILE,
    STATE_FILE,
    Item,
    LockError,
    State,
    Status,
    fingerprint_bytes,
    fingerprint_link,
    fingerprint_path,
    fingerprint_tree,
    load_state,
    project_lock,
    save_state,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- helpers --------------------------------------------------------------------------------


def link_op(target: str, text: str = "../cache/foo", **kw: Any) -> Op:
    return Op("link", target, kw.pop("kind", "skills"), payload=text, **kw)


def file_op(target: str, data: bytes = b"data", **kw: Any) -> Op:
    return Op("write", target, kw.pop("kind", "agents"), payload=data, **kw)


def tree_op(target: str, files: dict[str, bytes] | None = None, **kw: Any) -> Op:
    payload = {"SKILL.md": b"# skill", "refs/a.md": b"a"} if files is None else files
    return Op("write", target, kw.pop("kind", "skills"), payload=payload, **kw)


def remove_op(target: str, **kw: Any) -> Op:
    return Op("remove", target, kw.pop("kind", "skills"), **kw)


def do_apply(
    project: Path, ops: list[Op], *, prune: bool = False, force: bool = False
) -> engine.RunResult:
    return run(project, ops, apply=True, prune=prune, force=force)


def tree_snapshot(project: Path) -> dict[str, str | None]:
    """Every path in the project (links not followed) -> fingerprint of its own content."""
    snap: dict[str, str | None] = {}
    for dirpath, dirnames, filenames in os.walk(project):
        for name in (*dirnames, *filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(project).as_posix()
            snap[rel] = "dir" if path.is_dir() and not path.is_symlink() else fingerprint_path(path)
    return snap


def assert_state_matches_disk(project: Path) -> None:
    for item in load_state(project).items:
        path = project / item.target
        if item.kind == "dir":
            assert path.is_dir() and not path.is_symlink(), item.target
        else:
            assert fingerprint_path(path) == item.sha256, item.target


def targets(state: State) -> set[str]:
    return {i.target for i in state.items}


def conflicts_of(exc: pytest.ExceptionInfo[PlanError]) -> tuple[Conflict, ...]:
    return exc.value.conflicts


# --- create / replace / no-op ---------------------------------------------------------------


def test_create_link_records_state(tmp_path: Path) -> None:
    op = link_op(".agents/skills/foo", "/cache/foo", harness=None, source="github.com:o/r:foo")
    result = do_apply(tmp_path, [op])
    assert result.changes == (Change(op, "create"),)
    assert os.readlink(tmp_path / ".agents/skills/foo") == "/cache/foo"
    item = result.state.find(".agents/skills/foo")
    assert item == Item(
        "skills",
        None,
        ".agents/skills/foo",
        None,
        "github.com:o/r:foo",
        fingerprint_link("/cache/foo"),
    )
    assert set(load_state(tmp_path).items) == set(result.state.items)


def test_create_file_records_state(tmp_path: Path) -> None:
    op = file_op(".codex/agents/a.toml", b"x = 1\n", harness="codex", source="s")
    result = do_apply(tmp_path, [op])
    assert result.changes == (Change(op, "create"),)
    assert (tmp_path / ".codex/agents/a.toml").read_bytes() == b"x = 1\n"
    item = result.state.find(".codex/agents/a.toml")
    assert item == Item(
        "agents", "codex", ".codex/agents/a.toml", None, "s", fingerprint_bytes(b"x = 1\n")
    )


def test_create_tree_records_state(tmp_path: Path) -> None:
    files = {"SKILL.md": b"# s", "refs/deep/a.md": b"a", "b.txt": b""}
    op = tree_op(".agents/skills/foo", files)
    result = do_apply(tmp_path, [op])
    assert result.changes == (Change(op, "create"),)
    for rel, data in files.items():
        assert (tmp_path / ".agents/skills/foo" / rel).read_bytes() == data
    item = result.state.find(".agents/skills/foo")
    assert item is not None
    assert item.sha256 == fingerprint_tree(files) == fingerprint_path(tmp_path / item.target)


def test_empty_tree_creates_an_empty_folder(tmp_path: Path) -> None:
    do_apply(tmp_path, [tree_op("out/empty", {})])
    assert (tmp_path / "out/empty").is_dir()
    assert list((tmp_path / "out/empty").iterdir()) == []
    assert do_apply(tmp_path, [tree_op("out/empty", {})]).changes == ()


def test_second_identical_run_makes_no_changes(tmp_path: Path) -> None:
    ops = [link_op(".agents/skills/a"), file_op("out/b", b"1"), tree_op(".agents/skills/c")]
    first = do_apply(tmp_path, ops)
    assert len(first.changes) == 3
    before_state = (tmp_path / STATE_FILE).read_bytes()
    before_disk = tree_snapshot(tmp_path)
    second = do_apply(tmp_path, ops)
    assert second.changes == ()
    assert set(second.state.items) == set(first.state.items)
    assert (tmp_path / STATE_FILE).read_bytes() == before_state
    assert tree_snapshot(tmp_path) == before_disk


def test_changed_link_is_replaced(tmp_path: Path) -> None:
    do_apply(tmp_path, [link_op("l", "one")])
    op = link_op("l", "two")
    result = do_apply(tmp_path, [op])
    assert result.changes == (Change(op, "replace"),)
    assert os.readlink(tmp_path / "l") == "two"
    assert result.state.find("l") is not None
    assert result.state.find("l").sha256 == fingerprint_link("two")  # type: ignore[union-attr]


def test_changed_file_is_replaced(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("f", b"old")])
    op = file_op("f", b"new")
    result = do_apply(tmp_path, [op])
    assert result.changes == (Change(op, "replace"),)
    assert (tmp_path / "f").read_bytes() == b"new"
    assert_state_matches_disk(tmp_path)


def test_changed_tree_is_replaced_and_stale_files_vanish(tmp_path: Path) -> None:
    do_apply(tmp_path, [tree_op("t", {"keep.md": b"1", "stale/x.md": b"x"})])
    op = tree_op("t", {"keep.md": b"2", "new.md": b"n"})
    result = do_apply(tmp_path, [op])
    assert result.changes == (Change(op, "replace"),)
    assert sorted(p.name for p in (tmp_path / "t").iterdir()) == ["keep.md", "new.md"]
    assert (tmp_path / "t/keep.md").read_bytes() == b"2"
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != ".agents") == [
        "t"
    ]  # no temp left
    assert_state_matches_disk(tmp_path)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (lambda t: file_op(t), lambda t: tree_op(t)),
        (lambda t: tree_op(t), lambda t: file_op(t)),
        (lambda t: tree_op(t), lambda t: link_op(t)),
        (lambda t: link_op(t), lambda t: tree_op(t)),
        (lambda t: link_op(t), lambda t: file_op(t)),
        (lambda t: file_op(t), lambda t: link_op(t)),
    ],
)
def test_replace_can_change_the_type_of_the_target(
    tmp_path: Path, first: Callable[[str], Op], second: Callable[[str], Op]
) -> None:
    do_apply(tmp_path, [first("x")])
    op = second("x")
    result = do_apply(tmp_path, [op])
    assert result.changes == (Change(op, "replace"),)
    assert_state_matches_disk(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != ".agents") == ["x"]


def test_link_to_a_directory_is_replaced_not_followed(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f").write_bytes(b"keep")
    do_apply(tmp_path, [link_op("l", "real")])
    do_apply(tmp_path, [tree_op("l", {"n": b"1"})])
    assert (tmp_path / "real" / "f").read_bytes() == b"keep"
    assert not (tmp_path / "l").is_symlink()


def test_files_are_written_through_a_sibling_temp_and_os_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str, bool, bytes | None]] = []
    real = os.replace

    def spy(src: Any, dst: Any, *a: Any, **kw: Any) -> None:
        src_path = Path(src)
        content = (
            src_path.read_bytes() if src_path.is_file() and not src_path.is_symlink() else None
        )
        seen.append((str(src), str(dst), src_path.is_symlink(), content))
        real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", spy)
    do_apply(tmp_path, [link_op("a/l", "txt"), file_op("a/f", b"payload")])
    by_dst = {Path(dst): (Path(src), is_link, content) for src, dst, is_link, content in seen}
    src, is_link, _ = by_dst[tmp_path / "a/l"]
    assert is_link and src.parent == tmp_path / "a" and src.name != "l"
    src, is_link, content = by_dst[tmp_path / "a/f"]
    assert not is_link and content == b"payload" and src.parent == tmp_path / "a"
    assert src.name != "f"


def test_dangling_link_is_created_and_stable(tmp_path: Path) -> None:
    op = link_op("l", "does/not/exist")
    do_apply(tmp_path, [op])
    assert os.readlink(tmp_path / "l") == "does/not/exist"
    assert do_apply(tmp_path, [op]).changes == ()


# --- check is pure --------------------------------------------------------------------------


def test_check_never_writes(tmp_path: Path) -> None:
    changes = check(
        tmp_path, State(()), [link_op("a/l"), file_op("b/f"), tree_op("c/t")], force=False
    )
    assert [c.effect for c in changes] == ["create"] * 3
    assert list(tmp_path.iterdir()) == []


def test_check_returns_only_needed_changes(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("same", b"1"), file_op("diff", b"1")])
    state = load_state(tmp_path)
    ops = [file_op("same", b"1"), file_op("diff", b"2"), file_op("new", b"3")]
    changes = check(tmp_path, state, ops, force=False)
    assert [(c.op.target, c.effect) for c in changes] == [("diff", "replace"), ("new", "create")]


# --- all-or-nothing -------------------------------------------------------------------------


def test_conflict_on_the_last_op_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / "theirs").write_bytes(b"mine")
    before = tree_snapshot(tmp_path)
    ops = [link_op(".agents/skills/a"), file_op("out/b"), tree_op("c"), file_op("theirs", b"x")]
    with pytest.raises(PlanError) as exc:
        do_apply(tmp_path, ops)
    assert [c.op.target for c in conflicts_of(exc)] == ["theirs"]
    assert tree_snapshot(tmp_path) == before  # nothing at all: no .agents/, no lock, no state
    assert not (tmp_path / STATE_FILE).exists()
    assert (tmp_path / "theirs").read_bytes() == b"mine"


def test_every_conflict_is_listed_in_one_plan_error(tmp_path: Path) -> None:
    (tmp_path / "x").write_bytes(b"1")
    (tmp_path / "y").write_bytes(b"2")
    do_apply(tmp_path, [file_op("owned", b"o")])
    (tmp_path / "owned").write_bytes(b"edited")
    ops = [file_op("x"), file_op("fine"), file_op("y"), file_op("owned", b"o2")]
    with pytest.raises(PlanError) as exc:
        do_apply(tmp_path, ops)
    found = conflicts_of(exc)
    assert [(c.op.target, c.status) for c in found] == [
        ("x", Status.NOT_OURS),
        ("y", Status.NOT_OURS),
        ("owned", Status.OWNED_EDITED),
    ]
    lines = str(exc.value).splitlines()
    assert len(lines) == 3
    assert lines == [c.reason for c in found]
    assert not (tmp_path / "fine").exists()


def test_parent_that_is_a_file_is_a_conflict(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"i am a file")
    with pytest.raises(PlanError) as exc:
        do_apply(tmp_path, [file_op("a/b")])
    assert "a" in conflicts_of(exc)[0].reason and "not a folder" in conflicts_of(exc)[0].reason
    assert (tmp_path / "a").read_bytes() == b"i am a file"


# --- edited items ---------------------------------------------------------------------------


def test_edited_item_is_a_conflict_without_force(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("f", b"mine")])
    (tmp_path / "f").write_bytes(b"edited")
    with pytest.raises(PlanError) as exc:
        do_apply(tmp_path, [file_op("f", b"mine")])
    [conflict] = conflicts_of(exc)
    assert conflict.status is Status.OWNED_EDITED
    assert (
        conflict.reason
        == "f was changed since agent-sync wrote it; delete it or re-run with --force"
    )
    assert (tmp_path / "f").read_bytes() == b"edited"


def test_edited_item_is_replaced_with_force(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("f", b"mine")])
    (tmp_path / "f").write_bytes(b"edited")
    op = file_op("f", b"mine")
    result = do_apply(tmp_path, [op], force=True)
    assert result.changes == (Change(op, "replace"),)
    assert (tmp_path / "f").read_bytes() == b"mine"
    assert_state_matches_disk(tmp_path)


def test_edited_tree_and_link_follow_the_same_rule(tmp_path: Path) -> None:
    do_apply(tmp_path, [tree_op("t", {"a": b"1"}), link_op("l", "one")])
    (tmp_path / "t" / "extra").write_bytes(b"added by user")
    (tmp_path / "l").unlink()
    (tmp_path / "l").symlink_to("elsewhere")
    ops = [tree_op("t", {"a": b"1"}), link_op("l", "one")]
    with pytest.raises(PlanError) as exc:
        do_apply(tmp_path, ops)
    assert [c.op.target for c in conflicts_of(exc)] == ["t", "l"]
    do_apply(tmp_path, ops, force=True)
    assert sorted(p.name for p in (tmp_path / "t").iterdir()) == ["a"]
    assert os.readlink(tmp_path / "l") == "one"


def test_edited_item_is_not_pruned_without_force_and_is_with_it(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("f", b"mine"), file_op("g", b"g")])
    (tmp_path / "f").write_bytes(b"edited")
    with pytest.raises(PlanError) as exc:
        do_apply(tmp_path, [], prune=True)
    assert [c.op.target for c in conflicts_of(exc)] == ["f"]
    assert (tmp_path / "g").exists()  # all-or-nothing: the clean item is kept too
    result = do_apply(tmp_path, [], prune=True, force=True)
    assert {c.op.target for c in result.changes} == {"f", "g"}
    assert not (tmp_path / "f").exists() and not (tmp_path / "g").exists()
    assert result.state.items == ()


def test_edited_conflict_does_not_hide_a_second_run_after_deleting_the_file(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("f", b"mine")])
    (tmp_path / "f").write_bytes(b"edited")
    (tmp_path / "f").unlink()
    op = file_op("f", b"mine")
    assert do_apply(tmp_path, [op]).changes == (Change(op, "create"),)


# --- not ours -------------------------------------------------------------------------------


@pytest.mark.parametrize("force", [False, True])
def test_foreign_path_is_never_touched(tmp_path: Path, force: bool) -> None:
    (tmp_path / "f").write_bytes(b"theirs")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x").write_bytes(b"theirs")
    (tmp_path / "l").symlink_to("elsewhere")
    before = tree_snapshot(tmp_path)
    ops = [file_op("f"), tree_op("d"), link_op("l")]
    with pytest.raises(PlanError) as exc:
        do_apply(tmp_path, ops, force=force)
    found = conflicts_of(exc)
    assert [(c.op.target, c.status) for c in found] == [
        ("f", Status.NOT_OURS),
        ("d", Status.NOT_OURS),
        ("l", Status.NOT_OURS),
    ]
    assert all(c.reason.startswith("Target already exists: ") for c in found)
    assert all("not created by agent-sync" in c.reason for c in found)
    assert all(str(tmp_path / c.op.target) in c.reason for c in found)
    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("force", [False, True])
def test_explicit_remove_of_a_foreign_path_is_a_conflict(tmp_path: Path, force: bool) -> None:
    (tmp_path / "f").write_bytes(b"theirs")
    with pytest.raises(PlanError) as exc:
        do_apply(tmp_path, [remove_op("f")], force=force)
    assert conflicts_of(exc)[0].status is Status.NOT_OURS
    assert (tmp_path / "f").read_bytes() == b"theirs"


@pytest.mark.parametrize("force", [False, True])
def test_adopt_remove_of_a_foreign_link_removes_it_because_the_planner_asked(
    tmp_path: Path, force: bool
) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "l").symlink_to("gone")
    op = remove_op("d/l", adopt=True)
    result = do_apply(tmp_path, [op], force=force)
    assert result.changes == (Change(op, "remove"),)
    assert not os.path.lexists(tmp_path / "d" / "l")
    assert result.state.items == ()


def test_remove_without_adopt_stays_a_conflict_and_touches_nothing(tmp_path: Path) -> None:
    (tmp_path / "l").symlink_to("gone")
    with pytest.raises(PlanError) as exc:
        do_apply(tmp_path, [remove_op("l")])
    assert conflicts_of(exc)[0].status is Status.NOT_OURS
    assert os.readlink(tmp_path / "l") == "gone"
    assert not (tmp_path / STATE_FILE).exists()


def test_adopt_remove_of_an_absent_path_is_a_no_op(tmp_path: Path) -> None:
    assert do_apply(tmp_path, [remove_op("l", adopt=True)]).changes == ()


def test_adopt_remove_does_not_take_over_a_folder_agent_sync_created(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("out/f")])
    with pytest.raises(PlanError):
        do_apply(tmp_path, [remove_op("out", adopt=True)], force=True)
    assert (tmp_path / "out" / "f").exists()


def test_adopt_remove_of_an_edited_owned_item_still_needs_force(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("f", b"mine")])
    (tmp_path / "f").write_bytes(b"edited")
    with pytest.raises(PlanError):
        do_apply(tmp_path, [remove_op("f", adopt=True)])
    assert (tmp_path / "f").read_bytes() == b"edited"


# --- adoption -------------------------------------------------------------------------------


def test_adopt_identical_link_only_records_it(tmp_path: Path) -> None:
    (tmp_path / "l").symlink_to("../cache/foo")
    inode = os.lstat(tmp_path / "l").st_ino
    op = link_op("l", "../cache/foo", adopt=True, source="s")
    result = do_apply(tmp_path, [op])
    assert result.changes == (Change(op, "adopt"),)
    assert os.lstat(tmp_path / "l").st_ino == inode
    assert result.state.find("l") == Item(
        "skills", None, "l", None, "s", fingerprint_link("../cache/foo")
    )
    assert do_apply(tmp_path, [op]).changes == ()


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("kind", ["file", "folder"])
@pytest.mark.parametrize("action", ["link", "remove"])
def test_adopt_only_ever_takes_over_a_symlink(
    tmp_path: Path, action: str, kind: str, force: bool
) -> None:
    """A real file or folder is the user's, whatever the planner says and with or without --force."""
    if kind == "file":
        (tmp_path / "t").write_bytes(b"same")
    else:
        (tmp_path / "t").mkdir()
        (tmp_path / "t" / "a").write_bytes(b"1")
    before = tree_snapshot(tmp_path)
    op = link_op("t", "x", adopt=True) if action == "link" else remove_op("t", adopt=True)
    with pytest.raises(PlanError) as exc:
        do_apply(tmp_path, [op], force=force)
    (found,) = conflicts_of(exc)
    assert found.status is Status.NOT_OURS
    assert found.reason.startswith("Target already exists: ")
    assert tree_snapshot(tmp_path) == before
    assert not (tmp_path / STATE_FILE).exists()


@pytest.mark.parametrize("make", ["file", "tree"])
def test_adopt_on_a_write_op_over_a_real_path_is_a_conflict(tmp_path: Path, make: str) -> None:
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "a").write_bytes(b"1")
    op = tree_op("t", {"a": b"1"}, adopt=True) if make == "tree" else file_op("t", adopt=True)
    with pytest.raises(PlanError):
        do_apply(tmp_path, [op])
    assert (tmp_path / "t" / "a").read_bytes() == b"1"


def test_adopt_different_content_replaces_it(tmp_path: Path) -> None:
    (tmp_path / "l").symlink_to("old")
    op = link_op("l", "new", adopt=True)
    result = do_apply(tmp_path, [op])
    assert result.changes == (Change(op, "replace"),)
    assert os.readlink(tmp_path / "l") == "new"
    assert_state_matches_disk(tmp_path)


def test_adopt_on_an_absent_path_just_creates(tmp_path: Path) -> None:
    op = link_op("l", "new", adopt=True)
    assert do_apply(tmp_path, [op]).changes == (Change(op, "create"),)


# --- prune ----------------------------------------------------------------------------------


def test_prune_ops_selects_items_the_plan_no_longer_produces() -> None:
    kept = Item("skills", None, "keep", None, "s0", "h")
    gone = Item("agents", "codex", "gone", None, "s1", "h")
    entry = Item("mcp", "claude", ".mcp.json", ("mcpServers", "x"), "s2", "h")
    folder = Item("dir", None, "out", None, "", "")
    state = State((kept, gone, entry, folder))
    plan = [link_op("keep"), file_op("new")]
    assert prune_ops(state, plan) == [
        Op("remove", "gone", "agents", "codex", "s1"),
        Op("remove", ".mcp.json", "mcp", "claude", "s2", None, ("mcpServers", "x")),
    ]


def test_prune_ops_matches_on_target_and_locator() -> None:
    entry = Item("mcp", None, "f.json", ("k",), "s", "h")
    whole = Item("mcp", None, "f.json", None, "s", "h")
    plan = [Op("remove", "f.json", "mcp", locator=("k",))]  # targets only the entry
    assert prune_ops(State((entry, whole)), plan) == [Op("remove", "f.json", "mcp", None, "s")]


def test_prune_removes_only_owned_items_and_keeps_the_rest(tmp_path: Path) -> None:
    do_apply(tmp_path, [link_op(".agents/skills/a"), link_op(".agents/skills/b"), file_op("o/f")])
    (tmp_path / ".agents/skills/mine").write_bytes(b"user file")
    (tmp_path / "unrelated").write_bytes(b"user file")
    result = do_apply(tmp_path, [link_op(".agents/skills/a")], prune=True)
    assert {(c.op.target, c.effect) for c in result.changes} == {
        (".agents/skills/b", "remove"),
        ("o/f", "remove"),
    }
    assert os.path.lexists(tmp_path / ".agents/skills/a")
    assert not os.path.lexists(tmp_path / ".agents/skills/b")
    assert (tmp_path / ".agents/skills/mine").read_bytes() == b"user file"
    assert (tmp_path / "unrelated").exists()
    assert targets(result.state) == {".agents/skills/a", ".agents/skills"}
    assert result.state.find(".agents/skills").kind == "dir"  # type: ignore[union-attr]
    assert_state_matches_disk(tmp_path)


def test_prune_never_emits_dir_items_and_second_prune_is_a_no_op(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("out/deep/f")])
    state = load_state(tmp_path)
    assert {i.kind for i in state.items} == {"agents", "dir"}
    assert [o.target for o in prune_ops(state, [])] == ["out/deep/f"]
    result = do_apply(tmp_path, [], prune=True)
    assert [c.effect for c in result.changes] == ["remove"]
    assert do_apply(tmp_path, [], prune=True).changes == ()


def test_prune_removes_a_tree_with_everything_in_it(tmp_path: Path) -> None:
    do_apply(tmp_path, [tree_op(".agents/skills/t", {"a/b/c": b"1", "d": b"2"})])
    do_apply(tmp_path, [], prune=True)
    assert not (tmp_path / ".agents/skills").exists()
    assert sorted(p.name for p in (tmp_path / ".agents").iterdir()) == [
        "agent-sync.lock",
        "agent-sync.state.json",
    ]


def test_prune_off_keeps_unplanned_items(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("a"), file_op("b")])
    result = do_apply(tmp_path, [file_op("a")], prune=False)
    assert result.changes == ()
    assert (tmp_path / "b").exists()


def test_dry_run_with_prune_reports_removals_and_changes_nothing(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("a"), file_op("b")])
    before = (tree_snapshot(tmp_path), (tmp_path / STATE_FILE).read_bytes())
    result = run(tmp_path, [file_op("a")], apply=False, prune=True, force=False)
    assert [(c.op.target, c.effect) for c in result.changes] == [("b", "remove")]
    assert (tree_snapshot(tmp_path), (tmp_path / STATE_FILE).read_bytes()) == before


# --- forget ---------------------------------------------------------------------------------


def test_owned_but_deleted_path_is_forgotten_on_prune(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("f"), file_op("g")])
    (tmp_path / "f").unlink()
    result = do_apply(tmp_path, [file_op("g")], prune=True)
    assert [(c.op.target, c.effect) for c in result.changes] == [("f", "forget")]
    assert targets(result.state) == {"g"}
    assert targets(load_state(tmp_path)) == {"g"}


def test_remove_of_an_absent_unrecorded_path_is_a_no_op(tmp_path: Path) -> None:
    assert do_apply(tmp_path, [remove_op("nothing")]).changes == ()


def test_explicit_remove_of_an_owned_unchanged_path(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("f")])
    op = remove_op("f", kind="agents")
    result = do_apply(tmp_path, [op])
    assert result.changes == (Change(op, "remove"),)
    assert not (tmp_path / "f").exists()
    assert result.state.items == ()


# --- engine-created folders -----------------------------------------------------------------


def test_engine_created_folders_are_recorded_and_removed_when_empty(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("out/a/b/f")])
    state = load_state(tmp_path)
    dirs = {i.target for i in state.items if i.kind == "dir"}
    assert dirs == {"out", "out/a", "out/a/b"}
    assert state.find("out/a") == Item("dir", None, "out/a", None, "", "")
    do_apply(tmp_path, [], prune=True)
    assert not (tmp_path / "out").exists()
    assert load_state(tmp_path).items == ()


def test_user_folders_are_kept(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    do_apply(tmp_path, [file_op("out/a/f")])
    assert {i.target for i in load_state(tmp_path).items if i.kind == "dir"} == {"out/a"}
    do_apply(tmp_path, [], prune=True)
    assert (tmp_path / "out").is_dir()
    assert not (tmp_path / "out/a").exists()


def test_engine_folder_with_a_user_file_is_kept_and_stays_recorded(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("out/f"), file_op("out/sub/g")])
    (tmp_path / "out/mine").write_bytes(b"user")
    do_apply(tmp_path, [], prune=True)
    assert (tmp_path / "out/mine").read_bytes() == b"user"
    assert not (tmp_path / "out/sub").exists()
    assert targets(load_state(tmp_path)) == {"out"}


def test_a_folder_shared_by_two_items_is_removed_with_the_last(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("out/a"), file_op("out/b")])
    do_apply(tmp_path, [file_op("out/a")], prune=True)
    assert (tmp_path / "out").is_dir()
    do_apply(tmp_path, [], prune=True)
    assert not (tmp_path / "out").exists()


def test_deleted_engine_folder_does_not_break_cleanup(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("out/a/f")])
    shutil.rmtree(tmp_path / "out")
    result = do_apply(tmp_path, [], prune=True)
    assert [c.effect for c in result.changes] == ["forget"]
    assert result.state.items == ()


def test_forgotten_item_also_cleans_up_empty_engine_folders(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("out/f")])
    (tmp_path / "out/f").unlink()
    do_apply(tmp_path, [], prune=True)
    assert not (tmp_path / "out").exists()
    assert load_state(tmp_path).items == ()


def test_a_dir_record_is_never_a_reason_to_remove_a_folder_with_content_it_did_not_create(
    tmp_path: Path,
) -> None:
    do_apply(tmp_path, [file_op("out/f")])
    (tmp_path / "out/f").unlink()
    (tmp_path / "out/user.txt").write_bytes(b"keep")
    do_apply(tmp_path, [], prune=True)
    assert (tmp_path / "out/user.txt").read_bytes() == b"keep"


# --- rejected plans (ValueError, before anything else) --------------------------------------


@pytest.mark.parametrize(
    "target",
    ["/abs/path", "", "../x", "a/../b", "a/..", "a\\b", "a\nb", "a//b", "./a", "a/", "."],
)
def test_bad_targets_are_rejected(tmp_path: Path, target: str) -> None:
    with pytest.raises(ValueError) as exc:
        check(tmp_path, State(()), [file_op(target)], force=False)
    assert not isinstance(exc.value, PlanError)


def test_bad_target_in_a_later_op_rejects_the_whole_plan(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        do_apply(tmp_path, [file_op("ok"), file_op("../escape")])
    assert not (tmp_path / "ok").exists()


def test_state_and_lock_files_are_not_targets(tmp_path: Path) -> None:
    for target in (STATE_FILE.as_posix(), LOCK_FILE.as_posix()):
        with pytest.raises(ValueError, match="agent-sync"):
            check(tmp_path, State(()), [file_op(target)], force=False)


def test_target_below_a_symlinked_folder_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".agents/skills").mkdir(parents=True)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/skills").symlink_to("../.agents/skills")
    for op in (
        link_op(".claude/skills/foo"),
        file_op(".claude/skills/f"),
        tree_op(".claude/skills/t"),
    ):
        with pytest.raises(ValueError, match="symlink"):
            do_apply(tmp_path, [op])
    assert list((tmp_path / ".agents/skills").iterdir()) == []


def test_symlinked_parent_further_up_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "real/sub").mkdir(parents=True)
    (tmp_path / "alias").symlink_to("real")
    with pytest.raises(ValueError, match="symlink"):
        do_apply(tmp_path, [file_op("alias/sub/f")])
    assert list((tmp_path / "real/sub").iterdir()) == []


def test_the_link_itself_may_be_a_symlink_target(tmp_path: Path) -> None:
    # Only the PARENTS are checked: the target of a link op is itself a symlink by design.
    (tmp_path / ".claude").mkdir()
    do_apply(tmp_path, [link_op(".claude/skills", "../.agents/skills")])
    assert os.readlink(tmp_path / ".claude/skills") == "../.agents/skills"


def test_duplicate_targets_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="twice|duplicate"):
        check(
            tmp_path, State(()), [file_op("a"), link_op("b"), file_op("a", b"other")], force=False
        )


def test_prune_remove_plus_plan_op_for_the_same_target_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="twice|duplicate"):
        check(tmp_path, State(()), [file_op("a"), remove_op("a")], force=False)


def test_overlapping_targets_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="overlap"):
        check(tmp_path, State(()), [tree_op("t"), file_op("t/x")], force=False)
    with pytest.raises(ValueError, match="overlap"):
        check(tmp_path, State(()), [file_op("t/x"), link_op("t")], force=False)


@pytest.mark.parametrize(
    "op",
    [
        Op("link", "l", "skills", payload=b"bytes"),
        Op("link", "l", "skills", payload=""),
        Op("write", "f", "agents", payload="text"),
        Op("write", "f", "agents", payload=None),
        Op("write", "t", "skills", payload={"a": "text"}),
        Op("write", "t", "skills", payload={"../x": b""}),
        Op("write", "t", "skills", payload={"/x": b""}),
        Op("write", "t", "skills", payload={"": b""}),
        Op("write", "t", "skills", payload={"a//b": b""}),
        Op("write", "t", "skills", payload={"a": b"", "a/b": b""}),
        Op("write", "f", "dir", payload=b""),
        Op("copy", "f", "agents", payload=b""),  # type: ignore[arg-type]
    ],
)
def test_malformed_ops_are_rejected(tmp_path: Path, op: Op) -> None:
    with pytest.raises(ValueError):
        check(tmp_path, State(()), [op], force=False)


# --- dry run --------------------------------------------------------------------------------


def test_dry_run_writes_nothing_at_all(tmp_path: Path) -> None:
    ops = [link_op(".agents/skills/a"), file_op(".codex/agents/b"), tree_op(".claude/c")]
    result = run(tmp_path, ops, apply=False, prune=True, force=False)
    assert [c.effect for c in result.changes] == ["create"] * 3
    assert result.state == State(())
    assert list(tmp_path.iterdir()) == []  # no .agents/, no lock, no state


def test_dry_run_and_apply_agree_on_the_changes(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("a", b"1"), file_op("b", b"1")])
    ops = [file_op("a", b"2"), file_op("c", b"3")]
    dry = run(tmp_path, ops, apply=False, prune=True, force=False)
    real = run(tmp_path, ops, apply=True, prune=True, force=False)
    assert dry.changes == real.changes
    assert dry.state != real.state


# --- crash safety ---------------------------------------------------------------------------


def four_ops() -> list[Op]:
    return [link_op("d1/a"), file_op("d2/b", b"b"), tree_op("d3/c"), file_op("d4/e", b"e")]


@pytest.mark.parametrize("k", [1, 2, 3, 4])
def test_failure_on_op_k_leaves_a_state_that_matches_the_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, k: int
) -> None:
    ops = four_ops()
    original = engine._apply_change
    calls = 0

    def flaky(project: Path, state: State, change: Change) -> State:
        nonlocal calls
        calls += 1
        if calls == k:
            raise RuntimeError("boom")
        return original(project, state, change)

    monkeypatch.setattr(engine, "_apply_change", flaky)
    with pytest.raises(RuntimeError, match="boom"):
        do_apply(tmp_path, ops)

    state = load_state(tmp_path) if (tmp_path / STATE_FILE).exists() else State(())
    assert_state_matches_disk(tmp_path)
    for i, op in enumerate(ops, start=1):
        recorded = state.find(op.target)
        if i < k:
            assert recorded is not None and recorded.kind == op.kind
            assert fingerprint_path(tmp_path / op.target) == recorded.sha256
        else:
            assert recorded is None
            assert not os.path.lexists(tmp_path / op.target)


@pytest.mark.parametrize("k", [1, 2, 3])
def test_failure_inside_an_op_rolls_back_its_folders_and_keeps_earlier_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, k: int
) -> None:
    ops = four_ops()[:3]
    original = engine._install
    calls = 0

    def flaky(dest: Path, payload: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == k:
            raise OSError("disk full")
        original(dest, payload)

    monkeypatch.setattr(engine, "_install", flaky)
    with pytest.raises(OSError, match="disk full"):
        do_apply(tmp_path, ops)

    assert_state_matches_disk(tmp_path)
    state = load_state(tmp_path)
    assert set(targets(state)) == {
        t for op in ops[: k - 1] for t in (op.target, str(Path(op.target).parent))
    }
    assert not (
        tmp_path / Path(ops[k - 1].target).parent
    ).exists()  # the failed op's folder is gone


def test_a_failed_run_can_be_retried_to_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = four_ops()
    original = engine._apply_change
    calls = 0

    def flaky(project: Path, state: State, change: Change) -> State:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("boom")
        return original(project, state, change)

    monkeypatch.setattr(engine, "_apply_change", flaky)
    with pytest.raises(RuntimeError):
        do_apply(tmp_path, ops)
    monkeypatch.undo()
    result = do_apply(tmp_path, ops)
    assert [c.op.target for c in result.changes] == ["d3/c", "d4/e"]
    assert do_apply(tmp_path, ops).changes == ()


def test_state_is_saved_after_every_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[int] = []
    real = save_state

    def spy(project: Path, state: State) -> None:
        real(project, state)
        saved.append(len([i for i in state.items if i.kind != "dir"]))

    monkeypatch.setattr("agent_sync.engine.save_state", spy)
    do_apply(tmp_path, [file_op("a"), file_op("b"), file_op("c")])
    assert [n for n in saved if n] == sorted(n for n in saved if n)  # monotonic
    assert {1, 2, 3} <= set(saved)


def test_apply_changes_executes_in_order_and_returns_the_state(tmp_path: Path) -> None:
    changes = [Change(file_op("one"), "create"), Change(file_op("two"), "create")]
    state = _apply_changes(tmp_path, State(()), changes)
    assert targets(state) == {"one", "two"}
    assert set(load_state(tmp_path).items) == set(state.items)


def test_apply_refuses_to_create_over_something_that_appeared(tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"theirs")
    with pytest.raises(FileExistsError):
        _apply_changes(tmp_path, State(()), [Change(file_op("f"), "create")])
    assert (tmp_path / "f").read_bytes() == b"theirs"


# --- lock -----------------------------------------------------------------------------------

HOLDER = """
import sys
from pathlib import Path
from agent_sync.state import project_lock
with project_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.readline()
"""


def test_apply_fails_while_another_process_holds_the_lock(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(LockError):
            do_apply(tmp_path, [file_op("f")])
        assert not (tmp_path / "f").exists()
        assert not (tmp_path / STATE_FILE).exists()
        # A dry run does not need the lock.
        assert run(tmp_path, [file_op("f")], apply=False, prune=False, force=False).changes
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=30)
        if holder.stdout is not None:
            holder.stdout.close()
    assert [c.effect for c in do_apply(tmp_path, [file_op("f")]).changes] == ["create"]


# --- run ------------------------------------------------------------------------------------


def test_run_reads_existing_state_from_disk(tmp_path: Path) -> None:
    save_state(tmp_path, State((Item("agents", None, "f", None, "s", fingerprint_bytes(b"x")),)))
    (tmp_path / "f").write_bytes(b"x")
    result = run(tmp_path, [file_op("f", b"x")], apply=False, prune=False, force=False)
    assert result.changes == ()
    assert result.state.find("f") is not None


def test_engine_never_prints(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    do_apply(tmp_path, [file_op("a"), link_op("l")])
    do_apply(tmp_path, [], prune=True)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


# --- fix round: run() writes nothing until it must ------------------------------------------


def test_a_noop_apply_creates_nothing(tmp_path: Path) -> None:
    result = run(tmp_path, [], apply=True, prune=True, force=False)
    assert result.changes == ()
    assert list(tmp_path.iterdir()) == []  # no .agents/, no lock file


def test_an_up_to_date_apply_does_not_take_the_lock(tmp_path: Path) -> None:
    ops = [file_op("f")]
    do_apply(tmp_path, ops)
    (tmp_path / LOCK_FILE).unlink()
    assert do_apply(tmp_path, ops).changes == ()
    assert not (tmp_path / LOCK_FILE).exists()


def test_a_malformed_plan_writes_nothing_even_with_apply(tmp_path: Path) -> None:
    for plan in ([file_op("ok"), file_op("../x")], [Op("merge", "m.json", "mcp", locator=("a",))]):
        with pytest.raises((ValueError, NotImplementedError)):
            do_apply(tmp_path, plan)
    assert list(tmp_path.iterdir()) == []


def test_the_plan_is_checked_again_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_lock = project_lock

    @contextmanager
    def racing_lock(project: Path) -> Iterator[None]:
        with real_lock(project):
            (project / "f").write_bytes(b"appeared while we waited")  # another run / the user
            yield

    monkeypatch.setattr("agent_sync.engine.project_lock", racing_lock)
    with pytest.raises(PlanError):
        do_apply(tmp_path, [file_op("f")])
    assert (tmp_path / "f").read_bytes() == b"appeared while we waited"
    assert not (tmp_path / STATE_FILE).exists()


# --- fix round: a crash between the write and the state save --------------------------------


def _fail_next_save_after_install(monkeypatch: pytest.MonkeyPatch) -> None:
    installed = False
    original_install = engine._install

    def install(dest: Path, payload: Any) -> None:
        nonlocal installed
        original_install(dest, payload)
        installed = True

    def save(project: Path, state: State) -> None:
        if installed:
            raise OSError("disk full")
        save_state(project, state)

    monkeypatch.setattr(engine, "_install", install)
    monkeypatch.setattr("agent_sync.engine.save_state", save)


@pytest.mark.parametrize("make", [file_op, link_op, tree_op])
def test_crash_after_creating_leaves_a_state_the_next_run_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Callable[..., Op]
) -> None:
    op = make("out/x")
    with monkeypatch.context() as m:
        _fail_next_save_after_install(m)
        with pytest.raises(OSError, match="disk full"):
            do_apply(tmp_path, [op])
    assert os.path.lexists(tmp_path / "out/x")
    assert_state_matches_disk(tmp_path)
    assert do_apply(tmp_path, [op]).changes == ()  # no NOT_OURS, no --force needed


@pytest.mark.parametrize("make", [file_op, link_op, tree_op])
def test_crash_after_replacing_leaves_a_state_the_next_run_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Callable[..., Op]
) -> None:
    old = make("out/x")
    do_apply(tmp_path, [old])
    new = file_op("out/x", b"new") if make is not file_op else file_op("out/x", b"newer")
    with monkeypatch.context() as m:
        _fail_next_save_after_install(m)
        with pytest.raises(OSError, match="disk full"):
            do_apply(tmp_path, [new])
    result = do_apply(tmp_path, [new])  # the disk already holds what we wanted: record only
    assert [c.effect for c in result.changes] == ["adopt"]
    assert do_apply(tmp_path, [new]).changes == ()
    assert_state_matches_disk(tmp_path)


def test_create_is_recorded_before_the_write_happens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Item | None] = []
    original = engine._install

    def spy(dest: Path, payload: Any) -> None:
        seen.append(load_state(tmp_path).find("out/f"))
        original(dest, payload)

    monkeypatch.setattr(engine, "_install", spy)
    do_apply(tmp_path, [file_op("out/f", b"x")])
    assert seen == [Item("agents", None, "out/f", None, "", fingerprint_bytes(b"x"))]


def test_a_folder_is_recorded_before_it_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []
    real_mkdir = os.mkdir

    def spy(path: Any, *a: Any, **kw: Any) -> None:
        if Path(path) == tmp_path / "out":
            seen.append(load_state(tmp_path).find("out") is not None)
        real_mkdir(path, *a, **kw)

    monkeypatch.setattr(os, "mkdir", spy)
    do_apply(tmp_path, [file_op("out/f")])
    assert seen == [True]


def test_a_failing_mkdir_leaves_no_record_of_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mkdir = os.mkdir

    def flaky(path: Any, *a: Any, **kw: Any) -> None:
        if Path(path) == tmp_path / "out" / "deep":
            raise OSError("no space")
        real_mkdir(path, *a, **kw)

    monkeypatch.setattr(os, "mkdir", flaky)
    with pytest.raises(OSError, match="no space"):
        do_apply(tmp_path, [file_op("out/deep/f")])
    assert not (tmp_path / "out").exists()
    assert load_state(tmp_path).items == ()


def test_an_edited_item_that_already_equals_the_plan_is_only_refreshed(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("f", b"v1")])
    (tmp_path / "f").write_bytes(b"v2")  # the user's edit happens to be what the plan now wants
    op = file_op("f", b"v2")
    result = do_apply(tmp_path, [op])
    assert result.changes == (Change(op, "adopt"),)
    assert (tmp_path / "f").read_bytes() == b"v2"
    assert_state_matches_disk(tmp_path)


# --- fix round: dir records are not content -------------------------------------------------


def test_classify_path_ignores_dir_records_via_the_engine(tmp_path: Path) -> None:
    do_apply(tmp_path, [file_op("out/f")])
    (tmp_path / "out/f").unlink()
    (tmp_path / "out/mine").write_bytes(b"user")
    # "out" is recorded as a folder agent-sync created; it must not look like an owned file.
    with pytest.raises(PlanError):
        do_apply(tmp_path, [file_op("out")], force=True)


@pytest.mark.parametrize("make", [file_op, link_op, tree_op, remove_op])
@pytest.mark.parametrize("force", [False, True])
def test_a_folder_agent_sync_created_is_never_overwritten_or_removed(
    tmp_path: Path, make: Callable[..., Op], force: bool
) -> None:
    do_apply(tmp_path, [file_op("out/f")])
    (tmp_path / "out/mine").write_bytes(b"user")
    with pytest.raises(PlanError) as exc:
        do_apply(tmp_path, [make("out")], force=force)
    [conflict] = conflicts_of(exc)
    assert conflict.reason == "out is a folder agent-sync created; it may hold your files"
    assert (tmp_path / "out/mine").read_bytes() == b"user"
    assert (tmp_path / "out/f").exists()


def test_a_stale_folder_record_does_not_block_a_new_target(tmp_path: Path) -> None:
    save_state(tmp_path, State((Item("dir", None, "out", None, "", ""),)))  # the folder is gone
    op = file_op("out", b"now a file")
    assert do_apply(tmp_path, [op]).changes == (Change(op, "create"),)
    assert_state_matches_disk(tmp_path)
    assert load_state(tmp_path).find("out").kind == "agents"  # type: ignore[union-attr]


@pytest.mark.parametrize("target", [".agents"])
def test_ancestors_of_the_bookkeeping_files_are_not_targets(tmp_path: Path, target: str) -> None:
    for make in (file_op, link_op, tree_op, remove_op):
        with pytest.raises(ValueError, match="agent-sync"):
            check(tmp_path, State(()), [make(target)], force=False)


def test_other_things_inside_agents_are_fine(tmp_path: Path) -> None:
    do_apply(tmp_path, [link_op(".agents/skills/x")])
    assert (tmp_path / ".agents/skills/x").is_symlink()


# --- fix round: the bookkeeping folder must be a real folder --------------------------------


@pytest.mark.parametrize("apply", [False, True])
def test_a_symlinked_agents_folder_is_refused_before_anything_is_created(
    tmp_path: Path, apply: bool
) -> None:
    project = tmp_path / "project"
    elsewhere = tmp_path / "elsewhere"
    project.mkdir()
    elsewhere.mkdir()
    (project / ".agents").symlink_to(elsewhere)
    with pytest.raises(ValueError, match=r"\.agents.*symlink"):
        run(project, [file_op("f")], apply=apply, prune=False, force=False)
    assert list(elsewhere.iterdir()) == []
    assert not (project / "f").exists()


@pytest.mark.parametrize("apply", [False, True])
def test_agents_that_is_a_file_is_refused(tmp_path: Path, apply: bool) -> None:
    (tmp_path / ".agents").write_bytes(b"i am a file")
    with pytest.raises(ValueError, match=r"\.agents"):
        run(tmp_path, [file_op("f")], apply=apply, prune=False, force=False)
    assert (tmp_path / ".agents").read_bytes() == b"i am a file"
    assert not (tmp_path / "f").exists()


def test_a_symlinked_agents_folder_is_refused_even_for_an_empty_plan(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / ".agents").symlink_to("real")
    with pytest.raises(ValueError, match="symlink"):
        run(tmp_path, [], apply=True, prune=True, force=False)


# --- fix round: names that only differ by case ----------------------------------------------


def test_targets_that_differ_only_by_case_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="case"):
        check(tmp_path, State(()), [file_op("Foo"), file_op("foo")], force=False)


def test_target_folders_that_differ_only_by_case_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="case"):
        check(tmp_path, State(()), [file_op("Out/a"), file_op("out/b")], force=False)


def test_tree_keys_that_differ_only_by_case_are_rejected(tmp_path: Path) -> None:
    for files in ({"A.md": b"", "a.md": b""}, {"Sub/x": b"", "sub/y": b""}):
        with pytest.raises(ValueError, match="case"):
            check(tmp_path, State(()), [tree_op("t", files)], force=False)


def test_the_same_name_twice_in_a_folder_is_not_a_case_clash(tmp_path: Path) -> None:
    assert len(check(tmp_path, State(()), [file_op("o/a"), file_op("o/b")], force=False)) == 2


# --- fix round: symlink re-checks at the moment of writing ----------------------------------


def test_remove_refuses_to_delete_through_a_symlinked_parent(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real/f").write_bytes(b"precious")
    (tmp_path / "alias").symlink_to("real")
    change = Change(remove_op("alias/f", kind="agents"), "remove")
    with pytest.raises(ValueError, match="symlink"):
        _apply_changes(tmp_path, State(()), [change])
    assert (tmp_path / "real/f").read_bytes() == b"precious"


def test_apply_refuses_to_create_through_a_symlinked_parent(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to("real")
    with pytest.raises(ValueError, match="symlink"):
        _apply_changes(tmp_path, State(()), [Change(file_op("alias/f"), "create")])
    assert list((tmp_path / "real").iterdir()) == []


def test_ensure_parents_refuses_a_symlink_part_way_up(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "a/b").symlink_to("../real")
    with pytest.raises(ValueError, match="symlink"):
        _ensure_parents(tmp_path, State(()), "a/b/c/f")
    assert list((tmp_path / "real").iterdir()) == []


# --- fix round: replacing a folder can fail half way ----------------------------------------


def test_a_failed_swap_puts_the_old_folder_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    do_apply(tmp_path, [tree_op("t", {"a": b"old"})])
    real_rename = os.rename
    calls = 0

    def flaky(src: Any, dst: Any, *a: Any, **kw: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:  # old -> aside worked; new -> dest fails
            raise OSError("cannot rename")
        real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(os, "rename", flaky)
    with pytest.raises(OSError, match="cannot rename"):
        do_apply(tmp_path, [tree_op("t", {"a": b"new"})])
    monkeypatch.undo()
    assert (tmp_path / "t/a").read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != ".agents") == ["t"]  # no temps
    assert_state_matches_disk(tmp_path)
    assert do_apply(tmp_path, [tree_op("t", {"a": b"old"})]).changes == ()


# --- fix round: adopt onto a real folder ----------------------------------------------------


def test_adopt_does_not_replace_a_real_folder(tmp_path: Path) -> None:
    (tmp_path / "skill").mkdir()
    (tmp_path / "skill/mine.md").write_bytes(b"user")
    with pytest.raises(PlanError):
        do_apply(tmp_path, [link_op("skill", "../cache/skill", adopt=True)], force=True)
    assert (tmp_path / "skill/mine.md").read_bytes() == b"user"


def test_a_conflict_carries_the_planners_hint(tmp_path: Path) -> None:
    (tmp_path / "t").mkdir()
    with pytest.raises(PlanError) as exc:
        do_apply(tmp_path, [link_op("t", hint="Do the other thing.")])
    reason = conflicts_of(exc)[0].reason
    assert reason.startswith("Target already exists: ")
    assert reason.endswith("Do the other thing.")


# --- fix round 2: folder rollback, adopt re-check, bookkeeping files ------------------------


@pytest.mark.parametrize("failing", ["a", "a/b", "a/b/c"])
def test_a_failing_mkdir_at_any_level_rolls_back_exactly_this_calls_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    (tmp_path / "keep").mkdir()  # a user folder, and an unrelated empty engine folder
    save_state(tmp_path, State((Item("dir", None, "old", None, "", ""),)))
    (tmp_path / "old").mkdir()
    real_mkdir = os.mkdir

    def flaky(path: Any, *a: Any, **kw: Any) -> None:
        if Path(path) == tmp_path / failing:
            raise OSError("no space")
        real_mkdir(path, *a, **kw)

    monkeypatch.setattr(os, "mkdir", flaky)
    with pytest.raises(OSError, match="no space"):
        do_apply(tmp_path, [file_op("a/b/c/f")])
    assert not (tmp_path / "a").exists()  # no empty engine folder left behind
    assert (tmp_path / "keep").is_dir() and (tmp_path / "old").is_dir()
    assert targets(load_state(tmp_path)) == {"old"}  # no record of a folder that is not there
    assert_state_matches_disk(tmp_path)


def test_a_failing_record_save_while_creating_folders_rolls_back_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def save(project: Path, state: State) -> None:
        if state.find("a/b") is not None:
            raise OSError("disk full")
        save_state(project, state)

    monkeypatch.setattr("agent_sync.engine.save_state", save)
    with pytest.raises(OSError, match="disk full"):
        do_apply(tmp_path, [file_op("a/b/f")])
    assert not (tmp_path / "a").exists()
    assert load_state(tmp_path).items == ()


def test_adopt_records_nothing_if_the_content_changed_since_the_check(tmp_path: Path) -> None:
    (tmp_path / "f").symlink_to("same")
    op = link_op("f", "same", adopt=True)
    changes = check(tmp_path, State(()), [op], force=False)
    assert changes == [Change(op, "adopt")]
    (tmp_path / "f").unlink()
    (tmp_path / "f").symlink_to("changed in between")
    with pytest.raises(RuntimeError, match="changed since"):
        _apply_changes(tmp_path, State(()), changes)
    assert not (tmp_path / STATE_FILE).exists()
    assert os.readlink(tmp_path / "f") == "changed in between"


@pytest.mark.parametrize("name", [LOCK_FILE.name, STATE_FILE.name])
@pytest.mark.parametrize("apply", [False, True])
def test_a_symlinked_bookkeeping_file_is_refused_and_not_followed(
    tmp_path: Path, name: str, apply: bool
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    (project / ".agents").mkdir(parents=True)
    outside.mkdir()
    (project / ".agents" / name).symlink_to(outside / "target")
    with pytest.raises(ValueError, match="symlink"):
        run(project, [file_op("f")], apply=apply, prune=False, force=False)
    assert list(outside.iterdir()) == []
    assert not (project / "f").exists()


def test_a_symlinked_state_file_to_an_existing_file_is_not_read(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".agents").mkdir(parents=True)
    secret = tmp_path / "secret.json"
    secret.write_text('{"version": 1, "items": []}')
    (project / STATE_FILE).symlink_to(secret)
    with pytest.raises(ValueError, match="symlink"):
        run(project, [], apply=False, prune=True, force=False)
    assert secret.read_text() == '{"version": 1, "items": []}'
