"""Commands become explicit-invocation skills, with opt-in native reuse."""

from pathlib import Path

import pytest
import yaml

from agent_sync.context import Context
from agent_sync.kinds.commands import plan_commands
from agent_sync.manifest import ManifestEntry
from agent_sync.state import State


def context(tmp_path: Path) -> Context:
    source = tmp_path / "source"
    source.mkdir()
    return Context(tmp_path / "project", source, tmp_path / "cache", False, False, False, State(()))


def write(ctx: Context, path: str, body: str) -> Path:
    assert ctx.personal_dir is not None
    target = ctx.personal_dir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    return target


def entry(name: str = "plan") -> ManifestEntry:
    return ManifestEntry("commands", frozenset({"claude", "codex"}), "personal", None, name)


def test_generates_explicit_skill_preserving_command(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    body = "Plan $ARGUMENTS without executing !`pwd`.\n"
    write(ctx, "commands/plan.md", "---\ndescription: Make a plan\nmodel: sonnet\n---\n" + body)
    result = plan_commands([entry()], ctx)
    op = result.ops[0]
    assert (op.kind, op.target, op.harness) == ("commands", ".agents/skills/plan", None)
    assert set(op.payload) == {"SKILL.md", "agents/openai.yaml"}
    content = op.payload["SKILL.md"].decode()
    metadata = yaml.safe_load(content.split("---", 2)[1])
    assert metadata == {
        "name": "plan",
        "description": "Make a plan",
        "model": "sonnet",
        "disable-model-invocation": True,
        "user-invocable": True,
    }
    assert body in content
    assert "substitution" in content
    assert yaml.safe_load(op.payload["agents/openai.yaml"])["policy"] == {
        "allow_implicit_invocation": False
    }
    assert result.diagnostics
    assert not ctx.project.exists()
    assert plan_commands([entry(), entry()], ctx).ops == result.ops


def test_only_explicit_mapping_reuses_skill_and_all_resources(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    write(ctx, "commands/plan.md", "---\nskill: native-plan\n---\nOld command.\n")
    write(
        ctx,
        "skills/native-plan/SKILL.md",
        "---\nname: native-plan\ndescription: Native\nlicense: MIT\n---\nNative body.\n",
    )
    write(ctx, "skills/native-plan/scripts/run.py", "print(1)\n")
    write(
        ctx,
        "skills/native-plan/agents/openai.yaml",
        "interface:\n  display_name: Planner\npolicy:\n  allow_implicit_invocation: true\n",
    )
    tree = plan_commands([entry()], ctx).ops[0].payload
    assert tree["scripts/run.py"] == b"print(1)\n"
    assert b"Native body." in tree["SKILL.md"] and b"Old command." not in tree["SKILL.md"]
    assert b"license: MIT" in tree["SKILL.md"]
    assert yaml.safe_load(tree["agents/openai.yaml"]) == {
        "interface": {"display_name": "Planner"},
        "policy": {"allow_implicit_invocation": False},
    }
    write(ctx, "commands/plan.md", "Command only.\n")
    write(ctx, "skills/plan/SKILL.md", "Unrelated skill.\n")
    assert b"Command only." in plan_commands([entry()], ctx).ops[0].payload["SKILL.md"]


@pytest.mark.parametrize(
    "content",
    [
        "---\nname: other\n---\nBody",
        "---\ndescription: a\ndescription: b\n---\nBody",
        "---\n- invalid\n---\nBody",
        "   ",
        "---\nskill: missing\n---\nBody",
    ],
)
def test_invalid_commands_fail(tmp_path: Path, content: str) -> None:
    ctx = context(tmp_path)
    write(ctx, "commands/plan.md", content)
    with pytest.raises(ValueError):
        plan_commands([entry()], ctx)


def test_reused_skill_rejects_external_resource_symlink(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    write(ctx, "commands/plan.md", "---\nskill: reusable\n---\nBody")
    skill = write(ctx, "skills/reusable/SKILL.md", "Native body.").parent
    external = tmp_path / "secret"
    external.write_text("secret")
    (skill / "secret").symlink_to(external)
    with pytest.raises(ValueError):
        plan_commands([entry()], ctx)


def test_all_sorted_and_duplicate_roots_rejected(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    write(ctx, "commands/z.md", "Z")
    write(ctx, ".claude/commands/a.md", "A")
    assert [op.target for op in plan_commands([entry("all")], ctx).ops] == [
        ".agents/skills/a",
        ".agents/skills/z",
    ]
    write(ctx, "commands/a.md", "Duplicate")
    with pytest.raises(ValueError):
        plan_commands([entry("all")], ctx)


def test_unknown_selector_and_missing_cache(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    write(ctx, "commands/other.md", "Other command")
    with pytest.raises(ValueError, match="Unknown command"):
        plan_commands([entry()], ctx)
    remote = entry()._replace(source="github.com", repo="owner/repo")
    result = plan_commands([remote], ctx)
    assert result.missing == [remote]
    assert result.ops == []


def test_nested_commands_are_not_flattened(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    write(ctx, "commands/nested/plan.md", "Nested")
    assert plan_commands([entry("all")], ctx).ops == []


def test_case_only_command_collision_fails(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    write(ctx, "commands/Plan.md", "First")
    write(ctx, ".claude/commands/plan.md", "Second")
    with pytest.raises(ValueError):
        plan_commands([entry("all")], ctx)


def test_reused_skill_rejects_invalid_policy(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    write(ctx, "commands/plan.md", "---\nskill: reusable\n---\nBody")
    write(ctx, "skills/reusable/SKILL.md", "Native body.")
    write(ctx, "skills/reusable/agents/openai.yaml", "policy: false\n")
    with pytest.raises(ValueError):
        plan_commands([entry()], ctx)


def test_warns_about_unknown_metadata_and_relative_resources(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    write(ctx, "commands/plan.md", "---\nunknown: dropped\n---\nRead scripts/run.py")
    result = plan_commands([entry()], ctx)
    assert any("unknown" in message for message in result.diagnostics)
    assert any("source-relative" in message for message in result.diagnostics)
    assert b"unknown:" not in result.ops[0].payload["SKILL.md"]


def test_reused_skill_rejects_duplicate_native_policy_keys(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    write(ctx, "commands/plan.md", "---\nskill: reusable\n---\nBody")
    write(ctx, "skills/reusable/SKILL.md", "Native body.")
    write(ctx, "skills/reusable/agents/openai.yaml", "interface: {}\ninterface: {}\n")
    with pytest.raises(ValueError):
        plan_commands([entry()], ctx)


def test_empty_all_commands_reports_inventory(tmp_path: Path) -> None:
    result = plan_commands([entry("all")], context(tmp_path))
    assert result.ops == []
    assert any("No commands" in note for note in result.diagnostics)
