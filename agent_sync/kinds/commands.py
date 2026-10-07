"""Plan explicit-invocation command skills without executing source content."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from agent_sync.context import Context
from agent_sync.engine import Op
from agent_sync.kinds.metadata import parse_markdown, parse_yaml
from agent_sync.kinds.source_files import (
    KindPlan,
    SourceResolver,
    discover_files,
    safe_path,
    selector,
)
from agent_sync.manifest import ManifestEntry
from agent_sync.sources import NAME_RE, discover_skills

_PRESERVED = {"argument-hint", "allowed-tools", "model", "context", "agent"}
_KNOWN = _PRESERVED | {"name", "description", "disable-model-invocation", "user-invocable", "skill"}
_ARGUMENTS = re.compile(r"\$(?:ARGUMENTS(?:\[\d+\])?|\d+)")
_NOTE = (
    "\n\n<!-- agent-sync argument compatibility -->\n"
    "On hosts without argument substitution, interpret the placeholders above using the "
    "explicit invocation's user arguments. Ask the user if required input is absent.\n"
)


def _render(metadata: dict[str, Any], body: str) -> bytes:
    return (
        "---\n" + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True) + "---\n" + body
    ).encode("utf-8")


def _tree(root: Path, folder: Path) -> dict[str, bytes]:
    """Snapshot resources; reject symlinks rather than accidentally following external data."""
    safe_path(root, folder)
    tree: dict[str, bytes] = {}
    for path in sorted(folder.rglob("*")):
        safe_path(root, path)
        if path.is_symlink():
            raise ValueError(f"Unsafe skill resource symlink: {path}")
        if path.is_file():
            tree[path.relative_to(folder).as_posix()] = path.read_bytes()
        elif not path.is_dir():
            raise ValueError(f"Unsupported skill resource: {path}")
    return tree


def _policy(tree: dict[str, bytes]) -> None:
    name = "agents/openai.yaml"
    try:
        data = parse_yaml(tree[name].decode("utf-8")) if name in tree else {}
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"Invalid {name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a mapping")  # noqa: TRY004 - invalid source document
    policy = data.setdefault("policy", {})
    if not isinstance(policy, dict):
        raise ValueError(f"{name} policy must be a mapping")  # noqa: TRY004 - invalid source document
    policy["allow_implicit_invocation"] = False
    tree[name] = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode("utf-8")


def _command(root: Path, path: Path, name: str) -> tuple[dict[str, bytes], list[str]]:
    metadata, body = parse_markdown(path)
    if "name" in metadata and metadata["name"] != name:
        raise ValueError(f"Command name conflicts with filename: {path}")
    diagnostics: list[str] = []
    tree: dict[str, bytes] = {}
    if "skill" in metadata:
        mapping = metadata["skill"]
        if not isinstance(mapping, str) or not NAME_RE.fullmatch(mapping):
            raise ValueError(f"Invalid command skill mapping: {path}")
        skills = discover_skills(root)
        if mapping not in skills:
            raise ValueError(f"Command {name} maps to missing skill {mapping}")
        folder = skills[mapping]
        tree = _tree(root, folder)
        metadata, body = parse_markdown(safe_path(root, folder / "SKILL.md"))
    else:
        unknown = sorted(set(metadata) - _KNOWN)
        if unknown:
            diagnostics.append(f"Command {name}: dropped metadata: {', '.join(unknown)}")
        metadata = {
            key: value
            for key, value in metadata.items()
            if key in _PRESERVED or key == "description"
        }
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        metadata["description"] = f"Run the {name} command."
    metadata["name"] = name
    metadata["disable-model-invocation"] = True
    metadata["user-invocable"] = True
    if _ARGUMENTS.search(body):
        body += _NOTE
        diagnostics.append(
            f"Command {name}: argument placeholders need host substitution or prompt fallback"
        )
    if _PRESERVED & metadata.keys():
        diagnostics.append(f"Command {name}: Claude-specific metadata is not portable to Codex")
    if "!`" in body:
        diagnostics.append(f"Command {name}: dynamic shell context is not executed or translated")
    if re.search(
        r"(?:\./|\.\./|(?:scripts|references|assets)/|\$\{?CLAUDE_(?:PLUGIN_ROOT|SKILL_DIR))", body
    ):
        diagnostics.append(f"Command {name}: source-relative resources are not rewritten")
    tree["SKILL.md"] = _render(metadata, body)
    _policy(tree)
    return tree, diagnostics


def plan_commands(entries: Sequence[ManifestEntry], ctx: Context) -> KindPlan:
    """Build one owned directory per selected command in the shared skills pool."""
    resolver = SourceResolver(ctx)
    missing: list[ManifestEntry] = []
    diagnostics: list[str] = []
    ops: dict[str, Op] = {}
    for entry in entries:
        root = resolver.resolve(entry)
        if root is None:
            missing.append(entry)
            continue
        found = discover_files(root, ("commands", ".claude/commands"), (".md",))
        if entry.name != "all" and entry.name not in found:
            raise ValueError(f"Unknown command {entry.name}. Available: {', '.join(sorted(found))}")
        names = sorted(found) if entry.name == "all" else [entry.name]
        if not names:
            diagnostics.append(f"No commands found in {root}.")
        for name in names:
            tree, warnings = _command(root, found[name][".md"], name)
            op = Op(
                "write", f".agents/skills/{name}", "commands", None, selector(entry, name), tree
            )
            key = name.casefold()
            if key in ops and ops[key] != op:
                raise ValueError(f"Collision for command: {name}")
            if key not in ops:
                ops[key] = op
                diagnostics.extend(warnings)
    return KindPlan(sorted(ops.values(), key=lambda op: op.target), missing, diagnostics)
