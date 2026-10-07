"""Hooks planner: validation, native picks, ops, and engine round trips."""

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import Sandbox

from agent_sync.context import Context
from agent_sync.engine import run
from agent_sync.kinds.hooks import plan_hooks
from agent_sync.kinds.source_files import KindPlan
from agent_sync.manifest import parse_manifest
from agent_sync.state import State, load_state

CLAUDE_FILE = {
    "hooks": {
        "Stop": [
            {"hooks": [{"type": "command", "command": "node ${CLAUDE_PLUGIN_ROOT}/a.js stop:desktop-notify"}]},
            {"matcher": "Edit", "hooks": [{"type": "command", "command": "node b.js stop:other", "timeout": 5}]},
        ],
        "PreToolUse": [
            {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "node c.js pre:edit"}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "node d.js pre:edit"}]},
        ],
    }
}
CODEX_FILE = {
    "hooks": {
        "SessionStart": [
            {
                "id": "session:start",
                "description": "start",
                "matcher": "startup",
                "hooks": [{"type": "command", "command": "node ${PLUGIN_ROOT}/s.js", "statusMessage": "hi"}],
            }
        ],
        "Stop": [{"id": "multi", "hooks": [{"type": "command", "command": "x"}, {"type": "command", "command": "y"}]}],
    }
}


def put_json(root: Path, rel: str, data: Any) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def src(tmp_path: Path) -> Path:
    root = tmp_path / "personal"
    put_json(root, "hooks/hooks.json", CLAUDE_FILE)
    put_json(root, ".codex-plugin/plugin.json", {"hooks": "./hooks/codex-hooks.json"})
    put_json(root, "hooks/codex-hooks.json", CODEX_FILE)
    return root


def ctx_for(src: Path, project: Path, **kw: Any) -> Context:
    project.mkdir(exist_ok=True)
    return Context(project.resolve(), src, src.parent / "cache", False, False, kw.get("prune", False), kw.get("state", State(())))


def plan(src: Path, hooks: dict[str, list[Any]], **kw: Any) -> KindPlan:
    ctx = ctx_for(src, src.parent / "project", **kw)
    return plan_hooks(parse_manifest({"hooks": hooks}), ctx)


def declared(**kw: Any) -> dict[str, Any]:
    return {"name": "h1", "event": "PreToolUse", "command": "echo hi", **kw}


def test_declared_in_common_makes_two_ops(src: Path) -> None:
    result = plan(src, {"common": [declared(matcher="Bash", timeout=3)]})
    group = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi", "timeout": 3}]}
    assert [(o.target, o.harness) for o in result.ops] == [
        (".claude/settings.local.json", "claude"),
        (".codex/hooks.json", "codex"),
    ]
    for op in result.ops:
        assert (op.action, op.kind, op.container, op.payload) == ("merge", "hooks", "list", group)
        assert op.locator == ("hooks", "PreToolUse", "h1")


def test_declared_without_optionals_and_source_substitution(src: Path) -> None:
    result = plan(src, {"claude": [declared(source="personal", command="node {source}/x.js")]})
    (op,) = result.ops
    assert op.payload == {"hooks": [{"type": "command", "command": f"node {shlex.quote(str(src))}/x.js"}]}


def test_source_path_with_spaces_is_quoted(tmp_path: Path) -> None:
    root = tmp_path / "my src"
    put_json(root, "hooks/hooks.json", CLAUDE_FILE)
    result = plan(root, {"claude": [{"name": "p", "source": "personal", "pick": {"event": "PreToolUse", "contains": "c.js"}}]})
    command = result.ops[0].payload["hooks"][0]["command"]
    assert command.startswith(f"export CLAUDE_PLUGIN_ROOT={shlex.quote(str(root))}; ")
    assert shlex.split(command)[1] == f"CLAUDE_PLUGIN_ROOT={root};"


def test_pick_by_event_contains_claude(src: Path) -> None:
    result = plan(src, {"claude": [{"name": "n", "source": "personal", "pick": {"event": "Stop", "contains": "desktop-notify"}}]})
    (op,) = result.ops
    assert op.locator == ("hooks", "Stop", "n")
    assert op.payload == {
        "hooks": [{"type": "command", "command": f"export CLAUDE_PLUGIN_ROOT={shlex.quote(str(src))}; node ${{CLAUDE_PLUGIN_ROOT}}/a.js stop:desktop-notify"}]
    }


def test_pick_with_matcher_narrows_and_copies_group_matcher(src: Path) -> None:
    pick = {"event": "PreToolUse", "contains": "pre:edit", "matcher": "Bash"}
    (op,) = plan(src, {"claude": [{"name": "n", "source": "personal", "pick": pick}]}).ops
    assert op.payload["matcher"] == "Bash"
    assert op.payload["hooks"][0]["command"].endswith("node d.js pre:edit")


def test_pick_by_id_codex_copies_handler_verbatim(src: Path) -> None:
    (op,) = plan(src, {"codex": [{"name": "s", "source": "personal", "pick": {"id": "session:start"}}]}).ops
    assert (op.target, op.harness, op.locator) == (".codex/hooks.json", "codex", ("hooks", "SessionStart", "s"))
    assert op.payload == {
        "matcher": "startup",
        "hooks": [{"type": "command", "command": f"export PLUGIN_ROOT={shlex.quote(str(src))}; node ${{PLUGIN_ROOT}}/s.js", "statusMessage": "hi"}],
    }
    assert "id" not in op.payload and "description" not in op.payload


def test_claude_plugin_json_hooks_path_used(src: Path) -> None:
    put_json(src, ".claude-plugin/plugin.json", {"hooks": "custom/h.json"})
    put_json(src, "custom/h.json", {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "zzz"}]}]}})
    (op,) = plan(src, {"claude": [{"name": "n", "source": "personal", "pick": {"event": "Stop", "contains": "zzz"}}]}).ops
    assert op.payload["hooks"][0]["command"].endswith("; zzz")


def test_codex_fallback_file(src: Path) -> None:
    (src / ".codex-plugin/plugin.json").unlink()
    put_json(src, ".codex/hooks.json", CODEX_FILE)
    assert len(plan(src, {"codex": [{"name": "s", "source": "personal", "pick": {"id": "session:start"}}]}).ops) == 1


@pytest.mark.parametrize(
    ("pick", "match"),
    [
        ({"event": "Stop", "contains": "nomatch"}, "no handler"),
        ({"event": "PreToolUse", "contains": "pre:edit"}, "c.js"),
        ({"id": "nope"}, "no handler"),
    ],
)
def test_pick_zero_or_many_lists_candidates(src: Path, pick: dict[str, str], match: str) -> None:
    with pytest.raises(ValueError, match=match) as info:
        plan(src, {"claude": [{"name": "n", "source": "personal", "pick": pick}]})
    assert "hooks.claude hook 'n'" in str(info.value)


def test_multiple_matches_error_lists_each_candidate(src: Path) -> None:
    with pytest.raises(ValueError, match="d.js") as info:
        plan(src, {"claude": [{"name": "n", "source": "personal", "pick": {"event": "PreToolUse", "contains": "pre:edit"}}]})
    assert "Edit|Write" in str(info.value)


def test_pick_id_with_two_handlers_is_error(src: Path) -> None:
    with pytest.raises(ValueError, match="2 handlers|exactly one"):
        plan(src, {"codex": [{"name": "n", "source": "personal", "pick": {"id": "multi"}}]})


def test_pick_in_common_rejected(src: Path) -> None:
    with pytest.raises(ValueError, match="common"):
        plan(src, {"common": [{"name": "n", "source": "personal", "pick": {"id": "x"}}]})


def test_native_file_missing_or_escaping(src: Path) -> None:
    (src / "hooks/hooks.json").unlink()
    with pytest.raises(ValueError, match="hook file"):
        plan(src, {"claude": [{"name": "n", "source": "personal", "pick": {"id": "x"}}]})
    put_json(src, ".claude-plugin/plugin.json", {"hooks": "../evil.json"})
    with pytest.raises(ValueError, match="escapes"):
        plan(src, {"claude": [{"name": "n", "source": "personal", "pick": {"id": "x"}}]})


@pytest.mark.parametrize(
    ("section", "item", "match"),
    [
        ("claude", {"event": "Stop", "command": "x"}, "name"),
        ("claude", {"name": "bad name!", "event": "Stop", "command": "x"}, "name"),
        ("claude", {"name": "a", "command": "x"}, "event"),
        ("claude", {"name": "a", "event": "Stop"}, "command"),
        ("claude", {"name": "a", "event": "Stop", "command": 3}, "command"),
        ("claude", {"name": "a", "event": "Nope", "command": "x"}, "event"),
        ("codex", {"name": "a", "event": "TaskCreated", "command": "x"}, "event"),
        ("common", {"name": "a", "event": "TaskCreated", "command": "x"}, "event"),
        ("claude", {"name": "a", "event": "Stop", "command": "x", "matcher": 1}, "matcher"),
        ("claude", {"name": "a", "event": "Stop", "command": "x", "timeout": 0}, "timeout"),
        ("claude", {"name": "a", "event": "Stop", "command": "x", "timeout": True}, "timeout"),
        ("claude", {"name": "a", "event": "Stop", "command": "x", "timeout": "5"}, "timeout"),
        ("claude", {"name": "a", "event": "Stop", "command": "x", "bogus": 1}, "bogus"),
        ("claude", {"name": "a", "event": "Stop", "command": "{source}/x"}, "source"),
        ("claude", {"name": "a", "event": "Stop", "command": "x", "source": "github.com:bad repo"}, "source"),
        ("claude", {"name": "a", "event": "Stop", "command": "x", "source": "npx"}, "source"),
        ("claude", {"name": "a", "source": "personal", "pick": {}}, "pick"),
        ("claude", {"name": "a", "source": "personal", "pick": {"id": "x", "event": "Stop"}}, "pick"),
        ("claude", {"name": "a", "source": "personal", "pick": {"event": "Stop"}}, "contains"),
        ("claude", {"name": "a", "source": "personal", "pick": {"contains": "x"}}, "event"),
        ("claude", {"name": "a", "source": "personal", "pick": {"id": "x", "zzz": 1}}, "zzz"),
        ("claude", {"name": "a", "pick": {"id": "x"}}, "source"),
        ("claude", {"name": "a", "source": "personal", "pick": {"id": "x"}, "command": "y"}, "command"),
        ("claude", {"name": "a", "source": "personal", "pick": "x"}, "pick"),
    ],
)
def test_validation_errors_name_hook(src: Path, section: str, item: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match) as info:
        plan(src, {section: [item]})
    assert section in str(info.value)


def test_duplicate_name_per_harness_rejected_but_allowed_across(src: Path) -> None:
    with pytest.raises(ValueError, match="Duplicate|duplicate"):
        plan(src, {"claude": [declared(), declared(event="Stop")]})
    with pytest.raises(ValueError, match="Duplicate|duplicate"):
        plan(src, {"common": [declared()], "claude": [declared(event="Stop")]})
    assert len(plan(src, {"claude": [declared()], "codex": [declared()]}).ops) == 2


def test_missing_source_in_dry_run_reported(tmp_path: Path) -> None:
    src = tmp_path / "personal"
    src.mkdir()
    item = {"name": "g", "source": "github.com:o/r", "event": "Stop", "command": "x {source}"}
    result = plan(src, {"claude": [item]})
    assert result.ops == []
    assert [(m.source, m.repo) for m in result.missing] == [("github.com", "o/r")]


def test_missing_personal_source_names_the_source(tmp_path: Path) -> None:
    ctx = Context((tmp_path / "p").resolve(), None, tmp_path / "cache", False, False, False, State(()))
    entries = parse_manifest({"hooks": {"claude": [declared(source="github.com:o/r")]}})
    (missing,) = plan_hooks(entries, ctx).missing
    assert (missing.source, missing.repo) == ("github.com", "o/r")


def test_tracked_target_is_error(src: Path, tmp_path: Path) -> None:
    project = src.parent / "project"
    (project / ".codex").mkdir(parents=True)
    (project / ".codex/hooks.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "add", "-f", ".codex/hooks.json"], cwd=project, check=True)
    with pytest.raises(ValueError, match="tracks|tracked") as info:
        plan(src, {"common": [declared()]})
    assert ".codex/hooks.json" in str(info.value) and "cache" in str(info.value)
    assert (project / ".codex/hooks.json").read_text(encoding="utf-8") == "{}"
    assert not (project / ".claude").exists()


def apply_plan(src: Path, hooks: dict[str, list[Any]], *, prune: bool = False) -> Any:
    project = src.parent / "project"
    project.mkdir(exist_ok=True)
    result = plan(src, hooks, state=load_state(project), prune=prune)
    return run(project.resolve(), result.ops, apply=True, prune=prune, force=False)


def test_round_trip_preserves_user_content_and_is_idempotent(src: Path) -> None:
    project = src.parent / "project"
    (project / ".claude").mkdir(parents=True)
    user: dict[str, Any] = {
        "model": "x",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}], "Custom": []},
        "permissions": {"allow": ["a"]},
    }
    target = project / ".claude/settings.local.json"
    target.write_text(json.dumps(user), encoding="utf-8")
    hooks = {"claude": [declared(event="Stop", command="ours")]}
    first = apply_plan(src, hooks)
    assert first.changes
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["model"] == "x" and data["permissions"] == {"allow": ["a"]}
    assert data["hooks"]["Stop"][0] == user["hooks"]["Stop"][0]
    assert data["hooks"]["Stop"][1]["hooks"][0]["command"] == "ours"
    assert data["hooks"]["Custom"] == []
    assert apply_plan(src, hooks).changes == ()


def test_prune_removes_only_ours_and_created_file(src: Path) -> None:
    project = src.parent / "project"
    both = {"common": [declared(name="a"), declared(name="b", event="Stop")]}
    apply_plan(src, both)
    target = project / ".codex/hooks.json"
    data = json.loads(target.read_text(encoding="utf-8"))
    data["hooks"]["Stop"].insert(0, {"hooks": [{"type": "command", "command": "mine"}]})
    target.write_text(json.dumps(data), encoding="utf-8")
    apply_plan(src, {"common": [declared(name="a")]}, prune=True)
    after = json.loads(target.read_text(encoding="utf-8"))
    assert after["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "mine"}]}]
    assert len(after["hooks"]["PreToolUse"]) == 1
    claude = json.loads((project / ".claude/settings.local.json").read_text(encoding="utf-8"))
    assert "Stop" not in claude["hooks"] or claude["hooks"]["Stop"] == []
    apply_plan(src, {}, prune=True)
    assert not (project / ".claude/settings.local.json").exists()


MISSING_CONFIG = """\
hooks:
  claude:
    - {name: a, source: "github.com:affaan-m/ECC", pick: {id: x}}
    - {name: b, source: "github.com:affaan-m/ECC", pick: {id: y}}
skills:
  common:
    - github.com:owner/repo:one
    - github.com:owner/repo:two
"""


def test_cli_missing_hook_source_names_repo_and_prints_each_source_once(sandbox: Sandbox) -> None:
    sandbox.write_config(MISSING_CONFIG)
    out = sandbox.sync().stdout
    lines = [ln for ln in out.splitlines() if ln.startswith("Missing source (run with --apply)")]
    assert lines == [
        "Missing source (run with --apply): github.com/owner/repo",
        "Missing source (run with --apply): github.com/affaan-m/ECC",
    ]
    assert "/None" not in out
    assert "2 missing" in out


def test_event_change_replaces_the_old_group_without_prune(src: Path) -> None:
    project = src.parent / "project"
    apply_plan(src, {"claude": [declared(name="notify", event="Stop", command="ours")]})
    apply_plan(src, {"claude": [declared(name="notify", event="SessionEnd", command="ours")]})
    data = json.loads((project / ".claude/settings.local.json").read_text(encoding="utf-8"))
    assert list(data["hooks"].get("Stop", [])) == []
    assert len(data["hooks"]["SessionEnd"]) == 1
    assert apply_plan(src, {"claude": [declared(name="notify", event="SessionEnd", command="ours")]}).changes == ()


def test_upstream_move_of_picked_group_replaces_old_group(src: Path) -> None:
    project = src.parent / "project"
    pick = {"codex": [{"name": "s", "source": "personal", "pick": {"id": "session:start"}}]}
    apply_plan(src, pick)
    moved = json.loads(json.dumps(CODEX_FILE))
    moved["hooks"]["SessionEnd"] = moved["hooks"].pop("SessionStart")
    put_json(src, "hooks/codex-hooks.json", moved)
    apply_plan(src, pick)
    data = json.loads((project / ".codex/hooks.json").read_text(encoding="utf-8"))
    assert list(data["hooks"].get("SessionStart", [])) == []
    assert len(data["hooks"]["SessionEnd"]) == 1


def test_user_group_in_old_event_survives_event_change(src: Path) -> None:
    project = src.parent / "project"
    apply_plan(src, {"claude": [declared(name="notify", event="Stop", command="ours")]})
    target = project / ".claude/settings.local.json"
    data = json.loads(target.read_text(encoding="utf-8"))
    data["hooks"]["Stop"].insert(0, {"hooks": [{"type": "command", "command": "mine"}]})
    target.write_text(json.dumps(data), encoding="utf-8")
    apply_plan(src, {"claude": [declared(name="notify", event="SessionEnd", command="ours")]})
    after = json.loads(target.read_text(encoding="utf-8"))
    assert after["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "mine"}]}]


PREFIX = "node ${CLAUDE_PLUGIN_ROOT}/scripts/hooks/run-with-flags.js " + "very-long-shared-prefix " * 5
ECC_FILE = {"hooks": {"Stop": [
    {"id": f"stop:{n}", "matcher": "*", "hooks": [{"type": "command", "command": PREFIX + f"{n} scripts/{n}.js"}]}
    for n in ("alpha", "beta", "gamma")
], "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "only-one"}]}]}}


def test_multiple_matches_show_id_matcher_and_command_tail(src: Path) -> None:
    put_json(src, "hooks/hooks.json", ECC_FILE)
    with pytest.raises(ValueError, match="exactly one") as info:
        plan(src, {"claude": [{"name": "n", "source": "personal", "pick": {"event": "Stop", "contains": "run-with-flags"}}]})
    text = str(info.value)
    for n in ("alpha", "beta", "gamma"):
        assert f"id='stop:{n}'" in text and f"scripts/{n}.js" in text
    assert "matcher='*'" in text


def test_zero_matches_list_handlers_in_that_event(src: Path) -> None:
    put_json(src, "hooks/hooks.json", ECC_FILE)
    with pytest.raises(ValueError, match="no handler in Stop contains 'zzz'") as info:
        plan(src, {"claude": [{"name": "n", "source": "personal", "pick": {"event": "Stop", "contains": "zzz"}}]})
    assert "scripts/beta.js" in str(info.value)
    with pytest.raises(ValueError, match="no handler in Setup contains 'zzz'"):
        plan(src, {"claude": [{"name": "n", "source": "personal", "pick": {"event": "Setup", "contains": "zzz"}}]})


@pytest.mark.parametrize(
    ("handler", "match"),
    [
        ({"type": "command", "command": "x-node", "args": ["x.js"]}, "args"),
        ({"type": "command", "command": "x.ps1", "shell": "powershell"}, "powershell"),
    ],
)
def test_pick_of_exec_or_powershell_handler_is_refused(src: Path, handler: dict[str, Any], match: str) -> None:
    put_json(src, "hooks/hooks.json", {"hooks": {"Stop": [{"hooks": [handler]}]}})
    with pytest.raises(ValueError, match=match) as info:
        plan(src, {"claude": [{"name": "n", "source": "personal", "pick": {"event": "Stop", "contains": "x"}}]})
    assert "'n'" in str(info.value)


def test_posix_shell_handler_is_accepted(src: Path) -> None:
    put_json(src, "hooks/hooks.json", {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "shell": "bash"}]}]}})
    (op,) = plan(src, {"claude": [{"name": "n", "source": "personal", "pick": {"event": "Stop", "contains": "x"}}]}).ops
    assert op.payload["hooks"][0]["shell"] == "bash"


def test_duplicate_names_are_found_before_source_resolution(src: Path) -> None:
    item = {"name": "g", "source": "github.com:o/r", "event": "Stop", "command": "x"}
    with pytest.raises(ValueError, match="duplicate"):
        plan(src, {"claude": [item, dict(item)]})
