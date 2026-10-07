"""Hooks and MCP servers through the public CLI: output, writes, trust, refusals and prune."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import tomlkit
from conftest import Sandbox, snapshot

LONG = "node ${CLAUDE_PLUGIN_ROOT}/scripts/hooks/desktop-notify.js stop:desktop-notify --with --many --flags"
EXCLUDE = ".git/info/exclude"

MANIFEST = """hooks:
  common:
    - name: guard
      event: Stop
      matcher: ".*"
      command: node {source}/guard.js --really-long-argument-list --another-argument --third
      source: personal
  claude:
    - name: desktop-notify
      source: personal
      pick:
        event: Stop
        contains: desktop-notify
  codex:
    - name: session-start
      source: personal
      pick:
        id: session:start
mcp:
  common:
    - personal:docs
    - name: inline
      command: npx
      args: [-y, some-server]
      env: [API_TOKEN]
"""


def source(sandbox: Sandbox) -> Path:
    root = sandbox.root / "personal"
    (root / "hooks").mkdir(parents=True)
    (root / ".codex-plugin").mkdir()
    (root / ".codex").mkdir()
    (root / "hooks/hooks.json").write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": LONG}]},
    ]}}))
    (root / ".codex-plugin/plugin.json").write_text(json.dumps({"hooks": "./hooks/codex-hooks.json"}))
    (root / "hooks/codex-hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"id": "session:start", "matcher": "startup",
         "hooks": [{"type": "command", "command": "node ${PLUGIN_ROOT}/s.js"}]},
    ]}}))
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "docs": {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/server.js"]},
    }}))
    (root / ".codex/config.toml").write_text('[mcp_servers.docs]\ncommand = "node"\nargs = ["server.js"]\n')
    return root


def init_git(sandbox: Sandbox) -> None:
    subprocess.run(["git", "-C", str(sandbox.project), "init", "-q"], env=sandbox.env, check=True)


def exclude_text(sandbox: Sandbox) -> str:
    return (sandbox.project / EXCLUDE).read_text()


def test_dry_run_prints_every_command_in_full_and_writes_nothing(sandbox: Sandbox) -> None:
    root = source(sandbox)
    sandbox.write_config(MANIFEST)
    before = snapshot(sandbox.project)
    result = sandbox.sync("--personal-dir", str(root))
    assert result.returncode == 0, result.stderr
    assert snapshot(sandbox.project) == before
    out = result.stdout
    assert f"Would add hook claude Stop [matcher: .*] guard: node {root}/guard.js --really-long" in out
    assert "--another-argument --third" in out
    assert f"Would add hook claude Stop desktop-notify: export CLAUDE_PLUGIN_ROOT={root}; {LONG}" in out
    assert f"Would add hook codex SessionStart [matcher: startup] session-start: export PLUGIN_ROOT={root}; node ${{PLUGIN_ROOT}}/s.js" in out
    assert "Would add MCP server docs (claude) -> .mcp.json" in out
    assert "Would add MCP server docs (codex) -> .codex/config.toml" in out
    assert "Would add MCP server inline (claude) -> .mcp.json" in out


def test_apply_writes_all_four_files_and_a_second_apply_changes_nothing(sandbox: Sandbox) -> None:
    root = source(sandbox)
    sandbox.write_config(MANIFEST)
    result = sandbox.sync("--personal-dir", str(root), "--apply")
    assert result.returncode == 0, result.stderr
    assert "Add hook claude Stop desktop-notify" in result.stdout
    claude = json.loads((sandbox.project / ".claude/settings.local.json").read_text())
    commands = [h["command"] for g in claude["hooks"]["Stop"] for h in g["hooks"]]
    assert f"export CLAUDE_PLUGIN_ROOT={root}; {LONG}" in commands
    codex = json.loads((sandbox.project / ".codex/hooks.json").read_text())
    assert codex["hooks"]["SessionStart"][0]["hooks"][0]["command"].startswith("export PLUGIN_ROOT=")
    mcp = json.loads((sandbox.project / ".mcp.json").read_text())["mcpServers"]
    assert mcp["docs"]["args"] == [f"{root}/server.js"]
    assert mcp["inline"]["env"] == {"API_TOKEN": "${API_TOKEN}"}
    toml = tomlkit.parse((sandbox.project / ".codex/config.toml").read_text())
    assert toml["mcp_servers"]["docs"]["command"] == "node"
    assert "inline" in toml["mcp_servers"]
    before = snapshot(sandbox.project)
    again = sandbox.sync("--personal-dir", str(root), "--apply")
    assert again.returncode == 0, again.stderr
    assert "0 changes" in again.stdout
    assert snapshot(sandbox.project) == before


def test_codex_trust_reminder_appears_for_codex_hooks_and_mcp(sandbox: Sandbox) -> None:
    root = source(sandbox)
    reminder = "project trust"
    sandbox.write_config("hooks:\n  codex:\n    - {name: h, event: Stop, command: echo hi}\n")
    assert reminder in sandbox.sync("--personal-dir", str(root)).stdout
    sandbox.write_config("mcp:\n  codex:\n    - personal:docs\n")
    assert reminder in sandbox.sync("--personal-dir", str(root)).stdout
    sandbox.write_config("hooks:\n  claude:\n    - {name: h, event: Stop, command: echo hi}\n")
    assert reminder not in sandbox.sync("--personal-dir", str(root)).stdout


def test_a_tracked_hook_target_fails_and_nothing_is_written(sandbox: Sandbox) -> None:
    root = source(sandbox)
    init_git(sandbox)
    (sandbox.project / ".claude").mkdir()
    (sandbox.project / ".claude/settings.local.json").write_text("{}\n")
    subprocess.run(["git", "-C", str(sandbox.project), "add", "-f", ".claude/settings.local.json"],
                   env=sandbox.env, check=True)
    sandbox.write_config(MANIFEST)
    before = snapshot(sandbox.project)
    result = sandbox.sync("--personal-dir", str(root), "--apply")
    assert result.returncode == 1
    assert "tracked by git" in result.stderr
    assert snapshot(sandbox.project) == before


def test_a_literal_secret_fails_and_nothing_is_written(sandbox: Sandbox) -> None:
    root = source(sandbox)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "docs": {"command": "node", "env": {"API_KEY": "sk-live-123"}},
    }}))
    (root / ".codex/config.toml").write_text("")
    sandbox.write_config("hooks:\n  claude:\n    - {name: h, event: Stop, command: echo hi}\nmcp:\n  codex:\n    - personal:docs\n")
    before = snapshot(sandbox.project)
    result = sandbox.sync("--personal-dir", str(root), "--apply")
    assert result.returncode == 1
    assert "literal" in result.stderr
    assert "sk-live-123" not in result.stdout + result.stderr
    assert snapshot(sandbox.project) == before


def test_prune_removes_only_agent_sync_entries_and_the_exclude_lines(sandbox: Sandbox) -> None:
    root = source(sandbox)
    init_git(sandbox)
    (sandbox.project / ".claude").mkdir()
    own = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}, "model": "x"}
    (sandbox.project / ".claude/settings.local.json").write_text(json.dumps(own))
    (sandbox.project / ".mcp.json").write_text(json.dumps({"mcpServers": {"mine": {"command": "m"}}}))
    sandbox.write_config(MANIFEST)
    assert sandbox.sync("--personal-dir", str(root), "--apply").returncode == 0
    block = exclude_text(sandbox)
    for created in (".codex/hooks.json", ".codex/config.toml"):
        assert created in block
    assert ".mcp.json" not in block  # the user's file is never listed
    sandbox.write_config("{}\n")
    result = sandbox.sync("--personal-dir", str(root), "--apply", "--prune")
    assert result.returncode == 0, result.stderr
    assert "Remove hook claude Stop" in result.stdout
    assert json.loads((sandbox.project / ".claude/settings.local.json").read_text()) == own
    assert json.loads((sandbox.project / ".mcp.json").read_text()) == {"mcpServers": {"mine": {"command": "m"}}}
    assert not (sandbox.project / ".codex/hooks.json").exists()
    assert not (sandbox.project / ".codex/config.toml").exists()
    for created in (".codex/hooks.json", ".codex/config.toml"):
        assert created not in exclude_text(sandbox)


def test_codex_hook_trust_line_only_when_codex_hooks_change(sandbox: Sandbox) -> None:
    root = source(sandbox)
    line = "Codex skips new or changed hooks until you review and trust them (/hooks in Codex)"
    sandbox.write_config("hooks:\n  codex:\n    - {name: h, event: Stop, command: echo hi}\n")
    assert line in sandbox.sync("--personal-dir", str(root)).stdout
    assert "project trust" in sandbox.sync("--personal-dir", str(root)).stdout
    sandbox.sync("--personal-dir", str(root), "--apply")
    assert line not in sandbox.sync("--personal-dir", str(root)).stdout
    sandbox.write_config("hooks:\n  claude:\n    - {name: h, event: Stop, command: echo hi}\n")
    assert line not in sandbox.sync("--personal-dir", str(root)).stdout
    sandbox.write_config("mcp:\n  codex:\n    - personal:docs\n")
    assert line not in sandbox.sync("--personal-dir", str(root)).stdout


def test_changing_a_hook_event_moves_it_in_one_run(sandbox: Sandbox) -> None:
    root = source(sandbox)
    sandbox.write_config("hooks:\n  claude:\n    - {name: notify, event: Stop, command: echo hi}\n")
    assert sandbox.sync("--personal-dir", str(root), "--apply").returncode == 0
    sandbox.write_config("hooks:\n  claude:\n    - {name: notify, event: SessionEnd, command: echo hi}\n")
    dry = sandbox.sync("--personal-dir", str(root))
    assert "Would remove hook claude Stop notify" in dry.stdout
    assert "Would add hook claude SessionEnd notify" in dry.stdout
    assert sandbox.sync("--personal-dir", str(root), "--apply").returncode == 0
    data = json.loads((sandbox.project / ".claude/settings.local.json").read_text())
    assert list(data["hooks"].get("Stop", [])) == []
    assert len(data["hooks"]["SessionEnd"]) == 1


def test_help_mentions_hooks_and_mcp_servers(sandbox: Sandbox) -> None:
    out = sandbox.sync("--help").stdout
    assert "hooks" in out and "MCP servers" in out


def test_mcp_lines_show_what_the_server_runs_but_never_env_values(sandbox: Sandbox) -> None:
    root = source(sandbox)
    sandbox.write_config(
        "mcp:\n  claude:\n    - name: inline\n      command: npx\n      args: [-y, some-server]\n"
        "      env: [API_TOKEN]\n    - {name: remote, url: 'https://example.com/mcp'}\n"
    )
    out = sandbox.sync("--personal-dir", str(root)).stdout
    assert "Would add MCP server inline (claude) -> .mcp.json: npx -y some-server" in out
    assert "Would add MCP server remote (claude) -> .mcp.json: https://example.com/mcp" in out
    assert "API_TOKEN" not in out


def test_exclude_block_is_stable_for_a_created_file_that_survives_a_prune(sandbox: Sandbox) -> None:
    root = source(sandbox)
    init_git(sandbox)
    sandbox.write_config("mcp:\n  claude:\n    - {name: inline, command: npx, args: [-y, s]}\n")
    sandbox.sync("--personal-dir", str(root), "--apply")
    assert "/.mcp.json" in exclude_text(sandbox)
    path = sandbox.project / ".mcp.json"
    data = json.loads(path.read_text())
    data["mine"] = 1
    path.write_text(json.dumps(data))
    sandbox.write_config("skills: {}\n")
    pruned = sandbox.sync("--personal-dir", str(root), "--apply", "--prune")
    assert pruned.returncode == 0, pruned.stderr
    assert path.exists()
    assert "/.mcp.json" in exclude_text(sandbox)
    again = sandbox.sync("--personal-dir", str(root), "--apply")
    assert "0 changes" in again.stdout
    assert "Update .git/info/exclude" not in again.stdout
    assert "/.mcp.json" in exclude_text(sandbox)


def test_settings_local_stays_excluded_after_hooks_are_pruned(sandbox: Sandbox) -> None:
    root = source(sandbox)
    init_git(sandbox)
    sandbox.write_config("hooks:\n  claude:\n    - {name: h, event: Stop, command: echo hi}\n")
    sandbox.sync("--personal-dir", str(root), "--apply")
    path = sandbox.project / ".claude/settings.local.json"
    data = json.loads(path.read_text())
    data["permissions"] = {"allow": ["Bash(ls)"]}
    path.write_text(json.dumps(data))
    sandbox.write_config("skills: {}\n")
    sandbox.sync("--personal-dir", str(root), "--apply", "--prune")
    assert json.loads(path.read_text())["permissions"] == {"allow": ["Bash(ls)"]}
    assert "/.claude/settings.local.json" in exclude_text(sandbox)
    for _ in range(2):
        again = sandbox.sync("--personal-dir", str(root), "--apply")
        assert "Update .git/info/exclude" not in again.stdout
        assert "/.claude/settings.local.json" in exclude_text(sandbox)


def test_a_moved_hook_is_not_counted_as_pruned(sandbox: Sandbox) -> None:
    root = source(sandbox)
    sandbox.write_config("hooks:\n  claude:\n    - {name: n, event: Stop, command: echo hi}\n")
    sandbox.sync("--personal-dir", str(root), "--apply")
    sandbox.write_config("hooks:\n  claude:\n    - {name: n, event: SessionEnd, command: echo hi}\n")
    out = sandbox.sync("--personal-dir", str(root), "--apply", "--prune").stdout
    assert "Remove hook claude Stop n" in out
    assert "pruned" not in out
    sandbox.write_config("skills: {}\n")
    assert "1 pruned" in sandbox.sync("--personal-dir", str(root), "--apply", "--prune").stdout
