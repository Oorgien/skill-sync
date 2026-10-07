"""MCP server planning: native definitions first, conversion as a fallback, inline servers.

Servers are installed as merge entries in ``.mcp.json`` (Claude) and ``.codex/config.toml``
(Codex). Only references to environment variables are ever written, never secrets.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import tomlkit

from agent_sync.context import Context
from agent_sync.engine import Op
from agent_sync.gitexclude import is_tracked
from agent_sync.kinds.source_files import KindPlan, SourceResolver, read_utf8, safe_path, selector
from agent_sync.manifest import ManifestEntry
from agent_sync.shared_file import plain
from agent_sync.sources import NAME_RE

CLAUDE_FILE = '.mcp.json'
CODEX_FILE = '.codex/config.toml'
PLUGIN_ROOT = '${CLAUDE_PLUGIN_ROOT}'
_VAR = r'[A-Za-z_][A-Za-z0-9_]*'
_REF = re.compile(rf'\$\{{({_VAR})\}}')
_BEARER = re.compile(rf'Bearer \$\{{({_VAR})\}}')
_ENV_NAME = re.compile(_VAR)
_INLINE_KEYS = {'name', 'command', 'args', 'cwd', 'env', 'url', 'bearer_token_env', 'headers'}


_SECRET_WORDS = frozenset({
    'auth', 'authorization', 'token', 'key', 'apikey', 'secret', 'cookie', 'session', 'password',
    'passwd', 'credential', 'credentials', 'signature', 'sig', 'csrf', 'xsrf', 'bearer',
})
_CAMEL = re.compile(r'(?<=[a-z0-9])(?=[A-Z])')


def _auth_like(header: str) -> bool:
    """Whole-word match: split on - _ . and camelCase; 'X-Hotkey' passes, 'Cache-Key' does not."""
    words = [w for w in re.split(r'[-_.]+', _CAMEL.sub('-', header).lower()) if w]
    return bool(_SECRET_WORDS.intersection(words)) or 'apikey' in ''.join(words)


def _fail(name: str, harness: str, message: str) -> ValueError:
    return ValueError(f'MCP server {name!r} ({harness}): {message}')


def _check_url(name: str, harness: str, data: Mapping[str, Any]) -> None:
    url = data.get('url')
    if not isinstance(url, str):
        return
    if harness == 'codex' and '${' in url:
        raise _fail(name, harness, "url contains a '${...}' reference, which Codex does not expand; "
                    'use bearer_token_env_var or env_http_headers')
    parts = urlsplit(url)
    userinfo, at, _host = parts.netloc.rpartition('@')
    if at and not all(_REF.fullmatch(piece) for piece in userinfo.split(':')):
        raise _fail(name, harness, "url embeds credentials (userinfo); use a '${VAR}' reference or a header")
    for part, label in ((parts.query, 'query parameter'), (parts.fragment, 'fragment parameter')):
        for key, value in parse_qsl(part, keep_blank_values=True):
            if _auth_like(key) and not _REF.fullmatch(value):
                raise _fail(name, harness, f"url {label} {key!r} must be a '${{VAR}}' reference, not a literal value")


def _check_claude(name: str, data: Mapping[str, Any]) -> None:
    _check_url(name, 'claude', data)
    env = data.get('env', {})
    if not isinstance(env, Mapping):
        raise _fail(name, 'claude', "'env' must be a mapping")
    for key, value in env.items():
        if not isinstance(value, str) or not _REF.fullmatch(value):
            raise _fail(name, 'claude', f"env {key!r} must be a reference like '${{{key}}}', not a literal value")
    headers = data.get('headers', {})
    if not isinstance(headers, Mapping):
        raise _fail(name, 'claude', "'headers' must be a mapping")
    for key, value in headers.items():
        if _auth_like(key) and not (isinstance(value, str) and (_REF.fullmatch(value) or _BEARER.fullmatch(value))):
            raise _fail(name, 'claude', f"header {key!r} must be '${{VAR}}' or 'Bearer ${{VAR}}', not a literal value")


def _check_codex(name: str, data: Mapping[str, Any]) -> None:
    _check_url(name, 'codex', data)
    if 'env' in data:
        raise _fail(name, 'codex', "'env' holds literal values; use env_vars = [\"NAME\"] to pass variables by name")
    headers = data.get('http_headers', {})
    if isinstance(headers, Mapping):
        for key in headers:
            if _auth_like(key):
                raise _fail(name, 'codex', f"http_headers {key!r} is a literal value; use bearer_token_env_var or env_http_headers")


def _claude_to_codex(name: str, data: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    out: dict[str, Any] = {}
    dropped: list[str] = []
    env_vars: list[str] = []
    for key, value in data.items():
        if key in ('command', 'args', 'cwd', 'url'):
            out[key] = value
        elif key == 'env':
            for var, ref in value.items():
                match = _REF.fullmatch(ref)
                if not match or match.group(1) != var:
                    raise _fail(name, 'claude', f"env {var!r} renames a variable ({ref}); Codex env_vars cannot express that")
                env_vars.append(var)
        elif key == 'type' and value in ('stdio', 'http'):
            continue
        elif key == 'headers':
            for header, text in value.items():
                bearer = _BEARER.fullmatch(text) if header.lower() == 'authorization' else None
                ref = _REF.fullmatch(text)
                if bearer:
                    out['bearer_token_env_var'] = bearer.group(1)
                elif ref:
                    out.setdefault('env_http_headers', {})[header] = ref.group(1)
                else:
                    out.setdefault('http_headers', {})[header] = text
        else:
            dropped.append(key)
    if env_vars:
        out['env_vars'] = env_vars
    return _ordered(out, ('command', 'args', 'cwd', 'env_vars', 'url', 'bearer_token_env_var',
                          'http_headers', 'env_http_headers')), dropped


def _codex_to_claude(name: str, data: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    out: dict[str, Any] = {}
    dropped: list[str] = []
    headers: dict[str, str] = {}
    for key, value in data.items():
        if key in ('command', 'args', 'cwd'):
            out[key] = value
        elif key == 'url':
            out['type'] = 'http'
            out['url'] = value
        elif key == 'env_vars' and isinstance(value, list) and all(isinstance(v, str) for v in value):
            out['env'] = {v: f'${{{v}}}' for v in value}
        elif key == 'bearer_token_env_var' and isinstance(value, str):
            headers['Authorization'] = f'Bearer ${{{value}}}'
        elif key == 'http_headers' and isinstance(value, Mapping):
            headers.update(value)
        elif key == 'env_http_headers' and isinstance(value, Mapping):
            headers.update({h: f'${{{v}}}' for h, v in value.items()})
        else:
            dropped.append(key)
    if headers:
        out['headers'] = headers
    return _ordered(out, ('type', 'command', 'args', 'cwd', 'env', 'url', 'headers')), dropped


def _ordered(data: dict[str, Any], order: Sequence[str]) -> dict[str, Any]:
    return {key: data[key] for key in order if key in data}


def _replace_root(value: Any, root: str) -> Any:
    if isinstance(value, str):
        return value.replace(PLUGIN_ROOT, root)
    if isinstance(value, list):
        return [_replace_root(v, root) for v in value]
    if isinstance(value, dict):
        return {k: _replace_root(v, root) for k, v in value.items()}
    return value


class _Native:
    """The native server definitions of one source, read lazily and cached."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.claude = self._claude()
        self.codex = self._codex()

    def _claude(self) -> dict[str, Any]:
        path = safe_path(self.root, self.root / CLAUDE_FILE)
        if not path.exists():
            return {}
        try:
            data = json.loads(read_utf8(path))
        except ValueError as exc:
            raise ValueError(f'Invalid JSON {path}: {exc}') from exc
        servers = data.get('mcpServers', {}) if isinstance(data, dict) else None
        if not isinstance(servers, dict) or not all(isinstance(v, dict) for v in servers.values()):
            raise ValueError(f'Invalid mcpServers in {path}')
        return servers

    def _codex(self) -> dict[str, Any]:
        path = safe_path(self.root, self.root / CODEX_FILE)
        if not path.exists():
            return {}
        try:
            data = tomlkit.parse(read_utf8(path))
        except tomlkit.exceptions.TOMLKitError as exc:
            raise ValueError(f'Invalid TOML {path}: {exc}') from exc
        servers = data.get('mcp_servers', {})
        if not isinstance(servers, Mapping) or not all(isinstance(v, Mapping) for v in servers.values()):
            raise ValueError(f'Invalid mcp_servers in {path}')
        return dict(servers)


def _inline_claude(entry: ManifestEntry) -> dict[str, Any]:
    raw = entry.raw
    name = entry.name
    unknown = sorted(set(raw) - _INLINE_KEYS)
    if unknown:
        raise ValueError(f'MCP server {name!r}: unknown inline key(s): {", ".join(map(str, unknown))}')
    if ('command' in raw) == ('url' in raw):
        raise ValueError(f"MCP server {name!r}: define exactly one of 'command' or 'url'")
    out: dict[str, Any] = {}
    if 'command' in raw:
        for key in ('url', 'bearer_token_env', 'headers'):
            if key in raw:
                raise ValueError(f"MCP server {name!r}: {key!r} is only valid with 'url'")
        for key, kind in (('command', str), ('cwd', str)):
            if key in raw and not isinstance(raw[key], kind):
                raise ValueError(f'MCP server {name!r}: {key!r} must be a string')
        args = raw.get('args', [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError(f"MCP server {name!r}: 'args' must be a list of strings")
        env = raw.get('env', [])
        if not isinstance(env, list) or not all(isinstance(e, str) and _ENV_NAME.fullmatch(e) for e in env):
            raise ValueError(f"MCP server {name!r}: 'env' must be a list of variable names")
        out['command'] = raw['command']
        if 'args' in raw:
            out['args'] = args
        if 'cwd' in raw:
            out['cwd'] = raw['cwd']
        if env:
            out['env'] = {e: f'${{{e}}}' for e in env}
        return out
    for key in ('args', 'cwd', 'env'):
        if key in raw:
            raise ValueError(f"MCP server {name!r}: {key!r} is only valid with 'command'")
    if not isinstance(raw['url'], str):
        raise ValueError(f"MCP server {name!r}: 'url' must be a string")  # noqa: TRY004
    out['type'] = 'http'
    out['url'] = raw['url']
    headers = raw.get('headers', {})
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ValueError(f"MCP server {name!r}: 'headers' must be a mapping of strings")
    headers = dict(headers)
    if 'bearer_token_env' in raw:
        var = raw['bearer_token_env']
        if not isinstance(var, str) or not _ENV_NAME.fullmatch(var):
            raise ValueError(f"MCP server {name!r}: 'bearer_token_env' must be a variable name")
        headers['Authorization'] = f'Bearer ${{{var}}}'
    if headers:
        out['headers'] = headers
    return out


def _is_codex(harness: str) -> bool:
    return harness == 'codex'


def plan_mcp(entries: Sequence[ManifestEntry], ctx: Context) -> KindPlan:
    resolver = SourceResolver(ctx)
    natives: dict[Path, _Native] = {}
    ops: dict[tuple[str, str], Op] = {}
    missing: list[ManifestEntry] = []
    diagnostics: list[str] = []
    tracked: dict[str, bool] = {}

    def add(harness: str, name: str, payload: Any, source: str, *, machine: bool) -> None:
        if not NAME_RE.fullmatch(name):
            raise ValueError(f'Invalid MCP server name: {name!r}')
        target = CODEX_FILE if _is_codex(harness) else CLAUDE_FILE
        if (harness, name) in ops:
            raise ValueError(f'Duplicate MCP server {name!r} for {harness}')
        if machine:
            if target not in tracked:
                tracked[target] = is_tracked(ctx.project, target)
            if tracked[target]:
                raise ValueError(
                    f'MCP server {name!r} ({harness}) refers to the cached source folder, which is specific '
                    f'to this machine; {target} is tracked by git. Keep it untracked or define the server inline.'
                )
        locator = ('mcp_servers', name) if _is_codex(harness) else ('mcpServers', name)
        ops[(harness, name)] = Op('merge', target, 'mcp', harness, source, payload, locator, container='key')

    def install(entry: ManifestEntry, name: str, source: str, claude: Mapping[str, Any] | None,
                codex: Mapping[str, Any] | None, machine_claude: bool = False) -> None:
        for harness in ('claude', 'codex'):
            if harness not in entry.harnesses:
                continue
            native, other = (codex, claude) if _is_codex(harness) else (claude, codex)
            machine = False
            if native is not None:
                payload: Any = native
                machine = machine_claude and not _is_codex(harness)
            elif other is not None:
                src = 'claude' if _is_codex(harness) else 'codex'
                (_check_claude if src == 'claude' else _check_codex)(name, other)
                converted, dropped = (_claude_to_codex if _is_codex(harness) else _codex_to_claude)(name, other)
                diagnostics.append(
                    f'MCP server {name!r}: converted from {src} to {harness}; '
                    f'not carried over: {", ".join(dropped) or "none"}'
                )
                payload = converted
                machine = machine_claude and _is_codex(harness)
            else:
                continue
            (_check_codex if _is_codex(harness) else _check_claude)(name, payload)
            add(harness, name, payload, source, machine=machine)

    for entry in entries:
        if entry.raw is not None:
            claude = _inline_claude(entry)
            _check_claude(entry.name, claude)
            codex = _claude_to_codex(entry.name, claude)[0]
            install(entry, entry.name, f'mcp:{entry.name}', claude, codex)
            continue
        root = resolver.resolve(entry)
        if root is None:
            missing.append(entry)
            continue
        native = natives.setdefault(root, _Native(root))
        if entry.name == 'all':
            for harness in sorted(entry.harnesses):
                servers = native.codex if _is_codex(harness) else native.claude
                for name in servers:
                    claude_def = _prepare(servers[name], root) if not _is_codex(harness) else None
                    codex_def = _copy_table(servers[name]) if _is_codex(harness) else None
                    single = entry._replace(harnesses=frozenset({harness}))
                    install(single, name, selector(entry, name), claude_def, codex_def,
                            _uses_root(servers[name]))
            continue
        name = entry.name
        claude_def = _prepare(native.claude[name], root) if name in native.claude else None
        codex_def = _copy_table(native.codex[name]) if name in native.codex else None
        if claude_def is None and codex_def is None:
            raise ValueError(
                f'MCP server {name!r} not found in {native.root / CLAUDE_FILE} (mcpServers) '
                f'or {native.root / CODEX_FILE} ([mcp_servers])'
            )
        install(entry, name, selector(entry, name), claude_def, codex_def,
                _uses_root(native.claude.get(name)))
    return KindPlan(list(ops.values()), missing, list(dict.fromkeys(diagnostics)))


def _prepare(definition: Any, root: Path) -> Any:
    return _replace_root(plain(definition), str(root))


def _uses_root(definition: Any) -> bool:
    return definition is not None and PLUGIN_ROOT in json.dumps(plain(definition))


def _copy_table(table: Any) -> Any:
    """Deep copy of a Codex table; inner comments stay, trailing comments (the next section's) go."""
    clone = copy.deepcopy(table)
    body = getattr(getattr(clone, 'value', None), 'body', None)
    while body and body[-1][0] is None:
        body.pop()
    return clone
