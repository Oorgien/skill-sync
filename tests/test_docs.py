"""The README and the sample manifest must stay valid manifests."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import tomlkit
import yaml

from agent_sync.cli import _describe_merge
from agent_sync.context import Context
from agent_sync.engine import Change
from agent_sync.kinds.hooks import plan_hooks
from agent_sync.kinds.mcp import _SECRET_WORDS, _auth_like, _check_claude, _check_codex, plan_mcp
from agent_sync.kinds.source_files import KindPlan
from agent_sync.manifest import KINDS, load_manifest, parse_manifest
from agent_sync.state import State

ROOT = Path(__file__).resolve().parent.parent
FENCE = re.compile(r"^```ya?ml\n(.*?)^```", re.DOTALL | re.MULTILINE)


def readme_manifests() -> list[str]:
    """Every yaml code block in the README that has any top-level kind key."""
    blocks = FENCE.findall((ROOT / "README.md").read_text(encoding="utf-8"))
    found = []
    for block in blocks:
        data = yaml.safe_load(block)
        if isinstance(data, dict) and any(kind in data for kind in KINDS):
            found.append(block)
    return found


def test_the_readme_has_manifest_examples() -> None:
    assert len(readme_manifests()) >= 2  # the basic and the expanded form


def test_every_manifest_example_in_the_readme_parses() -> None:
    for block in readme_manifests():
        assert parse_manifest(yaml.safe_load(block)), block


def test_the_sample_config_parses() -> None:
    assert load_manifest(ROOT / "config.yaml")


# --- hooks and MCP examples ---------------------------------------------------------------------

README = (ROOT / "README.md").read_text(encoding="utf-8")


def kind_blocks(kind: str) -> list[str]:
    """README yaml blocks whose top-level key is `kind`."""
    found = []
    for block in FENCE.findall(README):
        data = yaml.safe_load(block)
        if isinstance(data, dict) and kind in data:
            found.append(block)
    return found


def _as_personal(value: Any) -> Any:
    """Point the examples' github sources at a local fixture source (the shape is unchanged)."""
    if isinstance(value, str):
        return re.sub(r"^github\.com:acme/tools(:|$)", lambda m: "personal" + (m[1] if m[1] == ":" else ""), value)
    if isinstance(value, list):
        return [_as_personal(v) for v in value]
    if isinstance(value, dict):
        return {k: _as_personal(v) for k, v in value.items()}
    return value


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "personal"
    (root / "hooks").mkdir(parents=True)
    (root / ".codex").mkdir()
    (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "node ${CLAUDE_PLUGIN_ROOT}/n.js stop:desktop-notify"}]},
    ]}}), encoding="utf-8")
    (root / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"id": "session:start", "matcher": "startup", "hooks": [{"type": "command", "command": "node ${PLUGIN_ROOT}/s.js"}]},
    ]}}), encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "browser": {"command": "npx", "args": ["-y", "browser-mcp"]},
    }}), encoding="utf-8")
    (root / ".codex" / "config.toml").write_text(
        '[mcp_servers.docs]\nurl = "https://example.com/mcp"\nbearer_token_env_var = "DOCS_TOKEN"\n',
        encoding="utf-8",
    )
    return root


def plan(kind: str, block: str, source: Path, tmp_path: Path) -> KindPlan:
    entries = [
        e for e in parse_manifest(_as_personal(yaml.safe_load(block))) if e.kind == kind
    ]
    ctx = Context(tmp_path / "project", source, tmp_path / "cache", False, False, False, State(()))
    return (plan_hooks if kind == "hooks" else plan_mcp)(entries, ctx)


def test_the_readme_documents_hooks_and_mcp() -> None:
    assert kind_blocks("hooks"), "README needs an example block with a hooks: key"
    assert kind_blocks("mcp"), "README needs an example block with an mcp: key"
    for heading in ("## Hooks", "## MCP servers", "## Limitations"):
        assert re.search(rf"^{heading}$", README, re.MULTILINE), heading
    assert "not supported yet" not in README
    assert "remain planned" not in README


def test_every_hooks_example_plans(source: Path, tmp_path: Path) -> None:
    names = set()
    for block in kind_blocks("hooks"):
        result = plan("hooks", block, source, tmp_path)
        assert result.ops and not result.missing, block
        names |= {op.locator[-1] for op in result.ops if op.locator}
        for op in result.ops:
            assert op.kind == "hooks" and op.payload["hooks"][0]["command"]
    assert {"edit-guard", "desktop-notify", "session-start", "audit-log"} <= names


def test_every_mcp_example_plans(source: Path, tmp_path: Path) -> None:
    names = set()
    for block in kind_blocks("mcp"):
        result = plan("mcp", block, source, tmp_path)
        assert result.ops and not result.missing, block
        names |= {op.locator[-1] for op in result.ops if op.locator}
    assert {"docs", "context7", "figma", "browser"} <= names


def test_the_readme_native_mcp_examples_follow_the_references_only_rule() -> None:
    toml_blocks = re.findall(r"^```toml\n(.*?)^```", README, re.DOTALL | re.MULTILINE)
    json_blocks = re.findall(r"^```json\n(.*?)^```", README, re.DOTALL | re.MULTILINE)
    assert toml_blocks and json_blocks
    for text in toml_blocks:
        for name, server in tomlkit.parse(text)["mcp_servers"].items():
            _check_codex(name, server)
    for text in json_blocks:
        for name, server in json.loads(text)["mcpServers"].items():
            _check_claude(name, server)


def test_the_sample_config_examples_parse_and_plan(source: Path, tmp_path: Path) -> None:
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert "SUPPORTED: skills, agents, commands, rules, hooks and mcp" in text
    assert "remain planned" not in text
    examples = re.findall(r"^# \[example (\w+)\]\n(.*?)^# \[/example\]", text, re.DOTALL | re.MULTILINE)
    assert {kind for kind, _ in examples} == {"hooks", "mcp"}
    for kind, body in examples:
        block = "\n".join(re.sub(r"^# ?", "", line) for line in body.splitlines())
        assert plan(kind, block, source, tmp_path).ops, kind


# --- content that must not go stale ---------------------------------------------------------------


def section(heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", README, re.DOTALL | re.MULTILINE)
    assert match, heading
    return match[1]


def test_what_is_written_lists_the_shared_files() -> None:
    text = section("What is written in the project")
    for path in (".claude/settings.local.json", ".codex/hooks.json", ".mcp.json", ".codex/config.toml"):
        assert path in text, path


def test_prune_covers_hooks_and_mcp() -> None:
    text = section("Prune")
    assert "hook" in text and "MCP" in text
    assert "`--prune` fails with a conflict until you pass `--force`" in text


def test_the_readme_secret_words_match_the_code() -> None:
    text = README.split("the secret words are", 1)[1].split("It fails closed", 1)[0]
    documented = set(re.findall(r"`([a-z]+)`", text))
    assert documented == set(_SECRET_WORDS)
    assert _auth_like("X-Session-Region") and _auth_like("Cache-Key")
    assert not _auth_like("X-Keyboard-Layout") and not _auth_like("X-Author")


def test_the_readme_dry_run_lines_match_the_cli(source: Path, tmp_path: Path) -> None:
    placeholder = "/home/me/.cache/agent-sync/github.com/acme%2Ftools"
    produced = set()
    for kind in ("hooks", "mcp"):
        for block in kind_blocks(kind):
            for op in plan(kind, block, source, tmp_path).ops:
                line = _describe_merge(Change(op, "create"), False)
                produced.add(line.replace(str(source), placeholder))
    documented = [
        line for line in README.splitlines() if line.startswith(("Would add hook", "Would add MCP server"))
    ]
    assert len(documented) >= 4
    for line in documented:
        assert line in produced, line


def test_the_readme_documents_the_codex_url_rule_and_created_file_exclusion() -> None:
    assert "A Codex `url` containing `${`" in README
    assert "stays git-excluded while it exists" in README
    assert "`--force` cannot remove it" in README
