"""Native-first agent planning and explicit, prompt-only Markdown conversion."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import tomlkit

from agent_sync.context import Context
from agent_sync.engine import Op
from agent_sync.kinds.metadata import parse_markdown
from agent_sync.kinds.source_files import (
    KindPlan,
    SourceResolver,
    discover_files,
    read_utf8,
    safe_path,
    selector,
)
from agent_sync.manifest import ManifestEntry
from agent_sync.sources import NAME_RE


def _toml(path: Path) -> Any:
    try:
        return tomlkit.parse(read_utf8(path))
    except (ValueError, tomlkit.exceptions.ParseError) as exc:
        raise ValueError(f'Invalid TOML {path}: {exc}') from exc


def _registry(root: Path) -> dict[Path, tuple[str, str]]:
    path = safe_path(root, root / '.codex/config.toml')
    if not path.exists():
        return {}
    agents = _toml(path).get('agents', {})
    if not isinstance(agents, Mapping):
        raise ValueError(f'Invalid agents registry: {path}')  # noqa: TRY004
    result: dict[Path, tuple[str, str]] = {}
    for role, data in agents.items():
        if not isinstance(data, Mapping) or 'config_file' not in data:
            continue
        config = data['config_file']
        description = data.get('description')
        if not isinstance(role, str) or not NAME_RE.fullmatch(role):
            raise ValueError(f'Invalid registered agent name: {role!r}')
        if not isinstance(config, str) or Path(config).is_absolute() or '\\' in config:
            raise ValueError(f'Unsafe agent config_file: {config!r}')
        target = safe_path(root, path.parent / config)
        if not target.is_file():
            raise ValueError(f'Missing registered agent file: {target}')
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f'Missing registered agent description: {role}')
        if target in result:
            raise ValueError(f'Ambiguous registered agent file: {target}')
        result[target] = (role, description)
    return result


def _check_resources(data: Mapping[str, Any], path: Path) -> None:
    for key, value in data.items():
        if isinstance(value, Mapping):
            _check_resources(value, path)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    _check_resources(item, path)
        elif (key.endswith(('_file', '_path', '_dir')) or key in {'cwd', 'command', 'path'}) and value:
            raise ValueError(
                f'Cannot safely relocate resource setting {key!r} in {path}; '
                'provide a self-contained native agent definition.'
            )


def _native(path: Path, registry: dict[Path, tuple[str, str]]) -> str:
    data = _toml(path)
    identity = registry.get(path)
    changed = False
    if identity:
        for key, value in zip(('name', 'description'), identity):
            if key not in data:
                data[key] = value
                changed = True
    for key in ('name', 'description', 'developer_instructions'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f'Missing nonempty {key}: {path}')
    if not NAME_RE.fullmatch(data['name']):
        raise ValueError(f'Invalid native agent name: {path}')
    _check_resources(data, path)
    return tomlkit.dumps(data) if changed else read_utf8(path)


def _convert(path: Path) -> tuple[str, str]:
    metadata, body = parse_markdown(path, require_name=True)
    data = tomlkit.document()
    for key in ('name', 'description'):
        data[key] = metadata[key]
    data['developer_instructions'] = body
    omitted = ', '.join(sorted(set(metadata) - {'name', 'description'})) or 'none'
    return tomlkit.dumps(data), (
        f'{path.stem}: prompt-only Codex conversion; omitted metadata: {omitted}. '
        'Codex inherits its own model and permissions; provide native TOML when controls matter.'
    )


def _discover(root: Path) -> dict[str, dict[str, Path]]:
    found: dict[str, dict[str, Path]] = {}
    folded: dict[str, str] = {}
    for directory, suffixes in (
        ('agents', ('.md', '.toml')), ('.claude/agents', ('.md',)),
        ('.codex/agents', ('.toml',)),
    ):
        for name, formats in discover_files(root, (directory,), suffixes).items():
            previous = folded.setdefault(name.casefold(), name)
            if previous != name:
                raise ValueError(f'Case collision: {previous} and {name}')
            existing = found.setdefault(name, {})
            if existing.keys() & formats.keys():
                raise ValueError(f'Ambiguous source definition: {name}')
            existing.update(formats)
    return found


def plan_agents(entries: Sequence[ManifestEntry], ctx: Context) -> KindPlan:
    resolver = SourceResolver(ctx)
    ops: dict[str, Op] = {}
    origins: dict[str, Path] = {}
    runtime_names: dict[str, str] = {}
    missing: list[ManifestEntry] = []
    diagnostics: list[str] = []
    for entry in entries:
        root = resolver.resolve(entry)
        if root is None:
            missing.append(entry)
            continue
        found = _discover(root)
        registry = _registry(root)
        names = sorted(found) if entry.name == 'all' else [entry.name]
        if not names:
            diagnostics.append(f'No agents found in {root}.')
        for name in names:
            if name not in found:
                raise ValueError(f'Unknown agent {name!r}. Available agents: {", ".join(sorted(found)) or "none"}')
            formats = found[name]
            compatible = False
            for harness in ('claude', 'codex'):
                if harness not in entry.harnesses:
                    continue
                path = formats.get('.md') if harness == 'claude' else formats.get('.toml', formats.get('.md'))
                if path is None:
                    diagnostics.append(f'{name}: no Claude representation exists.')
                    continue
                compatible = True
                if harness == 'claude':
                    parse_markdown(path, require_name=True)
                    payload = read_utf8(path)
                elif path.suffix == '.toml':
                    payload = _native(path, registry)
                else:
                    payload, diagnostic = _convert(path)
                    diagnostics.append(diagnostic)
                extension = 'md' if harness == 'claude' else 'toml'
                target = f'.{harness}/agents/{name}.{extension}'
                folded = target.casefold()
                if folded in ops:
                    if ops[folded].target != target or origins[folded] != path:
                        raise ValueError(f'Collision for agent target: {target}')
                    continue
                ops[folded] = Op('write', target, 'agents', harness, selector(entry, name), payload.encode('utf-8'))
                if harness == 'codex':
                    runtime_name = str(tomlkit.parse(payload)['name']).casefold()
                    previous_target = runtime_names.setdefault(runtime_name, target)
                    if previous_target != target:
                        raise ValueError(f'Collision for Codex runtime agent name: {runtime_name}')
                origins[folded] = path
            if not compatible and entry.name != 'all':
                raise ValueError(f'Agent {name!r} has no compatible representation. Available agents: {", ".join(sorted(found))}')
    return KindPlan(list(ops.values()), missing, list(dict.fromkeys(diagnostics)))
