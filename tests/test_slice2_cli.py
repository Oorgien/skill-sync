"""Mixed-kind installation, native reuse and ownership through the public CLI."""

from __future__ import annotations

from pathlib import Path

from conftest import Sandbox, snapshot


def source(sandbox: Sandbox) -> Path:
    root = sandbox.root / "personal"
    for folder in ("agents", "commands", "rules/python", "skills/helper"):
        (root / folder).mkdir(parents=True)
    (root / "agents/planner.md").write_text(
        "---\nname: planner\ndescription: Plan work\ntools: Read\n---\nMake a plan.\n"
    )
    (root / "commands/build.md").write_text("---\ndescription: Build\n---\nBuild $ARGUMENTS\n")
    (root / "rules/python/testing.md").write_text("Test changes.\n")
    (root / "skills/helper/SKILL.md").write_text(
        "---\nname: helper\ndescription: Help\n---\nHelp.\n"
    )
    return root


MIXED = """skills:
  common: [personal:helper]
agents:
  common: [personal:planner]
commands:
  common: [personal:build]
rules:
  common: [personal:python]
"""


def test_mixed_install_dry_run_reapply_and_prune(sandbox: Sandbox) -> None:
    root = source(sandbox)
    sandbox.write_config(MIXED)
    before = snapshot(sandbox.project)
    dry = sandbox.sync("--personal-dir", str(root))
    assert dry.returncode == 0, dry.stderr
    assert snapshot(sandbox.project) == before
    result = sandbox.sync("--personal-dir", str(root), "--apply")
    assert result.returncode == 0, result.stderr
    assert (sandbox.project / ".codex/agents/planner.toml").is_file()
    assert (sandbox.project / ".claude/agents/planner.md").read_bytes() == (
        root / "agents/planner.md"
    ).read_bytes()
    assert (sandbox.project / ".agents/skills/build/agents/openai.yaml").is_file()
    assert (sandbox.project / ".claude/rules/python/testing.md").read_text() == "Test changes.\n"
    before = snapshot(sandbox.project)
    again = sandbox.sync("--personal-dir", str(root), "--apply")
    assert again.returncode == 0, again.stderr
    assert "0 changes" in again.stdout
    assert snapshot(sandbox.project) == before
    sandbox.write_config("{}\n")
    prune = sandbox.sync("--apply", "--prune")
    assert prune.returncode == 0, prune.stderr
    assert not (sandbox.project / ".agents/skills/build").exists()
    assert not (sandbox.project / ".codex/agents/planner.toml").exists()
    assert (sandbox.project / ".claude/skills").is_symlink()


def test_invalid_last_kind_prevents_earlier_writes(sandbox: Sandbox) -> None:
    root = source(sandbox)
    sandbox.write_config(MIXED.replace("personal:python", "personal:missing"))
    before = snapshot(sandbox.project)
    result = sandbox.sync("--personal-dir", str(root), "--apply")
    assert result.returncode == 1
    assert snapshot(sandbox.project) == before


def test_commands_cannot_replace_owned_skills_even_with_force(sandbox: Sandbox) -> None:
    root = source(sandbox)
    (root / "commands/helper.md").write_text("Do something.\n")
    sandbox.write_config("skills:\n  common: [personal:helper]\n")
    assert sandbox.sync("--personal-dir", str(root), "--apply").returncode == 0
    sandbox.write_config("commands:\n  common: [personal:helper]\n")
    before = snapshot(sandbox.project)
    result = sandbox.sync("--personal-dir", str(root), "--apply", "--prune", "--force")
    assert result.returncode == 1
    assert "kind" in result.stderr.lower() or "collision" in result.stderr.lower()
    assert snapshot(sandbox.project) == before


def test_missing_source_suppresses_dry_run_pruning(sandbox: Sandbox) -> None:
    root = source(sandbox)
    sandbox.write_config("commands:\n  common: [personal:build]\n")
    assert sandbox.sync("--personal-dir", str(root), "--apply").returncode == 0
    sandbox.write_config("commands:\n  common: [github.com:owner/missing:all]\n")
    result = sandbox.sync("--prune")
    assert result.returncode == 0, result.stderr
    assert "prun" in result.stdout.lower()
    assert "Would remove" not in result.stdout


def test_shared_skill_command_name_collision(sandbox: Sandbox) -> None:
    root = source(sandbox)
    (root / "commands/helper.md").write_text("Help.\n")
    sandbox.write_config(
        "skills:\n  common: [personal:helper]\ncommands:\n  common: [personal:helper]\n"
    )
    before = snapshot(sandbox.project)
    result = sandbox.sync("--personal-dir", str(root), "--apply")
    assert result.returncode == 1
    assert snapshot(sandbox.project) == before


def test_edited_command_tree_requires_force(sandbox: Sandbox) -> None:
    root = source(sandbox)
    sandbox.write_config("commands:\n  common: [personal:build]\n")
    assert sandbox.sync("--personal-dir", str(root), "--apply").returncode == 0
    extra = sandbox.project / ".agents/skills/build/notes.txt"
    extra.write_text("My edit")
    before = snapshot(sandbox.project)
    failed = sandbox.sync("--personal-dir", str(root), "--apply")
    assert failed.returncode == 1
    assert snapshot(sandbox.project) == before
    forced = sandbox.sync("--personal-dir", str(root), "--apply", "--force")
    assert forced.returncode == 0, forced.stderr
    assert not extra.exists()


def test_explicit_command_skill_reuses_resources(sandbox: Sandbox) -> None:
    root = source(sandbox)
    (root / "commands/build.md").write_text("---\nskill: helper\n---\nRun helper.\n")
    (root / "skills/helper/data.txt").write_text("resource")
    sandbox.write_config("commands:\n  common: [personal:build]\n")
    result = sandbox.sync("--personal-dir", str(root), "--apply")
    assert result.returncode == 0, result.stderr
    assert (sandbox.project / ".agents/skills/build/data.txt").read_text() == "resource"
    assert "Help." in (sandbox.project / ".agents/skills/build/SKILL.md").read_text()


def test_legacy_codex_registry_preserves_native_settings(sandbox: Sandbox) -> None:
    root = source(sandbox)
    (root / ".codex/agents").mkdir(parents=True)
    (root / ".codex/config.toml").write_text(
        '[agents.docs_researcher]\ndescription = "Research docs"\n'
        'config_file = "agents/docs-researcher.toml"\n'
    )
    (root / ".codex/agents/docs-researcher.toml").write_text(
        'model = "native-model"\nsandbox_mode = "read-only"\n'
        'developer_instructions = "Check primary sources."\n'
    )
    sandbox.write_config("agents:\n  codex: [personal:docs-researcher]\n")
    result = sandbox.sync("--personal-dir", str(root), "--apply")
    assert result.returncode == 0, result.stderr
    import tomlkit

    data = tomlkit.parse((sandbox.project / ".codex/agents/docs-researcher.toml").read_text())
    assert data["name"] == "docs_researcher"
    assert data["model"] == "native-model"
    assert data["sandbox_mode"] == "read-only"
    assert data["description"] == "Research docs"
