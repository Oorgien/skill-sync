from pathlib import Path

import pytest
import tomlkit

from agent_sync.context import Context
from agent_sync.kinds.agents import plan_agents
from agent_sync.kinds.source_files import KindPlan
from agent_sync.manifest import parse_manifest
from agent_sync.state import State


def plan(tmp_path: Path, selection: str = 'all', section: str = 'common') -> KindPlan:
    ctx = Context(tmp_path / 'project', tmp_path, tmp_path / 'cache', False, False, False, State(()))
    entries = parse_manifest({'agents': {section: [f'personal:{selection}']}})
    return plan_agents(entries, ctx)


def put(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_conversion_and_native_claude(tmp_path: Path) -> None:
    text = '---\nname: reviewer\ndescription: Review code\ntools: Read\nmodel: opus\n---\nReview "carefully".\n'
    put(tmp_path, 'agents/reviewer.md', text)
    result = plan(tmp_path)
    assert result.ops[0].payload == text.encode('utf-8')
    converted = tomlkit.parse(result.ops[1].payload.decode('utf-8'))
    assert converted['developer_instructions'] == 'Review "carefully".\n'
    assert 'model' not in converted
    assert any('tools' in d and 'permissions' in d for d in result.diagnostics)


def test_registered_native_preserves_config(tmp_path: Path) -> None:
    put(tmp_path, '.codex/config.toml', '[agents.docs_researcher]\ndescription="Read docs"\nconfig_file="agents/docs-researcher.toml"\n')
    native = '# native settings\nmodel="gpt-6"\nsandbox_mode="read-only"\ndeveloper_instructions="Read"\n'
    put(tmp_path, '.codex/agents/docs-researcher.toml', native)
    result = plan(tmp_path, 'docs-researcher', 'codex')
    assert native.encode('utf-8') in result.ops[0].payload
    parsed = tomlkit.parse(result.ops[0].payload.decode('utf-8'))
    assert parsed['name'] == 'docs_researcher'
    assert parsed['description'] == 'Read docs'


def test_standalone_bytes_and_native_preference(tmp_path: Path) -> None:
    native = 'name="reviewer"\ndescription="Native"\ndeveloper_instructions="Review"\n'
    put(tmp_path, '.codex/agents/reviewer.toml', native)
    put(tmp_path, 'agents/reviewer.md', 'broken unused markdown')
    assert plan(tmp_path, 'reviewer', 'codex').ops[0].payload == native.encode('utf-8')


@pytest.mark.parametrize('relative,text', [
    ('agents/bad.md', '---\nname: other\ndescription: x\n---\nBody'),
    ('.codex/agents/bad.toml', 'name="bad"'),
    ('.codex/agents/bad.toml', 'name="bad"\ndescription="x"\ndeveloper_instructions="x"\nmodel_instructions_file="../x.md"'),
])
def test_invalid_agents(tmp_path: Path, relative: str, text: str) -> None:
    put(tmp_path, relative, text)
    with pytest.raises(ValueError):
        plan(tmp_path)


def test_duplicate_and_symlink_rejected(tmp_path: Path) -> None:
    put(tmp_path, 'agents/a.md', 'x')
    (tmp_path / 'agents/b.md').symlink_to(tmp_path / 'agents/a.md')
    with pytest.raises(ValueError, match='[Ss]ymlink'):
        plan(tmp_path)


def test_empty_and_unknown(tmp_path: Path) -> None:
    assert plan(tmp_path).diagnostics
    with pytest.raises(ValueError, match='Available'):
        plan(tmp_path, 'unknown')


@pytest.mark.parametrize('config', [
    '../agents/a.toml', '/tmp/a.toml', 'agents/missing.toml',
])
def test_registry_rejects_unsafe_or_missing_paths(tmp_path: Path, config: str) -> None:
    put(tmp_path, '.codex/config.toml', f'[agents.a]\ndescription="A"\nconfig_file="{config}"\n')
    with pytest.raises(ValueError):
        plan(tmp_path)


def test_repeated_entry_deduplicates(tmp_path: Path) -> None:
    put(tmp_path, 'agents/a.md', '---\nname: a\ndescription: A\n---\nBody')
    entries = parse_manifest({'agents': {'common': ['personal:a', 'personal:a']}})
    ctx = Context(tmp_path / 'project', tmp_path, tmp_path / 'cache', False, False, False, State(()))
    assert len(plan_agents(entries, ctx).ops) == 2


def test_same_format_ambiguity(tmp_path: Path) -> None:
    put(tmp_path, 'agents/a.md', 'x')
    put(tmp_path, '.claude/agents/a.md', 'x')
    with pytest.raises(ValueError, match='Ambiguous'):
        plan(tmp_path)


def test_wrong_harness_native_format_ignored(tmp_path: Path) -> None:
    put(tmp_path, '.claude/agents/a.toml', 'invalid ignored')
    assert plan(tmp_path).ops == []


def test_native_runtime_name_may_differ_from_selector(tmp_path: Path) -> None:
    native = 'name="runtime_name"\ndescription="A"\ndeveloper_instructions="Body"\n'
    put(tmp_path, '.codex/agents/selector.toml', native)
    assert plan(tmp_path, 'selector', 'codex').ops[0].payload == native.encode('utf-8')


def test_duplicate_runtime_names_rejected(tmp_path: Path) -> None:
    native = 'name="runtime_name"\ndescription="A"\ndeveloper_instructions="Body"\n'
    put(tmp_path, '.codex/agents/a.toml', native)
    put(tmp_path, '.codex/agents/b.toml', native)
    with pytest.raises(ValueError, match='runtime agent name'):
        plan(tmp_path, section='codex')


def test_registry_ambiguity_rejected(tmp_path: Path) -> None:
    put(tmp_path, '.codex/agents/a.toml', 'developer_instructions="A"\n')
    put(tmp_path, '.codex/config.toml', '[agents.a]\ndescription="A"\nconfig_file="agents/a.toml"\n[agents.b]\ndescription="B"\nconfig_file="agents/a.toml"\n')
    with pytest.raises(ValueError, match='Ambiguous registered'):
        plan(tmp_path)


def test_registered_symlink_rejected(tmp_path: Path) -> None:
    put(tmp_path, 'outside.toml', 'developer_instructions="A"\n')
    put(tmp_path, '.codex/config.toml', '[agents.a]\ndescription="A"\nconfig_file="agents/a.toml"\n')
    (tmp_path / '.codex/agents').mkdir()
    (tmp_path / '.codex/agents/a.toml').symlink_to(tmp_path / 'outside.toml')
    with pytest.raises(ValueError, match='Symlink'):
        plan(tmp_path)


def test_engine_native_crlf_roundtrip(tmp_path: Path) -> None:
    from agent_sync.engine import run

    native = b'name="a"\r\ndescription="A"\r\ndeveloper_instructions="Body"\r\n'
    path = tmp_path / '.codex/agents/a.toml'
    path.parent.mkdir(parents=True)
    path.write_bytes(native)
    project = tmp_path / 'project'
    project.mkdir()
    result = plan(tmp_path, section='codex')
    run(project, result.ops, apply=True, prune=False, force=False)
    assert (project / '.codex/agents/a.toml').read_bytes() == native


def test_nested_resource_config_rejected(tmp_path: Path) -> None:
    put(tmp_path, '.codex/agents/a.toml', 'name="a"\ndescription="A"\ndeveloper_instructions="A"\n[[skills.config]]\npath="../../skills/a"\nenabled=true\n')
    with pytest.raises(ValueError, match='relocate'):
        plan(tmp_path, section='codex')
