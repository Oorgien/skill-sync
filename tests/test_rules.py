from pathlib import Path

import pytest

from agent_sync.context import Context
from agent_sync.kinds.rules import plan_rules
from agent_sync.kinds.source_files import KindPlan
from agent_sync.manifest import parse_manifest
from agent_sync.state import State


def put(root: Path, relative: str, content: bytes = b'rule\n') -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def plan(root: Path, names: tuple[str, ...] = ('all',), section: str = 'common') -> KindPlan:
    ctx = Context(root / 'project', root, root / 'cache', False, False, False, State(()))
    entries = parse_manifest({'rules': {section: [f'personal:{n}' for n in names]}})
    return plan_rules(entries, ctx)


def test_routes_recursive_rules_preserving_original_bytes(tmp_path: Path) -> None:
    payload = b'---\r\npaths: ["*.py"]\r\n---\r\nCheck Python.\r\n'
    put(tmp_path, 'rules/python/testing.md', payload)
    put(tmp_path, 'rules/js/testing.md')
    put(tmp_path, '.codex/rules/git.rules', b'prefix_rule(pattern=["git"], decision="allow")\n')
    put(tmp_path, '.claude/rules/local.md')
    put(tmp_path, '.claude/rules/ignored.rules')
    put(tmp_path, '.codex/rules/ignored.md')
    put(tmp_path, 'rules/readme.txt')
    result = plan(tmp_path)
    outputs = {op.target: op for op in result.ops}
    assert set(outputs) == {'.claude/rules/python/testing.md', '.claude/rules/js/testing.md',
                            '.codex/rules/git.rules', '.claude/rules/local.md'}
    assert outputs['.claude/rules/python/testing.md'].payload == payload
    assert all(op.kind == 'rules' and op.action == 'write' and not op.adopt for op in result.ops)


@pytest.mark.parametrize('section,expected', [('common', 2), ('claude', 1), ('codex', 1)])
def test_formats_filtered_in_named_group(tmp_path: Path, section: str, expected: int) -> None:
    put(tmp_path, 'rules/python/testing.md')
    put(tmp_path, 'rules/python/git.rules')
    assert len(plan(tmp_path, ('python',), section).ops) == expected


def test_named_file_and_overlaps_deduplicate(tmp_path: Path) -> None:
    put(tmp_path, 'rules/basic.md')
    put(tmp_path, 'rules/python/deep/test.md')
    result = plan(tmp_path, ('python', 'basic', 'all', 'python'))
    assert len(result.ops) == 2
    assert result.ops[0].source == 'personal:python'


def test_file_directory_selector_ambiguity(tmp_path: Path) -> None:
    put(tmp_path, 'rules/python.md')
    put(tmp_path, 'rules/python/test.md')
    with pytest.raises(ValueError, match='[Aa]mbiguous'):
        plan(tmp_path, ('python',))
    assert len(plan(tmp_path).ops) == 2


def test_conflicting_discovery_roots(tmp_path: Path) -> None:
    put(tmp_path, 'rules/python/test.md')
    put(tmp_path, '.claude/rules/python/test.md')
    with pytest.raises(ValueError, match='[Cc]ollision'):
        plan(tmp_path)


def test_empty_and_incompatible_selectors(tmp_path: Path) -> None:
    put(tmp_path, 'rules/python/test.md')
    assert plan(tmp_path, section='codex').diagnostics
    with pytest.raises(ValueError, match='Available'):
        plan(tmp_path, ('python',), 'codex')


@pytest.mark.parametrize('relative', ['rules/bad name.md', 'rules/good/bad name.md',
                                     'rules/bad name/test.md'])
def test_rejects_unsafe_components(tmp_path: Path, relative: str) -> None:
    put(tmp_path, relative)
    with pytest.raises(ValueError, match='[Ii]nvalid'):
        plan(tmp_path)


@pytest.mark.parametrize('relative', ['rules', 'rules/group', 'rules/test.md'])
def test_rejects_source_symlinks(tmp_path: Path, relative: str) -> None:
    outside = put(tmp_path, 'other/test.md')
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside if path.suffix else outside.parent)
    with pytest.raises(ValueError, match='[Ss]ymlink'):
        plan(tmp_path)


def test_unselected_invalid_rules_are_not_read(tmp_path: Path) -> None:
    put(tmp_path, 'rules/good.md')
    put(tmp_path, 'rules/bad.md', b'\xff')
    assert len(plan(tmp_path, ('good',)).ops) == 1
    with pytest.raises(ValueError, match='UTF-8'):
        plan(tmp_path)


def test_missing_source_reported_without_fetch(tmp_path: Path) -> None:
    ctx = Context(tmp_path, None, tmp_path / 'cache', False, False, True, State(()))
    entries = parse_manifest({'rules': {'common': ['github.com:owner/repo:all']}})
    result = plan_rules(entries, ctx)
    assert result.missing == entries
    assert result.ops == []
    assert not ctx.cache_root.exists()


def test_unselected_symlink_does_not_block_named_rule(tmp_path: Path) -> None:
    put(tmp_path, 'rules/good.md')
    (tmp_path / 'rules/bad.md').symlink_to(tmp_path / 'missing')
    assert len(plan(tmp_path, ('good',)).ops) == 1


def test_distinct_sources_cannot_share_target(tmp_path: Path) -> None:
    put(tmp_path, 'rules/test.md')
    put(tmp_path, 'cache/github.com/owner%2Frepo/rules/test.md')
    ctx = Context(tmp_path / 'project', tmp_path, tmp_path / 'cache',
                  False, False, False, State(()))
    entries = parse_manifest({'rules': {'common': [
        'personal:all', 'github.com:owner/repo:all']}})
    with pytest.raises(ValueError, match='[Cc]ollision'):
        plan_rules(entries, ctx)
