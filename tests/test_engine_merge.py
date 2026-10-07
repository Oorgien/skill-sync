"""Engine: merge and locator-remove for JSON and TOML files (key and list entries)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import tomlkit

from agent_sync import engine, shared_file
from agent_sync.cli import _exclude_paths
from agent_sync.engine import Change, Op, PlanError, check, prune_ops, run
from agent_sync.state import (
    STATE_FILE,
    Item,
    Status,
    fingerprint_entry,
    load_state,
    save_state,
)

SERVER = {"command": "npx", "args": ["-y", "gh"]}
SERVER2 = {"command": "uvx", "args": ["other"]}


def key_op(target: str, locator: tuple[str, ...], payload: Any, **kw: Any) -> Op:
    return Op("merge", target, kw.pop("kind", "mcp"), payload=payload, locator=locator, **kw)


def list_op(target: str, locator: tuple[str, ...], payload: Any, **kw: Any) -> Op:
    return Op(
        "merge",
        target,
        kw.pop("kind", "hooks"),
        payload=payload,
        locator=locator,
        container="list",
        **kw,
    )


def mcp(payload: Any = SERVER, name: str = "github", **kw: Any) -> Op:
    return key_op(".mcp.json", ("mcpServers", name), payload, **kw)


def apply(
    project: Path, ops: list[Op], *, prune: bool = False, force: bool = False
) -> engine.RunResult:
    return run(project, ops, apply=True, prune=prune, force=force)


def read_json(project: Path, rel: str = ".mcp.json") -> Any:
    return json.loads((project / rel).read_text(encoding="utf-8"))


def disk(project: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for dirpath, _dirs, files in os.walk(project):
        for name in files:
            path = Path(dirpath) / name
            out[path.relative_to(project).as_posix()] = path.read_bytes()
    return out


# --- JSON key entries -----------------------------------------------------------------------


def test_json_key_create_in_new_file_records_entry_and_file(tmp_path: Path) -> None:
    op = mcp(source="inline", harness="claude")
    result = apply(tmp_path, [op])
    assert result.changes == (Change(op, "create"),)
    assert read_json(tmp_path) == {"mcpServers": {"github": SERVER}}
    assert (tmp_path / ".mcp.json").read_text(encoding="utf-8").endswith("}\n")
    assert result.state.find(".mcp.json", ("mcpServers", "github")) == Item(
        "mcp", "claude", ".mcp.json", ("mcpServers", "github"), "inline", fingerprint_entry(SERVER)
    )
    assert result.state.find(".mcp.json") == Item("file", None, ".mcp.json", None, "", "")
    assert set(load_state(tmp_path).items) == set(result.state.items)


def test_json_key_second_run_is_a_noop_and_writes_nothing(tmp_path: Path) -> None:
    apply(tmp_path, [mcp()])
    before = disk(tmp_path)
    result = apply(tmp_path, [mcp()])
    assert result.changes == ()
    assert disk(tmp_path) == before


def test_json_key_replace_when_payload_changes(tmp_path: Path) -> None:
    apply(tmp_path, [mcp()])
    op = mcp(SERVER2)
    result = apply(tmp_path, [op])
    assert result.changes == (Change(op, "replace"),)
    assert read_json(tmp_path) == {"mcpServers": {"github": SERVER2}}
    item = load_state(tmp_path).find(".mcp.json", ("mcpServers", "github"))
    assert item is not None and item.sha256 == fingerprint_entry(SERVER2)


def test_json_round_trip_keeps_order_and_unrelated_keys(tmp_path: Path) -> None:
    original = {
        "zeta": 1,
        "mcpServers": {"mine": {"command": "x"}},
        "alpha": {"nested": [1, 2, "é"]},
    }
    (tmp_path / ".mcp.json").write_text(json.dumps(original, indent=2), encoding="utf-8")
    apply(tmp_path, [mcp()])
    after = read_json(tmp_path)
    assert list(after) == ["zeta", "mcpServers", "alpha"]
    assert list(after["mcpServers"]) == ["mine", "github"]
    del after["mcpServers"]["github"]
    assert after == original
    assert "é" in (tmp_path / ".mcp.json").read_text(encoding="utf-8")
    # an existing file is not ours: no file item
    assert load_state(tmp_path).find(".mcp.json") is None


def test_json_not_ours_is_a_conflict_even_with_force(tmp_path: Path) -> None:
    mine = {"mcpServers": {"github": {"command": "mine"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(mine), encoding="utf-8")
    before = disk(tmp_path)
    for force in (False, True):
        with pytest.raises(PlanError) as exc:
            apply(tmp_path, [mcp()], force=force)
        (conflict,) = exc.value.conflicts
        assert conflict.status is Status.NOT_OURS
        assert conflict.reason.startswith("Target already exists")
    assert disk(tmp_path) == before


def test_json_edited_key_entry_needs_force(tmp_path: Path) -> None:
    apply(tmp_path, [mcp()])
    doc = read_json(tmp_path)
    doc["mcpServers"]["github"]["command"] = "edited"
    (tmp_path / ".mcp.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(PlanError) as exc:
        apply(tmp_path, [mcp(SERVER2)])
    assert exc.value.conflicts[0].status is Status.OWNED_EDITED
    assert read_json(tmp_path)["mcpServers"]["github"]["command"] == "edited"
    result = apply(tmp_path, [mcp(SERVER2)], force=True)
    assert result.changes[0].effect == "replace"
    assert read_json(tmp_path) == {"mcpServers": {"github": SERVER2}}


def test_json_edited_entry_already_desired_is_adopted(tmp_path: Path) -> None:
    apply(tmp_path, [mcp()])
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"github": SERVER2}}))
    result = apply(tmp_path, [mcp(SERVER2)])
    assert result.changes[0].effect == "adopt"
    assert apply(tmp_path, [mcp(SERVER2)]).changes == ()


def test_json_prune_deletes_entry_and_created_file(tmp_path: Path) -> None:
    apply(tmp_path, [mcp()])
    result = apply(tmp_path, [], prune=True)
    assert [c.effect for c in result.changes] == ["remove"]
    assert not (tmp_path / ".mcp.json").exists()
    assert result.state.items == ()
    assert load_state(tmp_path).items == ()


def test_json_prune_keeps_file_with_user_content_and_other_entries(tmp_path: Path) -> None:
    apply(tmp_path, [mcp(), mcp(SERVER2, "other")])
    doc = read_json(tmp_path)
    doc["theirs"] = True
    (tmp_path / ".mcp.json").write_text(json.dumps(doc), encoding="utf-8")
    apply(tmp_path, [mcp(SERVER2, "other")], prune=True)
    assert read_json(tmp_path) == {"mcpServers": {"other": SERVER2}, "theirs": True}
    apply(tmp_path, [], prune=True)
    assert read_json(tmp_path) == {"theirs": True}  # emptied containers removed, file kept
    assert load_state(tmp_path).find(".mcp.json", ("mcpServers", "other")) is None


def test_json_prune_of_edited_entry_needs_force(tmp_path: Path) -> None:
    apply(tmp_path, [mcp()])
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"github": SERVER2}}))
    with pytest.raises(PlanError):
        apply(tmp_path, [], prune=True)
    apply(tmp_path, [], prune=True, force=True)
    assert not (tmp_path / ".mcp.json").exists()


def test_remove_of_unowned_key_entry_is_a_conflict_even_with_force(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"github": SERVER}}))
    op = Op("remove", ".mcp.json", "mcp", locator=("mcpServers", "github"))
    with pytest.raises(PlanError):
        apply(tmp_path, [op], force=True)
    assert read_json(tmp_path) == {"mcpServers": {"github": SERVER}}


def test_entry_removed_by_user_is_forgotten(tmp_path: Path) -> None:
    apply(tmp_path, [mcp(), mcp(SERVER2, "other")])
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": SERVER2}}))
    result = apply(tmp_path, [mcp(SERVER2, "other")], prune=True)
    assert [c.effect for c in result.changes] == ["forget"]
    assert read_json(tmp_path) == {"mcpServers": {"other": SERVER2}}


def test_two_merges_into_one_file_in_one_run(tmp_path: Path) -> None:
    result = apply(tmp_path, [mcp(), mcp(SERVER2, "other")])
    assert [c.effect for c in result.changes] == ["create", "create"]
    assert read_json(tmp_path) == {"mcpServers": {"github": SERVER, "other": SERVER2}}
    file_items = [i for i in result.state.items if i.kind == "file"]
    assert len(file_items) == 1


# --- JSON list entries ----------------------------------------------------------------------

GROUP = {"matcher": "", "hooks": [{"type": "command", "command": "echo a"}]}
GROUP2 = {"matcher": "", "hooks": [{"type": "command", "command": "echo b"}]}
THEIRS = {"matcher": "x", "hooks": [{"type": "command", "command": "mine"}]}


def hook(payload: Any = GROUP, name: str = "a", **kw: Any) -> Op:
    return list_op(".claude/settings.json", ("hooks", "Stop", name), payload, **kw)


def settings(project: Path) -> Any:
    return read_json(project, ".claude/settings.json")


def test_list_create_appends_and_creates_file_and_parents(tmp_path: Path) -> None:
    result = apply(tmp_path, [hook()])
    assert settings(tmp_path) == {"hooks": {"Stop": [GROUP]}}
    item = result.state.find(".claude/settings.json", ("hooks", "Stop", "a"))
    assert item is not None and item.sha256 == fingerprint_entry(GROUP)
    assert result.state.find(".claude") is not None  # dir item
    assert result.state.find(".claude/settings.json") == Item(
        "file", None, ".claude/settings.json", None, "", ""
    )


def test_list_second_run_is_a_noop(tmp_path: Path) -> None:
    apply(tmp_path, [hook()])
    before = disk(tmp_path)
    assert apply(tmp_path, [hook()]).changes == ()
    assert disk(tmp_path) == before


def test_list_replace_keeps_index(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text(
        json.dumps({"hooks": {"Stop": [THEIRS]}, "x": 1}), encoding="utf-8"
    )
    apply(tmp_path, [hook(), hook(GROUP2, "b")])
    apply(tmp_path, [hook(GROUP2), hook(GROUP2, "b")])  # "a" changes to equal b's content
    stop = settings(tmp_path)["hooks"]["Stop"]
    assert stop == [THEIRS, GROUP2, GROUP2]


def test_list_replace_in_place(tmp_path: Path) -> None:
    apply(tmp_path, [hook(), hook(GROUP2, "b")])
    new = {"matcher": "m", "hooks": []}
    apply(tmp_path, [hook(new), hook(GROUP2, "b")])
    assert settings(tmp_path)["hooks"]["Stop"] == [new, GROUP2]


def test_list_element_edited_by_user_is_reappended_theirs_untouched(tmp_path: Path) -> None:
    apply(tmp_path, [hook()])
    edited = {"matcher": "edited", "hooks": []}
    doc = settings(tmp_path)
    doc["hooks"]["Stop"][0] = edited
    (tmp_path / ".claude/settings.json").write_text(json.dumps(doc), encoding="utf-8")
    result = apply(tmp_path, [hook()])
    assert result.changes[0].effect == "create"
    assert settings(tmp_path)["hooks"]["Stop"] == [edited, GROUP]


def test_list_identical_unowned_element_is_a_conflict(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text(json.dumps({"hooks": {"Stop": [GROUP]}}))
    before = disk(tmp_path)
    with pytest.raises(PlanError) as exc:
        apply(tmp_path, [hook()], force=True)
    assert "an identical entry already exists" in exc.value.conflicts[0].reason
    assert disk(tmp_path) == before


def test_list_unowned_different_element_is_appended_after(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text(json.dumps({"hooks": {"Stop": [THEIRS]}}))
    apply(tmp_path, [hook()])
    assert settings(tmp_path)["hooks"]["Stop"] == [THEIRS, GROUP]


def test_list_prune_removes_element_and_empty_containers(tmp_path: Path) -> None:
    apply(tmp_path, [hook(), hook(GROUP2, "b")])
    apply(tmp_path, [hook(GROUP2, "b")], prune=True)
    assert settings(tmp_path) == {"hooks": {"Stop": [GROUP2]}}
    apply(tmp_path, [], prune=True)
    assert not (tmp_path / ".claude/settings.json").exists()
    assert not (tmp_path / ".claude").exists()  # the dir we created goes too
    assert load_state(tmp_path).items == ()


def test_list_prune_forgets_when_the_element_is_gone(tmp_path: Path) -> None:
    apply(tmp_path, [hook()])
    (tmp_path / ".claude/settings.json").write_text(json.dumps({"hooks": {"Stop": [THEIRS]}}))
    result = apply(tmp_path, [], prune=True)
    assert [c.effect for c in result.changes] == ["forget"]
    assert settings(tmp_path) == {"hooks": {"Stop": [THEIRS]}}


def test_prune_ops_uses_list_container_for_hooks(tmp_path: Path) -> None:
    result = apply(tmp_path, [hook(), mcp()])
    ops = prune_ops(result.state, [])
    by_kind = {op.kind: op for op in ops}
    assert set(by_kind) == {"hooks", "mcp"}  # file and dir items are never pruned
    assert by_kind["hooks"].container == "list"
    assert by_kind["mcp"].container == "key"


# --- TOML -----------------------------------------------------------------------------------

TOML_DOC = (
    "# my codex config\n"
    'model = "x"  # inline\n'
    "\n"
    "[profiles.a]\n"
    "k = 1\n"
    "\n"
    "# keep me\n"
    "[other]\n"
    'y = "z"\n'
)
CODEX = ".codex/config.toml"


def toml_op(payload: Any = SERVER, name: str = "github", **kw: Any) -> Op:
    return key_op(CODEX, ("mcp_servers", name), payload, **kw)


def test_toml_create_in_new_file(tmp_path: Path) -> None:
    result = apply(tmp_path, [toml_op()])
    assert result.changes[0].effect == "create"
    assert tomlkit.parse((tmp_path / CODEX).read_text()).unwrap() == {
        "mcp_servers": {"github": SERVER}
    }
    assert "[mcp_servers.github]" in (tmp_path / CODEX).read_text()
    assert result.state.find(CODEX) == Item("file", None, CODEX, None, "", "")
    assert apply(tmp_path, [toml_op()]).changes == ()


def test_toml_existing_file_preserved_byte_identical_outside_our_table(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / CODEX).write_text(TOML_DOC, encoding="utf-8")
    apply(tmp_path, [toml_op()])
    text = (tmp_path / CODEX).read_text()
    assert text.startswith(TOML_DOC.rstrip("\n"))
    assert text.count("[mcp_servers.github]") == 1
    assert "# my codex config" in text and "# keep me" in text and "# inline" in text
    outside = text.split("[mcp_servers.github]")[0].rstrip("\n")
    assert outside == TOML_DOC.rstrip("\n")
    apply(tmp_path, [], prune=True)
    # tomlkit keeps the blank line it put before our table; everything else is untouched
    assert (tmp_path / CODEX).read_text().rstrip("\n") == TOML_DOC.rstrip("\n")


def test_toml_replace_and_edit_rules(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / CODEX).write_text(TOML_DOC, encoding="utf-8")
    apply(tmp_path, [toml_op()])
    result = apply(tmp_path, [toml_op(SERVER2)])
    assert result.changes[0].effect == "replace"
    text = (tmp_path / CODEX).read_text()
    assert 'command = "uvx"' in text and "npx" not in text
    assert text.startswith(TOML_DOC.rstrip("\n"))
    (tmp_path / CODEX).write_text(text.replace("uvx", "mine"), encoding="utf-8")
    with pytest.raises(PlanError):
        apply(tmp_path, [toml_op(SERVER)])
    apply(tmp_path, [toml_op(SERVER)], force=True)
    assert 'command = "npx"' in (tmp_path / CODEX).read_text()


def test_toml_not_ours_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / CODEX).write_text('[mcp_servers.github]\ncommand = "mine"\n', encoding="utf-8")
    for force in (False, True):
        with pytest.raises(PlanError) as exc:
            apply(tmp_path, [toml_op()], force=force)
        assert exc.value.conflicts[0].reason.startswith("Target already exists")
    assert (tmp_path / CODEX).read_text() == '[mcp_servers.github]\ncommand = "mine"\n'


def test_toml_accepts_a_tomlkit_table_payload_keeping_comments(tmp_path: Path) -> None:
    native = tomlkit.parse('[github]\n# why\ncommand = "npx"\nargs = ["a"]\n')["github"]
    apply(tmp_path, [toml_op(native)])
    text = (tmp_path / CODEX).read_text()
    assert "# why" in text
    assert apply(tmp_path, [toml_op({"command": "npx", "args": ["a"]})]).changes == ()


def test_toml_prune_deletes_empty_created_file(tmp_path: Path) -> None:
    apply(tmp_path, [toml_op()])
    apply(tmp_path, [], prune=True)
    assert not (tmp_path / CODEX).exists()
    assert load_state(tmp_path).items == ()


# --- created files and exclusion ------------------------------------------------------------


def test_file_item_is_listed_for_exclusion_dir_items_are_not(tmp_path: Path) -> None:
    ops = [hook(), mcp()]
    would_be = _exclude_paths(
        tmp_path, load_state(tmp_path), ops, prune=True, create_bookkeeping=False
    )
    assert ".mcp.json" in would_be and ".claude/settings.json" in would_be
    result = apply(tmp_path, ops)
    after = _exclude_paths(tmp_path, result.state, ops, prune=False, create_bookkeeping=False)
    assert ".mcp.json" in after and ".claude/settings.json" in after
    assert ".claude" not in after
    pruned = _exclude_paths(tmp_path, result.state, ops, prune=True, create_bookkeeping=False)
    assert ".mcp.json" in pruned


def test_preexisting_file_is_not_listed_for_exclusion(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("{}")
    ops = [mcp()]
    paths = _exclude_paths(
        tmp_path, load_state(tmp_path), ops, prune=False, create_bookkeeping=False
    )
    assert ".mcp.json" not in paths
    result = apply(tmp_path, ops)
    paths = _exclude_paths(tmp_path, result.state, ops, prune=False, create_bookkeeping=False)
    assert ".mcp.json" not in paths


def test_whole_path_op_on_a_created_file_is_a_conflict_and_never_pruned(tmp_path: Path) -> None:
    result = apply(tmp_path, [mcp()])
    with pytest.raises(PlanError):
        apply(tmp_path, [Op("write", ".mcp.json", "agents", payload=b"x")], force=True)
    with pytest.raises(PlanError):
        apply(tmp_path, [Op("remove", ".mcp.json", "agents")], force=True)
    assert all(op.locator is not None for op in prune_ops(result.state, []))


def test_file_item_survives_when_user_added_content(tmp_path: Path) -> None:
    apply(tmp_path, [mcp()])
    doc = read_json(tmp_path)
    doc["theirs"] = 1
    (tmp_path / ".mcp.json").write_text(json.dumps(doc))
    apply(tmp_path, [], prune=True)
    assert read_json(tmp_path) == {"theirs": 1}
    assert load_state(tmp_path).find(".mcp.json") is not None


# --- invalid files and shape ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "text", "op"),
    [
        (".mcp.json", "{not json", mcp()),
        (".mcp.json", "[1, 2]", mcp()),
        (CODEX, "this is = = bad", toml_op()),
    ],
)
def test_invalid_file_is_an_error_naming_the_file_and_nothing_is_written(
    tmp_path: Path, rel: str, text: str, op: Op
) -> None:
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(text, encoding="utf-8")
    other = Op("write", "agents/a.md", "agents", payload=b"x")
    before = disk(tmp_path)
    with pytest.raises(ValueError, match=rel.replace(".", r"\.")):
        apply(tmp_path, [other, op])
    assert disk(tmp_path) == before
    assert not (tmp_path / STATE_FILE).exists()


def test_invalid_file_during_prune_is_an_error(tmp_path: Path) -> None:
    apply(tmp_path, [mcp()])
    (tmp_path / ".mcp.json").write_text("{bad")
    with pytest.raises(ValueError, match=r"\.mcp\.json"):
        apply(tmp_path, [], prune=True)
    assert (tmp_path / ".mcp.json").read_text() == "{bad"


def test_unknown_extension_is_a_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="settings.yaml"):
        apply(tmp_path, [key_op("settings.yaml", ("a", "b"), {})])


def test_same_target_and_locator_twice_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="twice"):
        apply(tmp_path, [mcp(), mcp(SERVER2)])


def test_whole_path_op_on_a_file_with_merge_ops_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"\.mcp\.json"):
        apply(tmp_path, [mcp(), Op("write", ".mcp.json", "agents", payload=b"x")])


def test_bad_locator_and_container_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        apply(tmp_path, [key_op(".mcp.json", (), {})])
    with pytest.raises(ValueError):
        apply(tmp_path, [list_op(".claude/settings.json", ("only",), {})])
    with pytest.raises(ValueError):
        apply(tmp_path, [Op("merge", ".mcp.json", "mcp", payload={})])


def test_blocked_path_is_a_conflict(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": 3}))
    with pytest.raises(PlanError):
        apply(tmp_path, [mcp()])


def test_target_through_symlink_or_symlink_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "real.json").write_text("{}")
    os.symlink("real.json", tmp_path / ".mcp.json")
    with pytest.raises(ValueError, match="symlink"):
        apply(tmp_path, [mcp()])


# --- dry run, atomicity, failure injection --------------------------------------------------


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    result = run(tmp_path, [mcp(), hook()], apply=False, prune=False, force=False)
    assert [c.effect for c in result.changes] == ["create", "create"]
    assert disk(tmp_path) == {}
    assert not (tmp_path / ".agents").exists()


def test_check_returns_changes_without_writing(tmp_path: Path) -> None:
    changes = check(tmp_path, load_state(tmp_path), [mcp()], force=False)
    assert changes == [Change(mcp(), "create")]
    assert disk(tmp_path) == {}


def test_no_temp_files_left_behind(tmp_path: Path) -> None:
    apply(tmp_path, [mcp(), mcp(SERVER2, "o")])
    assert sorted(disk(tmp_path)) == [".agents/agent-sync.state.json", ".mcp.json"] or sorted(
        disk(tmp_path)
    ) == [".agents/agent-sync.lock", ".agents/agent-sync.state.json", ".mcp.json"]


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_failure_after_op_k_leaves_state_matching_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: int
) -> None:
    real = os.replace
    calls = {"n": 0}

    def flaky(src: Any, dst: Any) -> None:
        if str(dst).endswith(".mcp.json") or str(dst).endswith("settings.json"):
            calls["n"] += 1
            if calls["n"] == fail_at:
                raise OSError("boom")
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    ops = [mcp(), mcp(SERVER2, "o"), hook()]
    with pytest.raises(OSError, match="boom"):
        apply(tmp_path, ops)
    monkeypatch.undo()
    state = load_state(tmp_path)
    for item in state.items:
        if item.locator is None or item.kind == "container":
            continue
        doc = read_json(tmp_path, item.target)
        assert item.kind in ("mcp", "hooks")
        if item.kind == "hooks":
            assert any(fingerprint_entry(e) == item.sha256 for e in doc["hooks"]["Stop"])
        else:
            assert fingerprint_entry(doc[item.locator[0]][item.locator[1]]) == item.sha256
    for item in state.items:
        if item.kind == "file":
            assert (tmp_path / item.target).exists()
        if item.kind == "dir":
            assert (tmp_path / item.target).is_dir()
    # and a later run completes the job
    apply(tmp_path, ops)
    assert read_json(tmp_path) == {"mcpServers": {"github": SERVER, "o": SERVER2}}
    assert settings(tmp_path) == {"hooks": {"Stop": [GROUP]}}


# --- fix round: unsafe layouts, crash recovery, user-created containers, JSON safety ---------


def test_toml_dotted_key_parent_is_a_conflict_and_nothing_is_written(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    text = 'model = "a"\nmcp_servers.mine.command = "x"\napproval_policy = "never"\n'
    (tmp_path / CODEX).write_text(text, encoding="utf-8")
    other = Op("write", "agents/a.md", "agents", payload=b"x")
    before = disk(tmp_path)
    for force in (False, True):
        with pytest.raises(PlanError) as exc:
            apply(tmp_path, [other, toml_op()], force=force)
        reason = exc.value.conflicts[0].reason
        assert CODEX in reason and "[mcp_servers]" in reason
    assert disk(tmp_path) == before


def test_toml_inline_table_parent_is_a_conflict_in_check(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / CODEX).write_text('mcp_servers = { mine = { command = "x" } }\n', encoding="utf-8")
    other = Op("write", "agents/a.md", "agents", payload=b"x")
    before = disk(tmp_path)
    with pytest.raises(PlanError) as exc:
        apply(tmp_path, [other, toml_op()])
    assert CODEX in exc.value.conflicts[0].reason
    assert "[mcp_servers]" in exc.value.conflicts[0].reason
    assert disk(tmp_path) == before


def test_apply_edit_refuses_an_edit_that_changes_other_data(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    text = 'model = "a"\nmcp_servers.mine.command = "x"\napproval_policy = "never"\n'
    (tmp_path / CODEX).write_text(text, encoding="utf-8")
    doc = shared_file.load_doc(tmp_path, CODEX)
    with pytest.raises(ValueError, match=r"config\.toml"):
        shared_file.apply_edit(
            doc, CODEX, lambda d: shared_file.set_key(d, ("mcp_servers", "g"), SERVER, [])
        )


class _FailSaveAfterWrite:
    """Make the next state save fail once a file write has happened."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, suffix: str) -> None:
        self.armed = False
        self.fired = False
        real_replace = os.replace
        real_save = save_state

        def replace(src: Any, dst: Any) -> None:
            real_replace(src, dst)
            if str(dst).endswith(suffix):
                self.armed = True

        def save(project: Path, state: Any) -> None:
            if self.armed and not self.fired:
                self.fired = True
                raise OSError("state save failed")
            real_save(project, state)

        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr("agent_sync.engine.save_state", save)


def test_list_replace_survives_a_failed_state_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply(tmp_path, [hook()])
    new = {"matcher": "m", "hooks": []}
    with monkeypatch.context() as m:
        _FailSaveAfterWrite(m, "settings.json")
        with pytest.raises(OSError, match="state save failed"):
            apply(tmp_path, [hook(new)])
    assert settings(tmp_path)["hooks"]["Stop"] == [new]
    result = apply(tmp_path, [hook(new)])  # no --force needed
    assert [c.effect for c in result.changes] == ["adopt"]
    assert apply(tmp_path, [hook(new)]).changes == ()
    apply(tmp_path, [], prune=True)
    assert not (tmp_path / ".claude/settings.json").exists()


def test_failure_on_a_replace_leaves_state_matching_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply(tmp_path, [mcp(), hook()])
    before = disk(tmp_path)
    real = os.replace

    def flaky(src: Any, dst: Any) -> None:
        if str(dst).endswith("settings.json"):
            raise OSError("boom")
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError, match="boom"):
        apply(tmp_path, [mcp(SERVER2), hook(GROUP2)])
    monkeypatch.undo()
    state = load_state(tmp_path)
    item = state.find(".mcp.json", ("mcpServers", "github"))
    assert item is not None and item.sha256 == fingerprint_entry(SERVER2)
    assert read_json(tmp_path)["mcpServers"]["github"] == SERVER2
    assert (tmp_path / ".claude/settings.json").read_bytes() == before[".claude/settings.json"]
    hook_item = state.find(".claude/settings.json", ("hooks", "Stop", "a"))
    assert hook_item is not None and hook_item.sha256 == fingerprint_entry(GROUP)
    apply(tmp_path, [mcp(SERVER2), hook(GROUP2)])
    assert settings(tmp_path)["hooks"]["Stop"] == [GROUP2]


def test_failure_on_a_remove_leaves_state_matching_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply(tmp_path, [mcp(), mcp(SERVER2, "o")])
    before = disk(tmp_path)
    real = os.replace

    def flaky(src: Any, dst: Any) -> None:
        if str(dst).endswith(".mcp.json"):
            raise OSError("boom")
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError, match="boom"):
        apply(tmp_path, [mcp()], prune=True)
    monkeypatch.undo()
    assert (tmp_path / ".mcp.json").read_bytes() == before[".mcp.json"]
    assert load_state(tmp_path).find(".mcp.json", ("mcpServers", "o")) is not None
    apply(tmp_path, [mcp()], prune=True)
    assert read_json(tmp_path) == {"mcpServers": {"github": SERVER}}


def test_failed_state_save_after_a_remove_is_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply(tmp_path, [mcp(), mcp(SERVER2, "o")])
    with monkeypatch.context() as m:
        _FailSaveAfterWrite(m, ".mcp.json")
        with pytest.raises(OSError, match="state save failed"):
            apply(tmp_path, [mcp()], prune=True)
    result = apply(tmp_path, [mcp()], prune=True)
    assert [c.effect for c in result.changes] == ["forget"]
    assert read_json(tmp_path) == {"mcpServers": {"github": SERVER}}


def test_toml_container_with_a_comment_the_user_owns_is_kept(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    text = "[mcp_servers]\n# my servers go here\n"
    (tmp_path / CODEX).write_text(text, encoding="utf-8")
    apply(tmp_path, [toml_op()])
    apply(tmp_path, [], prune=True)
    assert (tmp_path / CODEX).read_text().startswith(text.rstrip("\n"))
    assert "github" not in (tmp_path / CODEX).read_text()
    assert load_state(tmp_path).items == ()


def test_user_created_empty_json_containers_are_kept(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text(json.dumps({"hooks": {"Stop": []}, "x": {}}))
    apply(tmp_path, [hook()])
    apply(tmp_path, [], prune=True)
    assert settings(tmp_path) == {"hooks": {"Stop": []}, "x": {}}


def test_only_containers_the_engine_created_are_removed(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text(json.dumps({"hooks": {}}))
    result = apply(tmp_path, [hook()])
    containers = {i.locator for i in result.state.items if i.kind == "container"}
    assert containers == {("hooks", "Stop")}  # "hooks" was the user's
    assert all(op.kind != "container" for op in prune_ops(result.state, []))
    apply(tmp_path, [], prune=True)
    assert settings(tmp_path) == {"hooks": {}}
    assert not [i for i in load_state(tmp_path).items if i.kind == "container"]


def test_duplicate_json_keys_are_an_error_and_the_file_is_not_rewritten(tmp_path: Path) -> None:
    text = '{"mcpServers": {"a": {"command": "x"}}, "mcpServers": {}}'
    (tmp_path / ".mcp.json").write_text(text)
    with pytest.raises(ValueError, match=r"\.mcp\.json.*duplicate"):
        apply(tmp_path, [mcp()])
    assert (tmp_path / ".mcp.json").read_text() == text


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_json_nan_and_infinity_are_an_error(tmp_path: Path, constant: str) -> None:
    text = f'{{"x": {constant}}}'
    (tmp_path / ".mcp.json").write_text(text)
    with pytest.raises(ValueError, match=r"\.mcp\.json"):
        apply(tmp_path, [mcp()])
    assert (tmp_path / ".mcp.json").read_text() == text


def test_nan_in_a_payload_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        apply(tmp_path, [mcp({"command": "x", "n": float("nan")})])
    assert not (tmp_path / ".mcp.json").exists()
    assert load_state(tmp_path).items == ()


# --- fix round 2: trailing comments, advice, nan ---------------------------------------------


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def test_toml_comment_after_our_table_survives_prune(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / CODEX).write_text('model = "a"\n', encoding="utf-8")
    apply(tmp_path, [toml_op()])
    _append(tmp_path / CODEX, '# Profiles for work\n[profiles.work]\nmodel = "b"\n')
    apply(tmp_path, [], prune=True)
    text = (tmp_path / CODEX).read_text()
    assert "github" not in text
    assert text.index("# Profiles for work") < text.index("[profiles.work]")
    assert tomlkit.parse(text).unwrap() == {"model": "a", "profiles": {"work": {"model": "b"}}}


def test_toml_comment_after_our_table_survives_replace(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / CODEX).write_text('model = "a"\n', encoding="utf-8")
    apply(tmp_path, [toml_op()])
    _append(tmp_path / CODEX, '# Profiles for work\n[profiles.work]\nmodel = "b"\n')
    apply(tmp_path, [toml_op(SERVER2)])
    text = (tmp_path / CODEX).read_text()
    assert 'command = "uvx"' in text
    assert text.index("[mcp_servers.github]") < text.index("# Profiles for work")
    assert text.index("# Profiles for work") < text.index("[profiles.work]")


def test_toml_comment_at_end_of_file_survives(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / CODEX).write_text('model = "a"\n', encoding="utf-8")
    apply(tmp_path, [toml_op()])
    _append(tmp_path / CODEX, "# the end\n")
    apply(tmp_path, [toml_op(SERVER2)])
    assert "# the end" in (tmp_path / CODEX).read_text()
    apply(tmp_path, [], prune=True)
    assert "# the end" in (tmp_path / CODEX).read_text()


def test_toml_comment_between_two_of_our_tables_survives_prune_of_the_first(
    tmp_path: Path,
) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / CODEX).write_text('model = "a"\n', encoding="utf-8")
    apply(tmp_path, [toml_op(), toml_op(SERVER2, "other")])
    text = (
        (tmp_path / CODEX)
        .read_text()
        .replace("[mcp_servers.other]", "# about other\n[mcp_servers.other]")
    )
    (tmp_path / CODEX).write_text(text, encoding="utf-8")
    apply(tmp_path, [toml_op(SERVER2, "other")], prune=True)
    after = (tmp_path / CODEX).read_text()
    assert "# about other" in after and "github" not in after


def test_apply_edit_refuses_an_edit_that_drops_a_comment(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / CODEX).write_text('# keep me\nmodel = "a"\n', encoding="utf-8")
    doc = shared_file.load_doc(tmp_path, CODEX)

    def drop_everything(d: shared_file.Doc) -> None:
        del d.root["model"]
        if hasattr(d.root, "_body"):  # the real tomlkit document, not the plain copy
            d.root._body[:] = []

    with pytest.raises(ValueError, match="comment"):
        shared_file.apply_edit(doc, CODEX, drop_everything)


@pytest.mark.parametrize(
    ("op", "target"),
    [
        (mcp({"command": "x", "n": float("nan")}), ".mcp.json"),
        (toml_op({"command": "x", "n": None}), CODEX),
    ],
)
def test_payload_problem_advice_names_the_payload_not_a_table_header(
    tmp_path: Path, op: Op, target: str
) -> None:
    with pytest.raises(PlanError) as exc:
        apply(tmp_path, [op])
    reason = exc.value.conflicts[0].reason
    assert "payload" in reason and "table header" not in reason
    assert not (tmp_path / target).exists()


def test_json_layout_problem_has_no_table_header_advice(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": []}))
    with pytest.raises(PlanError) as exc:
        apply(tmp_path, [mcp()])
    assert "table header" not in exc.value.conflicts[0].reason


def test_nan_in_the_users_toml_does_not_block_edits(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / CODEX).write_text("x = nan\n[other]\ny = [nan, 1.0]\n", encoding="utf-8")
    apply(tmp_path, [toml_op()])
    assert "[mcp_servers.github]" in (tmp_path / CODEX).read_text()
    apply(tmp_path, [toml_op(SERVER2)])
    apply(tmp_path, [], prune=True)
    assert "x = nan" in (tmp_path / CODEX).read_text()
