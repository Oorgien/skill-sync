"""Parse source Markdown as data, preserving its instruction body."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agent_sync.kinds.source_files import read_utf8


def parse_markdown(path: Path, *, require_name: bool = False) -> tuple[dict[str, Any], str]:
    text = read_utf8(path)
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        if require_name:
            raise ValueError(f'Missing YAML frontmatter: {path}')
        if not text.strip():
            raise ValueError(f'Empty instruction body: {path}')
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        raise ValueError(f'Unclosed YAML frontmatter: {path}')
    try:
        metadata = parse_yaml(''.join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise ValueError(f'Invalid YAML frontmatter: {path}: {exc}') from exc
    if not isinstance(metadata, dict) or any(not isinstance(k, str) for k in metadata):
        raise ValueError(f'Frontmatter must be a string-keyed mapping: {path}')
    if require_name:
        for key in ('name', 'description'):
            if not isinstance(metadata.get(key), str) or not metadata[key].strip():
                raise ValueError(f'Missing nonempty {key}: {path}')
        if metadata['name'] != path.stem:
            raise ValueError(f'Agent name must match filename: {path}')
    body = ''.join(lines[end + 1:])
    if not body.strip():
        raise ValueError(f'Empty instruction body: {path}')
    return metadata, body


class _UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        keys: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in keys:
                    raise ValueError(f'Duplicate YAML key: {key!r}')
                keys.add(key)
            except TypeError as exc:
                raise ValueError('YAML mapping keys must be scalar values') from exc
        return super().construct_mapping(node, deep=deep)


def parse_yaml(text: str) -> Any:
    """Load safe YAML while rejecting ambiguous duplicate mapping keys."""
    try:
        return yaml.load(text, Loader=_UniqueLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f'Invalid YAML: {exc}') from exc
