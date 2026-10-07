"""MCP planner: native first, conversion fallback, inline servers, references-only secrets."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import tomlkit

from agent_sync.context import Context
from agent_sync.engine import PlanError, run
from agent_sync.kinds.mcp import plan_mcp
from agent_sync.kinds.source_files import KindPlan
from agent_sync.manifest import parse_manifest
from agent_sync.state import State

CLAUDE_JSON = {'mcpServers': {'chrome-devtools': {'command': 'npx', 'args': ['-y', 'chrome-devtools-mcp@1.10.1']}}}
CODEX_TOML = '''\
# a top-level comment
approval_policy = "on-request"

[mcp_servers.github]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
startup_timeout_sec = 30

[mcp_servers.context7]
command = "npx"
# Canonical Codex section name is `context7`.
args = ["-y", "@upstash/context7-mcp@latest"]

# [mcp_servers.commented]
# command = "nope"
'''


def put(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


@pytest.fixture
def src(tmp_path: Path) -> Path:
    root = tmp_path / 'src'
    put(root, '.mcp.json', json.dumps(CLAUDE_JSON))
    put(root, '.codex/config.toml', CODEX_TOML)
    return root


def ctx_for(tmp_path: Path, *, apply: bool = False) -> Context:
    project = tmp_path / 'project'
    project.mkdir(exist_ok=True)
    return Context(project.resolve(), tmp_path / 'src', tmp_path / 'cache', apply, False, False, State(()))


def plan(tmp_path: Path, items: list[Any], section: str = 'common') -> KindPlan:
    entries = parse_manifest({'mcp': {section: items}})
    return plan_mcp(entries, ctx_for(tmp_path))


def by(result: KindPlan) -> dict[tuple[str | None, str], Any]:
    return {(op.harness, op.locator[-1] if op.locator else ''): op for op in result.ops}


def apply(tmp_path: Path, result: KindPlan, **kw: bool) -> Any:
    return run(ctx_for(tmp_path).project, result.ops, apply=True, prune=kw.get('prune', False), force=False)


# --- native -----------------------------------------------------------------------------------


def test_native_per_harness_is_copied_unchanged(tmp_path: Path, src: Path) -> None:
    result = plan(tmp_path, ['personal:github'], 'codex')
    (op,) = result.ops
    assert (op.target, op.locator, op.container, op.harness) == ('.codex/config.toml', ('mcp_servers', 'github'), 'key', 'codex')
    assert op.source == 'personal:github'
    assert dict(op.payload) == {'command': 'npx', 'args': ['-y', '@modelcontextprotocol/server-github'], 'startup_timeout_sec': 30}
    assert result.diagnostics == []
    claude = plan(tmp_path, ['personal:chrome-devtools'], 'claude')
    (cop,) = claude.ops
    assert (cop.target, cop.locator) == ('.mcp.json', ('mcpServers', 'chrome-devtools'))
    assert cop.payload == CLAUDE_JSON['mcpServers']['chrome-devtools']


def test_codex_inner_comment_survives_into_written_file(tmp_path: Path, src: Path) -> None:
    result = plan(tmp_path, ['personal:context7'], 'codex')
    apply(tmp_path, result)
    text = (tmp_path / 'project/.codex/config.toml').read_text(encoding='utf-8')
    assert '# Canonical Codex section name is `context7`.' in text
    assert tomlkit.parse(text)['mcp_servers']['context7']['command'] == 'npx'


def test_all_is_native_only(tmp_path: Path, src: Path) -> None:
    result = plan(tmp_path, ['personal:all'])
    assert set(by(result)) == {('claude', 'chrome-devtools'), ('codex', 'github'), ('codex', 'context7')}
    assert result.diagnostics == []


def test_commented_tables_are_not_definitions(tmp_path: Path, src: Path) -> None:
    with pytest.raises(ValueError, match='commented'):
        plan(tmp_path, ['personal:commented'])


def test_missing_on_both_sides_names_server_and_files(tmp_path: Path, src: Path) -> None:
    with pytest.raises(ValueError, match=r"'nope'.*\.mcp\.json.*config\.toml"):
        plan(tmp_path, ['personal:nope'])


def test_missing_source_files_are_optional(tmp_path: Path) -> None:
    (tmp_path / 'src').mkdir()
    assert plan(tmp_path, ['personal:all']).ops == []


# --- conversion -------------------------------------------------------------------------------


def test_fallback_codex_to_claude_warns_about_dropped_fields(tmp_path: Path, src: Path) -> None:
    result = plan(tmp_path, ['personal:github'], 'claude')
    (op,) = result.ops
    assert op.payload == {'command': 'npx', 'args': ['-y', '@modelcontextprotocol/server-github']}
    (warning,) = result.diagnostics
    assert 'github' in warning and 'startup_timeout_sec' in warning


def test_fallback_claude_to_codex_converts(tmp_path: Path, src: Path) -> None:
    put(src, '.mcp.json', json.dumps({'mcpServers': {'api': {
        'command': 'npx', 'args': ['x'], 'cwd': 'sub', 'env': {'KEY': '${KEY}'}, 'timeout': 5}}}))
    result = plan(tmp_path, ['personal:api'], 'codex')
    (op,) = result.ops
    assert op.payload == {'command': 'npx', 'args': ['x'], 'cwd': 'sub', 'env_vars': ['KEY']}
    assert 'timeout' in result.diagnostics[0]


def test_conversion_of_http_and_bearer_both_ways(tmp_path: Path, src: Path) -> None:
    put(src, '.mcp.json', json.dumps({'mcpServers': {'web': {
        'type': 'http', 'url': 'https://x.test/mcp', 'headers': {'Authorization': 'Bearer ${TOK}'}}}}))
    (op,) = plan(tmp_path, ['personal:web'], 'codex').ops
    assert op.payload == {'url': 'https://x.test/mcp', 'bearer_token_env_var': 'TOK'}
    put(src, '.mcp.json', '{}')
    put(src, '.codex/config.toml', '[mcp_servers.web]\nurl = "https://x.test/mcp"\nbearer_token_env_var = "TOK"\n')
    (back,) = plan(tmp_path, ['personal:web'], 'claude').ops
    assert back.payload == {'type': 'http', 'url': 'https://x.test/mcp', 'headers': {'Authorization': 'Bearer ${TOK}'}}


def test_codex_env_vars_convert_to_claude_env(tmp_path: Path, src: Path) -> None:
    put(src, '.codex/config.toml', '[mcp_servers.s]\ncommand = "x"\nenv_vars = ["A", "B"]\nenabled = false\n')
    (op,) = plan(tmp_path, ['personal:s'], 'claude').ops
    assert op.payload == {'command': 'x', 'env': {'A': '${A}', 'B': '${B}'}}


def test_renamed_reference_is_an_error(tmp_path: Path, src: Path) -> None:
    put(src, '.mcp.json', json.dumps({'mcpServers': {'r': {'command': 'x', 'env': {'X': '${Y}'}}}}))
    with pytest.raises(ValueError, match=r"'r'.*'X'"):
        plan(tmp_path, ['personal:r'], 'codex')
    assert plan(tmp_path, ['personal:r'], 'claude').ops  # native Claude is not converted


# --- inline -----------------------------------------------------------------------------------


def test_inline_command_server_for_both_harnesses(tmp_path: Path, src: Path) -> None:
    result = plan(tmp_path, [{'name': 'ctx', 'command': 'npx', 'args': ['-y', 'p'], 'cwd': 'd', 'env': ['K']}])
    ops = by(result)
    assert ops[('claude', 'ctx')].payload == {'command': 'npx', 'args': ['-y', 'p'], 'cwd': 'd', 'env': {'K': '${K}'}}
    assert ops[('codex', 'ctx')].payload == {'command': 'npx', 'args': ['-y', 'p'], 'cwd': 'd', 'env_vars': ['K']}
    assert ops[('claude', 'ctx')].source == 'mcp:ctx'
    assert result.diagnostics == []


def test_inline_url_server_for_claude_only(tmp_path: Path, src: Path) -> None:
    result = plan(tmp_path, [{'name': 'figma', 'url': 'https://f.test/mcp', 'bearer_token_env': 'FIG',
                              'headers': {'X-Team': 'a'}}], 'claude')
    (op,) = result.ops
    assert op.payload == {'type': 'http', 'url': 'https://f.test/mcp',
                          'headers': {'X-Team': 'a', 'Authorization': 'Bearer ${FIG}'}}


def test_inline_url_server_for_codex(tmp_path: Path, src: Path) -> None:
    (op,) = plan(tmp_path, [{'name': 'figma', 'url': 'https://f.test/mcp', 'bearer_token_env': 'FIG',
                             'headers': {'X-Team': 'a'}}], 'codex').ops
    assert op.payload == {'url': 'https://f.test/mcp', 'bearer_token_env_var': 'FIG', 'http_headers': {'X-Team': 'a'}}


@pytest.mark.parametrize('item', [
    {'name': 'a', 'command': 'x', 'bogus': 1},
    {'name': 'a'},
    {'name': 'a', 'command': 'x', 'url': 'https://u'},
    {'name': 'a', 'command': 'x', 'args': 'notalist'},
    {'name': 'a', 'command': 'x', 'env': ['bad name']},
    {'name': 'a', 'command': 'x', 'bearer_token_env': 'T'},
    {'name': 'a', 'url': 'https://u', 'args': ['x']},
])
def test_inline_validation(tmp_path: Path, item: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="'a'"):
        plan(tmp_path, [item])


# --- secrets ----------------------------------------------------------------------------------


@pytest.mark.parametrize('definition', [
    {'command': 'x', 'env': {'TOKEN': 'abc123'}},
    {'type': 'http', 'url': 'https://u', 'headers': {'Authorization': 'Bearer abc'}},
    {'type': 'http', 'url': 'https://u', 'headers': {'X-Api-Key': 'abc'}},
    {'type': 'http', 'url': 'https://u', 'headers': {'my-secret': 'abc'}},
    {'type': 'http', 'url': 'https://u', 'headers': {'X-Token': 'abc'}},
])
def test_claude_literal_secrets_fail(tmp_path: Path, src: Path, definition: dict[str, Any]) -> None:
    put(src, '.mcp.json', json.dumps({'mcpServers': {'s': definition}}))
    for section in ('claude', 'codex'):  # also when converted
        with pytest.raises(ValueError, match=r"'s'.*(claude)"):
            plan(tmp_path, ['personal:s'], section)


def test_claude_reference_headers_pass(tmp_path: Path, src: Path) -> None:
    put(src, '.mcp.json', json.dumps({'mcpServers': {'s': {
        'type': 'http', 'url': 'https://u', 'headers': {'X-Api-Key': '${K}', 'Authorization': '${T}', 'X-Plain': 'v'}}}}))
    (op,) = plan(tmp_path, ['personal:s'], 'codex').ops
    assert op.payload == {'url': 'https://u', 'http_headers': {'X-Plain': 'v'},
                          'env_http_headers': {'X-Api-Key': 'K', 'Authorization': 'T'}}


def test_codex_env_table_fails(tmp_path: Path, src: Path) -> None:
    put(src, '.codex/config.toml', '[mcp_servers.s]\ncommand = "x"\n[mcp_servers.s.env]\nTOKEN = "abc"\n')
    for section in ('codex', 'claude'):
        with pytest.raises(ValueError, match=r"'s'.*codex.*env_vars"):
            plan(tmp_path, ['personal:s'], section)


def test_codex_literal_auth_header_fails(tmp_path: Path, src: Path) -> None:
    put(src, '.codex/config.toml',
        '[mcp_servers.s]\nurl = "https://u"\n[mcp_servers.s.http_headers]\nAuthorization = "Bearer abc"\n')
    with pytest.raises(ValueError, match=r"'s'.*codex.*Authorization.*bearer_token_env_var"):
        plan(tmp_path, ['personal:s'], 'codex')


def test_inline_literal_auth_header_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"'a'.*X-Token"):
        plan(tmp_path, [{'name': 'a', 'url': 'https://u', 'headers': {'X-Token': 'abc'}}])


def test_args_are_not_checked(tmp_path: Path, src: Path) -> None:
    put(src, '.mcp.json', json.dumps({'mcpServers': {'s': {'command': 'x', 'args': ['--project-ref=YOUR_PROJECT_REF']}}}))
    assert plan(tmp_path, ['personal:s'], 'claude').ops


# --- source path ------------------------------------------------------------------------------


def _plugin_root_source(src: Path) -> None:
    put(src, '.mcp.json', json.dumps({'mcpServers': {'p': {'command': '${CLAUDE_PLUGIN_ROOT}/bin/run'}}}))


def test_plugin_root_replaced_with_source_path(tmp_path: Path, src: Path) -> None:
    _plugin_root_source(src)
    (op,) = plan(tmp_path, ['personal:p'], 'claude').ops
    assert op.payload == {'command': f'{src}/bin/run'}


def test_plugin_root_rejected_for_tracked_mcp_json(tmp_path: Path, src: Path) -> None:
    _plugin_root_source(src)
    project = ctx_for(tmp_path).project
    subprocess.run(['git', 'init', '-q'], cwd=project, check=True)
    (project / '.mcp.json').write_text('{}')
    subprocess.run(['git', 'add', '.mcp.json'], cwd=project, check=True)
    with pytest.raises(ValueError, match='tracked'):
        plan(tmp_path, ['personal:p'], 'claude')


# --- planner rules ----------------------------------------------------------------------------


def test_duplicate_harness_and_name_is_an_error(tmp_path: Path, src: Path) -> None:
    with pytest.raises(ValueError, match='Duplicate'):
        plan(tmp_path, ['personal:github', {'name': 'github', 'command': 'x'}])


def test_missing_source_in_dry_run_is_reported(tmp_path: Path) -> None:
    entries = parse_manifest({'mcp': {'common': ['github.com:o/r:x']}})
    result = plan_mcp(entries, ctx_for(tmp_path))
    assert result.ops == [] and result.missing == entries


# --- engine round trips -----------------------------------------------------------------------


def test_round_trip_preserves_user_content_and_is_idempotent(tmp_path: Path, src: Path) -> None:
    project = ctx_for(tmp_path).project
    (project / '.mcp.json').write_text(json.dumps({'other': 1, 'mcpServers': {'mine': {'command': 'm'}}}))
    mine = '# my note\n[mcp_servers.mine]\ncommand = "m"\n\n[profiles.x]\nmodel = "y"\n'
    put(project, '.codex/config.toml', mine)
    result = plan(tmp_path, ['personal:all'])
    first = apply(tmp_path, result)
    assert len(first.changes) == 3
    data = json.loads((project / '.mcp.json').read_text())
    assert list(data['mcpServers']) == ['mine', 'chrome-devtools'] and data['other'] == 1
    text = (project / '.codex/config.toml').read_text()
    assert text.startswith('# my note\n[mcp_servers.mine]') and '[profiles.x]\nmodel = "y"' in text
    assert '[mcp_servers.github]' in text and 'commented' not in text
    assert apply(tmp_path, plan(tmp_path, ['personal:all'])).changes == ()


def test_same_name_user_server_is_a_conflict_and_untouched(tmp_path: Path, src: Path) -> None:
    project = ctx_for(tmp_path).project
    claude_text = json.dumps({'mcpServers': {'chrome-devtools': {'command': 'mine'}}})
    toml_text = '[mcp_servers.github]\ncommand = "mine"\n'
    (project / '.mcp.json').write_text(claude_text)
    put(project, '.codex/config.toml', toml_text)
    with pytest.raises(PlanError):
        apply(tmp_path, plan(tmp_path, ['personal:all']))
    assert (project / '.mcp.json').read_text() == claude_text
    assert (project / '.codex/config.toml').read_text() == toml_text


def test_prune_removes_only_ours_and_deletes_created_files(tmp_path: Path, src: Path) -> None:
    project = ctx_for(tmp_path).project
    apply(tmp_path, plan(tmp_path, ['personal:all']))
    assert (project / '.mcp.json').exists()
    data = json.loads((project / '.mcp.json').read_text())
    data['mcpServers']['mine'] = {'command': 'm'}
    (project / '.mcp.json').write_text(json.dumps(data))
    apply(tmp_path, plan(tmp_path, ['personal:github'], 'codex'), prune=True)
    assert json.loads((project / '.mcp.json').read_text()) == {'mcpServers': {'mine': {'command': 'm'}}}
    toml = tomlkit.parse((project / '.codex/config.toml').read_text())
    assert list(toml['mcp_servers']) == ['github']
    apply(tmp_path, KindPlan([], [], []), prune=True)
    assert not (project / '.codex/config.toml').exists()


def test_inline_env_must_be_variable_names(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="'env' must be a list of variable names"):
        plan(tmp_path, [{'name': 'a', 'command': 'x', 'env': ['bad name']}])


# --- S8: wider secret detection ---------------------------------------------------------------

SECRET_HEADERS = ['Proxy-Authorization', 'Cookie', 'X-Session-Id', 'X-Password', 'X-Credential']
BAD_URLS = ['https://user:pass@h.test/mcp', 'https://tok@h.test/mcp', 'https://h.test/mcp?api_key=abc',
            'https://h.test/mcp?x=1&Token=abc', 'https://h.test/mcp?session=abc']


@pytest.mark.parametrize('header', SECRET_HEADERS)
def test_wide_secret_headers_rejected_native_converted_inline(tmp_path: Path, src: Path, header: str) -> None:
    put(src, '.mcp.json', json.dumps({'mcpServers': {'s': {'type': 'http', 'url': 'https://u', 'headers': {header: 'abc'}}}}))
    put(src, '.codex/config.toml', f'[mcp_servers.c]\nurl = "https://u"\n[mcp_servers.c.http_headers]\n{header} = "abc"\n')
    for section in ('claude', 'codex'):
        with pytest.raises(ValueError, match=rf"'s'.*{header}"):
            plan(tmp_path, ['personal:s'], section)
        with pytest.raises(ValueError, match=rf"'c'.*{header}"):
            plan(tmp_path, ['personal:c'], section)
        with pytest.raises(ValueError, match=rf"'a'.*{header}"):
            plan(tmp_path, [{'name': 'a', 'url': 'https://u', 'headers': {header: 'abc'}}], section)


@pytest.mark.parametrize('header', SECRET_HEADERS)
def test_wide_secret_headers_accept_references(tmp_path: Path, src: Path, header: str) -> None:
    put(src, '.mcp.json', json.dumps({'mcpServers': {'s': {'type': 'http', 'url': 'https://u', 'headers': {header: '${V}'}}}}))
    assert plan(tmp_path, ['personal:s']).ops


@pytest.mark.parametrize('url', BAD_URLS)
def test_credentials_in_url_rejected(tmp_path: Path, src: Path, url: str) -> None:
    put(src, '.mcp.json', json.dumps({'mcpServers': {'s': {'type': 'http', 'url': url}}}))
    put(src, '.codex/config.toml', f'[mcp_servers.c]\nurl = "{url}"\n')
    for section in ('claude', 'codex'):
        with pytest.raises(ValueError, match=r"'s'.*(claude).*url"):
            plan(tmp_path, ['personal:s'], section)
        with pytest.raises(ValueError, match=r"'c'.*(codex).*url"):
            plan(tmp_path, ['personal:c'], 'codex')
        with pytest.raises(ValueError, match=r"'a'.*url"):
            plan(tmp_path, [{'name': 'a', 'url': url}], section)


def test_url_references_and_harmless_values_allowed(tmp_path: Path, src: Path) -> None:
    url = 'https://h.test/mcp?api_key=${KEY}&region=eu'
    put(src, '.mcp.json', json.dumps({'mcpServers': {'s': {
        'type': 'http', 'url': url, 'headers': {'Accept': 'application/json', 'X-Region': 'eu'}}}}))
    assert plan(tmp_path, ['personal:s'], 'claude').ops
    assert plan(tmp_path, [{'name': 'a', 'url': 'https://h.test/mcp?token=${T}'}], 'claude').ops


# --- S8 round 2: whole words, fragments, Codex url references ---------------------------------

LITERAL_HEADERS = ['Authorization', 'Proxy-Authorization', 'Cookie', 'X-Api-Key', 'X-Session-Id',
                   'X-Signature', 'X-Csrf-Token', 'apiKey', 'X-ApiKey', 'x_api_key']
HARMLESS_HEADERS = ['X-Keyboard-Layout', 'X-Author', 'X-Hotkey', 'X-Tokenizer-Model', 'Accept', 'X-Region']


@pytest.mark.parametrize('header', LITERAL_HEADERS)
def test_secret_words_reject_literal_headers(tmp_path: Path, header: str) -> None:
    with pytest.raises(ValueError, match=rf"'a'.*{header}"):
        plan(tmp_path, [{'name': 'a', 'url': 'https://u', 'headers': {header: 'abc'}}], 'claude')


@pytest.mark.parametrize('header', HARMLESS_HEADERS)
def test_words_that_merely_contain_a_secret_word_pass(tmp_path: Path, header: str) -> None:
    for section in ('claude', 'codex'):
        assert plan(tmp_path, [{'name': 'a', 'url': 'https://u', 'headers': {header: 'abc'}}], section).ops


@pytest.mark.parametrize('header', ['X-Session-Region', 'Cache-Key'])
def test_fail_closed_when_a_whole_secret_word_is_present(tmp_path: Path, header: str) -> None:
    """Judgement call: the names contain the whole words 'session' / 'key', so they are rejected
    as literals even though they are probably harmless. Use a ${VAR} reference to pass."""
    with pytest.raises(ValueError, match=header):
        plan(tmp_path, [{'name': 'a', 'url': 'https://u', 'headers': {header: 'eu'}}], 'claude')


@pytest.mark.parametrize('url', [
    'https://h.test/mcp?sig=abc', 'https://h.test/mcp?api_key=abc', 'https://h.test/mcp?apiKey=abc',
    'https://h.test/mcp#token=abc', 'https://h.test/mcp?x=1#access_token=abc',
])
def test_url_query_and_fragment_secrets_rejected(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError, match=r"'a'.*claude.*url"):
        plan(tmp_path, [{'name': 'a', 'url': url}], 'claude')


@pytest.mark.parametrize('url', ['https://h.test/mcp?keyword=x', 'https://h.test/mcp?author=x#hotkey=1'])
def test_url_harmless_parameters_pass(tmp_path: Path, url: str) -> None:
    assert plan(tmp_path, [{'name': 'a', 'url': url}], 'claude').ops


def test_url_fragment_reference_allowed(tmp_path: Path) -> None:
    assert plan(tmp_path, [{'name': 'a', 'url': 'https://h.test/mcp#token=${T}'}], 'claude').ops


def test_codex_url_with_reference_is_an_error_in_all_forms(tmp_path: Path, src: Path) -> None:
    url = 'https://h.test/mcp?q=${Q}'
    put(src, '.codex/config.toml', f'[mcp_servers.n]\nurl = "{url}"\n')
    with pytest.raises(ValueError, match=r"'n'.*codex.*expand.*bearer_token_env_var"):
        plan(tmp_path, ['personal:n'], 'codex')  # native
    put(src, '.mcp.json', json.dumps({'mcpServers': {'c': {'type': 'http', 'url': url}}}))
    with pytest.raises(ValueError, match=r"'c'.*codex.*expand"):
        plan(tmp_path, ['personal:c'], 'codex')  # converted
    with pytest.raises(ValueError, match=r"'a'.*codex.*expand"):
        plan(tmp_path, [{'name': 'a', 'url': url}], 'codex')  # inline
    assert plan(tmp_path, ['personal:c'], 'claude').ops  # Claude keeps the reference
    assert plan(tmp_path, [{'name': 'a', 'url': url}], 'claude').ops
