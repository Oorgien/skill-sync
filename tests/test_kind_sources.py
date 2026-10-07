from pathlib import Path

import pytest

from agent_sync.kinds.metadata import parse_markdown
from agent_sync.kinds.source_files import discover_files, read_utf8, safe_path


def test_optional_frontmatter_preserves_body(tmp_path: Path) -> None:
    path = tmp_path / 'command.md'
    path.write_bytes(b'Run $ARGUMENTS\r\n')
    assert parse_markdown(path) == ({}, 'Run $ARGUMENTS\r\n')
    with pytest.raises(ValueError, match='frontmatter'):
        parse_markdown(path, require_name=True)


def test_unsafe_source_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='escapes'):
        safe_path(tmp_path, tmp_path / '../outside')
    (tmp_path / 'agents').symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(ValueError, match='Symlink'):
        discover_files(tmp_path, ('agents',), ('.md',))


def test_utf8_error_is_actionable(tmp_path: Path) -> None:
    path = tmp_path / 'bad.md'
    path.write_bytes(b'\xff')
    with pytest.raises(ValueError, match='UTF-8'):
        read_utf8(path)


def test_case_collision(tmp_path: Path) -> None:
    # Separate roots work on case-insensitive filesystems, too.
    (tmp_path / 'one').mkdir()
    (tmp_path / 'two').mkdir()
    (tmp_path / 'one/A.md').write_text('A')
    (tmp_path / 'two/a.md').write_text('a')
    with pytest.raises(ValueError, match='Case collision'):
        discover_files(tmp_path, ('one', 'two'), ('.md',))


def test_duplicate_yaml_keys_rejected(tmp_path: Path) -> None:
    path = tmp_path / 'command.md'
    path.write_text('---\ndescription: First\ndescription: Second\n---\nBody')
    with pytest.raises(ValueError, match='[Dd]uplicate'):
        parse_markdown(path)


@pytest.mark.parametrize('apply', [False, True])
@pytest.mark.parametrize('kind', ['agents', 'commands', 'rules'])
def test_cached_non_skill_source_needs_no_skill_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, apply: bool, kind: str,
) -> None:
    from agent_sync.context import Context
    from agent_sync.kinds.source_files import SourceResolver
    from agent_sync.manifest import parse_manifest
    from agent_sync.state import State

    cache = tmp_path / 'cache'
    source = cache / 'github.com/owner%2Frepo'
    (source / kind).mkdir(parents=True)
    (source / kind / 'reviewer.md').write_text('Review')

    def forbid_network(*args: object, **kwargs: object) -> None:
        pytest.fail('Cached non-skill source must not trigger git or download')

    monkeypatch.setattr('agent_sync.sources.subprocess.run', forbid_network)
    ctx = Context(tmp_path, None, cache, apply, False, False, State(()))
    entry = parse_manifest({kind: {'common': ['github.com:owner/repo:reviewer']}})[0]
    resolver = SourceResolver(ctx)
    assert resolver.resolve(entry) == source
    assert resolver.resolve(entry) == source
