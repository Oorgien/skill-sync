"""Plan format-routed rule copies without interpreting their contents."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import NamedTuple

from agent_sync.context import Context
from agent_sync.engine import Op
from agent_sync.kinds.source_files import KindPlan, SourceResolver, read_utf8, safe_path, selector
from agent_sync.manifest import Harness, ManifestEntry
from agent_sync.sources import NAME_RE


class Rule(NamedTuple):
    path: Path
    relative: Path
    harness: Harness
    name: str
    grouped: bool

    @property
    def target(self) -> str:
        return f'.{self.harness}/rules/{self.relative.as_posix()}'


def _walk(
    root: Path, folder: Path, suffixes: dict[str, Harness], name: str = "all"
) -> Iterator[Path]:
    for path in sorted(folder.iterdir()):
        if name != "all" and path.name != name and path.stem != name:
            continue
        # Reject links before is_dir(), so directory links are never traversed.
        if path.is_symlink():
            safe_path(root, path)
        if path.is_dir():
            yield from _walk(root, path, suffixes)
        elif path.suffix in suffixes:
            safe_path(root, path)
            if not path.is_file():
                raise ValueError(f'Rule source is not a file: {path}')
            yield path


def _inventory(root: Path, harnesses: frozenset[Harness], name: str = "all") -> list[Rule]:
    rules: list[Rule] = []
    roots: tuple[tuple[str, dict[str, Harness]], ...] = (
        ('rules', {'.md': 'claude', '.rules': 'codex'}),
        ('.claude/rules', {'.md': 'claude'}),
        ('.codex/rules', {'.rules': 'codex'}),
    )
    for directory, formats in roots:
        suffixes: dict[str, Harness] = {
            ext: harness for ext, harness in formats.items() if harness in harnesses
        }
        if not suffixes:
            continue
        folder = safe_path(root, root / directory)
        if not folder.exists():
            continue
        if not folder.is_dir():
            raise ValueError(f'Rule source directory is not a directory: {folder}')
        for path in _walk(root, folder, suffixes, name):
            relative = path.relative_to(folder)
            grouped = len(relative.parts) > 1
            selected_name = relative.parts[0] if grouped else path.stem
            rules.append(Rule(path, relative, suffixes[path.suffix], selected_name, grouped))
    return rules


def _validate_name(rule: Rule) -> None:
    components = (*rule.relative.parts[:-1], rule.relative.stem)
    for component in components:
        if not NAME_RE.fullmatch(component):
            raise ValueError(f'Invalid rule path component: {component!r} in {rule.path}')


def plan_rules(entries: Sequence[ManifestEntry], ctx: Context) -> KindPlan:
    """Preserve each selected rule's bytes and relative layout in its native harness."""
    resolver = SourceResolver(ctx)
    ops: list[Op] = []
    missing: list[ManifestEntry] = []
    diagnostics: list[str] = []
    targets: dict[str, Path] = {}
    for entry in entries:
        root = resolver.resolve(entry)
        if root is None:
            missing.append(entry)
            continue
        inventory = _inventory(root, entry.harnesses, entry.name)
        chosen = [r for r in inventory if entry.name == 'all' or r.name == entry.name]
        if not chosen:
            if entry.name == 'all':
                diagnostics.append(f'No compatible rules found in {root}.')
                continue
            available_rules = _inventory(root, entry.harnesses)
            available = ', '.join(sorted({r.name for r in available_rules})) or 'none'
            raise ValueError(
                f"Rule '{entry.name}' not in {root}. Available rules: [{available}]."
            )
        if entry.name != 'all' and len({r.grouped for r in chosen}) > 1:
            raise ValueError(f'Ambiguous rule selector: {entry.name} names a file and directory')
        for rule in chosen:
            _validate_name(rule)
            if rule.target in targets:
                if targets[rule.target] != rule.path:
                    raise ValueError(f'Collision for rule: {rule.target}')
                continue
            payload = read_utf8(rule.path).encode('utf-8')
            targets[rule.target] = rule.path
            ops.append(Op('write', rule.target, 'rules', rule.harness,
                          selector(entry, rule.name), payload))
    return KindPlan(ops, missing, diagnostics)
