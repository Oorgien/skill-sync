"""Safe, bounded source discovery shared by file-based planners."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from agent_sync.context import Context
from agent_sync.engine import Op
from agent_sync.manifest import ManifestEntry
from agent_sync.sources import NAME_RE, Entry, resolve_source


class KindPlan(NamedTuple):
    ops: list[Op]
    missing: list[ManifestEntry]
    diagnostics: list[str]


class SourceResolver:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.resolved: dict[tuple[str, str | None], Path | None] = {}

    def resolve(self, entry: ManifestEntry) -> Path | None:
        if entry.source == 'npx':
            raise ValueError('npx sources are only supported for skills')
        key = (entry.source, entry.repo)
        if key not in self.resolved:
            self.resolved[key] = resolve_source(
                # Resolve the repository inventory; its items are not necessarily skills.
                Entry(entry.source, entry.repo, 'all'), self.ctx.personal_dir,
                self.ctx.cache_root, self.ctx.apply, self.ctx.update,
            )
        return self.resolved[key]


def selector(entry: ManifestEntry, name: str) -> str:
    return f'personal:{name}' if entry.source == 'personal' else f'{entry.source}:{entry.repo}:{name}'


def safe_path(root: Path, path: Path) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f'Path escapes source: {path}') from exc
    if '..' in relative.parts:
        raise ValueError(f'Path escapes source: {path}')
    current = root
    for part in ('.', *relative.parts):
        current = current / part
        if current.is_symlink():
            raise ValueError(f'Symlinked source path: {current}')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Path escapes source: {path}')
    return path


def read_utf8(path: Path) -> str:
    try:
        return path.read_bytes().decode('utf-8')
    except (UnicodeError, OSError) as exc:
        raise ValueError(f'Cannot read UTF-8 source {path}: {exc}') from exc


def discover_files(
    root: Path, directories: Sequence[str], suffixes: Sequence[str],
) -> dict[str, dict[str, Path]]:
    found: dict[str, dict[str, Path]] = {}
    folded: dict[str, str] = {}
    for directory in directories:
        folder = safe_path(root, root / directory)
        if not folder.exists():
            continue
        if not folder.is_dir():
            raise ValueError(f'Source directory is not a directory: {folder}')
        for path in sorted(folder.iterdir()):
            if path.suffix not in suffixes:
                continue
            safe_path(root, path)
            if not path.is_file():
                raise ValueError(f'Source candidate is not a file: {path}')
            name = path.stem
            if not NAME_RE.fullmatch(name):
                raise ValueError(f'Invalid source name: {name!r}')
            previous = folded.setdefault(name.casefold(), name)
            if previous != name:
                raise ValueError(f'Case collision: {previous} and {name}')
            formats = found.setdefault(name, {})
            if path.suffix in formats:
                raise ValueError(f'Ambiguous source definition: {name}{path.suffix}')
            formats[path.suffix] = path
    return found
