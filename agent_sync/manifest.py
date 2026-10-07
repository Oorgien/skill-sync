"""Manifest format v2: typed entries for every kind, with the harnesses each one targets."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, NamedTuple, cast

import yaml

from agent_sync.sources import NAME_RE, parse_entry

Kind = Literal["skills", "agents", "commands", "rules", "hooks", "mcp"]
Harness = Literal["claude", "codex"]

KINDS: tuple[Kind, ...] = ("skills", "agents", "commands", "rules", "hooks", "mcp")
SECTIONS = ("common", "claude", "codex")

_BOTH: frozenset[Harness] = frozenset({"claude", "codex"})
# Kinds that share one pool across harnesses: every section installs for both.
_SHARED_POOL: frozenset[Kind] = frozenset({"skills", "commands"})


class ManifestEntry(NamedTuple):
    kind: Kind
    harnesses: frozenset[Harness]
    source: str  # "github.com" | "personal" | "npx" ("" for hooks)
    repo: str | None  # owner/repo or npm package; None for personal and hooks
    name: str  # item name, or "all"; "" for hooks
    raw: Any = None  # hooks and inline mcp servers: the raw mapping, unparsed


def _harnesses(kind: Kind, section: str) -> frozenset[Harness]:
    if kind in _SHARED_POOL or section == "common":
        return _BOTH
    return frozenset({cast(Harness, section)})


def _parse_item(
    kind: Kind, section: str, harnesses: frozenset[Harness], item: Any
) -> list[ManifestEntry]:
    where = f"{kind}.{section} item {item!r}"
    if kind == "hooks":
        if not isinstance(item, dict):
            raise ValueError(f"Invalid {where}: expected a mapping")
        return [ManifestEntry(kind, harnesses, "", None, "", item)]
    if kind == "mcp" and isinstance(item, dict) and "name" in item:
        name = item["name"]
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid {where}: invalid item name {name!r}")
        return [ManifestEntry(kind, harnesses, "", None, name, item)]
    try:
        parsed = parse_entry(item)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {where}: {exc}") from exc
    for entry in parsed:
        if entry.source == "npx" and kind != "skills":
            raise ValueError(f"Invalid {where}: npx sources are only supported for skills")
        if not NAME_RE.fullmatch(entry.skill):
            raise ValueError(f"Invalid {where}: invalid item name {entry.skill!r}")
    return [ManifestEntry(kind, harnesses, e.source, e.repo, e.skill) for e in parsed]


def parse_manifest(data: Any) -> list[ManifestEntry]:
    """Turn yaml.safe_load output into entries, ordered by kind, section, then file order."""
    if data is None:
        return []
    if not isinstance(data, dict):
        raise ValueError(f"Manifest must be a mapping of kinds, got: {data!r}")  # noqa: TRY004
    unknown = [key for key in data if key not in KINDS]
    if unknown:
        raise ValueError(f"Unknown manifest key: {unknown[0]!r}. Valid keys: {', '.join(KINDS)}")

    entries: list[ManifestEntry] = []
    for kind in KINDS:
        sections = data.get(kind)
        if not sections:
            continue
        if not isinstance(sections, dict):
            raise ValueError(  # noqa: TRY004
                f"'{kind}' must be a mapping of sections ({', '.join(SECTIONS)})"
            )
        bad = [name for name in sections if name not in SECTIONS]
        if bad:
            raise ValueError(
                f"Unknown section {bad[0]!r} in '{kind}'. Valid sections: {', '.join(SECTIONS)}"
            )
        for section in SECTIONS:
            items = sections.get(section)
            if items is None:
                continue
            if not isinstance(items, list):
                raise ValueError(  # noqa: TRY004
                    f"'{kind}.{section}' must be a list, got: {items!r}"
                )
            harnesses = _harnesses(kind, section)
            for item in items:
                entries.extend(_parse_item(kind, section, harnesses, item))
    return entries


def load_manifest(path: Path) -> list[ManifestEntry]:
    """Read a YAML manifest file and parse it."""
    return parse_manifest(yaml.safe_load(path.read_text(encoding="utf-8")))
