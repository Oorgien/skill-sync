"""Plan hooks: declared in the manifest, or one handler picked from a source's native hook file."""

from __future__ import annotations

import copy
import json
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_sync.context import Context
from agent_sync.engine import Op
from agent_sync.gitexclude import is_tracked
from agent_sync.kinds.source_files import KindPlan, SourceResolver, read_utf8, safe_path
from agent_sync.manifest import Harness, ManifestEntry
from agent_sync.sources import GITHUB_RE, NAME_RE
from agent_sync.state import State

# Hook events each harness documents. Re-check when the docs change.
# Claude Code: https://code.claude.com/docs/en/hooks (fetched 2026-10-07).
CLAUDE_EVENTS = frozenset({
    "SessionStart", "Setup", "UserPromptSubmit", "UserPromptExpansion", "PreToolUse",
    "PermissionRequest", "PermissionDenied", "PostToolUse", "PostToolUseFailure",
    "PostToolBatch", "Notification", "MessageDisplay", "SubagentStart", "SubagentStop",
    "TaskCreated", "TaskCompleted", "Stop", "StopFailure", "TeammateIdle",
    "InstructionsLoaded", "ConfigChange", "CwdChanged", "DirectoryAdded", "FileChanged",
    "WorktreeCreate", "WorktreeRemove", "PreCompact", "PostCompact", "PreModelSwitch",
    "PostModelSwitch", "Elicitation", "ElicitationResult", "SessionEnd",
})
# Codex: https://learn.chatgpt.com/docs/hooks.md (fetched 2026-10-07).
CODEX_EVENTS = frozenset({
    "PreToolUse", "PermissionRequest", "PostToolUse", "PreCompact", "PostCompact",
    "UserPromptSubmit", "SubagentStop", "Stop", "Interrupt", "SessionStart",
    "SubagentStart", "SessionEnd",
})
EVENTS: dict[Harness, frozenset[str]] = {"claude": CLAUDE_EVENTS, "codex": CODEX_EVENTS}

_TARGETS: dict[Harness, str] = {"claude": ".claude/settings.local.json", "codex": ".codex/hooks.json"}
_ROOT_VARS: dict[Harness, str] = {"claude": "CLAUDE_PLUGIN_ROOT", "codex": "PLUGIN_ROOT"}
_PLUGIN_JSON: dict[Harness, str] = {
    "claude": ".claude-plugin/plugin.json", "codex": ".codex-plugin/plugin.json",
}
_DEFAULT_FILE: dict[Harness, str] = {"claude": "hooks/hooks.json", "codex": ".codex/hooks.json"}
_POSIX_SHELLS = {"bash", "sh", "zsh"}
_DECLARED_KEYS = {"name", "source", "event", "matcher", "command", "timeout"}


def _json(path: Path) -> Any:
    try:
        return json.loads(read_utf8(path))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON {path}: {exc}") from exc


def _where(entry: ManifestEntry, name: Any) -> str:
    section = "common" if len(entry.harnesses) > 1 else next(iter(entry.harnesses))
    return f"hooks.{section} hook {name!r}"


def _source_entry(value: Any, entry: ManifestEntry, where: str) -> ManifestEntry:
    if value == "personal":
        return ManifestEntry("hooks", entry.harnesses, "personal", None, "all")
    if isinstance(value, str) and value.startswith("github.com:"):
        repo = value.removeprefix("github.com:")
        if GITHUB_RE.fullmatch(repo):
            return ManifestEntry("hooks", entry.harnesses, "github.com", repo, "all")
    raise ValueError(f"{where}: invalid source {value!r}; use 'github.com:<owner/repo>' or 'personal'")


def _native_file(root: Path, harness: Harness) -> Path:
    plugin = safe_path(root, root / _PLUGIN_JSON[harness])
    relative = _DEFAULT_FILE[harness]
    if plugin.is_file():
        declared = _json(plugin)
        if isinstance(declared, Mapping) and isinstance(declared.get("hooks"), str):
            relative = declared["hooks"]
    path = safe_path(root, root / relative)
    if not path.is_file():
        raise ValueError(f"No {harness} hook file in {root}: expected {relative}")
    return path


def _groups(path: Path) -> list[tuple[str, dict[str, Any]]]:
    data = _json(path)
    hooks = data.get("hooks") if isinstance(data, Mapping) else None
    if not isinstance(hooks, Mapping):
        raise ValueError(  # noqa: TRY004
f"Invalid hook file {path}: expected an object with 'hooks'")
    found: list[tuple[str, dict[str, Any]]] = []
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError(f"Invalid hook file {path}: {event!r} must be a list")  # noqa: TRY004
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
                raise ValueError(f"Invalid hook file {path}: bad group under {event!r}")  # noqa: TRY004
            found.append((str(event), group))
    return found


def _describe(event: str, group: Mapping[str, Any], handler: Mapping[str, Any]) -> str:
    # Commands often share a long prefix, so show the END, where they differ.
    command = str(handler.get("command", ""))
    excerpt = command if len(command) <= 80 else "..." + command[-80:]
    ident = f"id={group['id']!r} " if "id" in group else ""
    return f"  - event={event} {ident}matcher={group.get('matcher')!r} command={excerpt!r}"


def _pick(path: Path, pick: Mapping[str, Any], where: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    candidates: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    in_event: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for event, group in _groups(path):
        for handler in group.get("hooks", []):
            if not isinstance(handler, dict):
                continue
            if "id" in pick:
                ok = group.get("id") == pick["id"]
            else:
                if event == pick["event"]:
                    in_event.append((event, group, handler))
                ok = (
                    event == pick["event"]
                    and pick["contains"] in str(handler.get("command", ""))
                    and ("matcher" not in pick or group.get("matcher") == pick["matcher"])
                )
            if ok:
                candidates.append((event, group, handler))
    if len(candidates) != 1:
        if candidates:
            lines = "\n".join(_describe(e, g, h) for e, g, h in candidates)
            problem = (
                f"pick {dict(pick)!r} must match exactly one handler, "
                f"found {len(candidates)} in {path}:\n{lines}"
            )
        elif "id" in pick:
            ids = sorted({str(g["id"]) for _, g in _groups(path) if "id" in g})
            problem = f"no handler has group id {pick['id']!r} in {path}. Group ids: {', '.join(ids) or 'none'}"
        else:
            listing = "\n".join(_describe(e, g, h) for e, g, h in in_event) or "  (no handlers)"
            matcher = f" with matcher {pick['matcher']!r}" if "matcher" in pick else ""
            problem = (
                f"no handler in {pick['event']} contains {pick['contains']!r}{matcher} in {path}. "
                f"Handlers in {pick['event']}:\n{listing}"
            )
        raise ValueError(f"{where}: {problem}")
    return candidates[0]


def _validate(entry: ManifestEntry, seen: set[tuple[str, str]]) -> dict[str, Any]:
    raw = entry.raw
    name = raw.get("name") if isinstance(raw, Mapping) else None
    where = _where(entry, name)
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError(f"{where}: 'name' is required and must match {NAME_RE.pattern}")
    for harness in entry.harnesses:
        if (harness, name) in seen:
            raise ValueError(f"{where}: duplicate hook name for {harness}")
    is_pick = "pick" in raw
    allowed = {"name", "source", "pick"} if is_pick else _DECLARED_KEYS
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(map(repr, unknown))}")
    if "source" not in raw and (is_pick or "{source}" in str(raw.get("command", ""))):
        raise ValueError(f"{where}: 'source' is required for a pick or a command using {{source}}")
    if "source" in raw:
        _source_entry(raw["source"], entry, where)
    if is_pick:
        pick = raw["pick"]
        if len(entry.harnesses) > 1:
            raise ValueError(f"{where}: a pick is harness-specific and is not allowed in common")
        if not isinstance(pick, dict):
            raise ValueError(f"{where}: 'pick' must be a mapping")
        if set(pick) == {"id"} and isinstance(pick["id"], str) and pick["id"]:
            return dict(raw)
        extra = set(pick) - {"event", "contains", "matcher"}
        if extra or "id" in pick:
            raise ValueError(
                f"{where}: invalid pick keys {sorted(pick)}; "
                "use either 'id' or 'event' + 'contains' [+ 'matcher']"
            )
        for key in ("event", "contains"):
            if not isinstance(pick.get(key), str) or not pick[key]:
                raise ValueError(f"{where}: pick needs 'id', or 'event' and 'contains' (string '{key}' missing)")
        if "matcher" in pick and not isinstance(pick["matcher"], str):
            raise ValueError(f"{where}: pick 'matcher' must be a string")
        return dict(raw)
    event, command = raw.get("event"), raw.get("command")
    if not isinstance(event, str) or not event:
        raise ValueError(f"{where}: 'event' is required")
    if not isinstance(command, str) or not command.strip():
        raise ValueError(f"{where}: 'command' is required and must be a string")
    if "matcher" in raw and not isinstance(raw["matcher"], str):
        raise ValueError(f"{where}: 'matcher' must be a string")
    timeout = raw.get("timeout")
    if "timeout" in raw and (isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0):
        raise ValueError(f"{where}: 'timeout' must be a positive integer")
    return dict(raw)


def _check_event(event: str, harness: Harness, where: str) -> None:
    if event not in EVENTS[harness]:
        raise ValueError(
            f"{where}: event {event!r} is not a {harness} hook event. "
            f"Valid events: {', '.join(sorted(EVENTS[harness]))}"
        )


def _moved_groups(state: State, ops: Sequence[Op]) -> list[Op]:
    """Removes for groups recorded under another event: the name is the hook's identity."""
    removes: list[Op] = []
    for op in ops:
        assert op.locator is not None
        for item in state.items:
            old = item.locator
            if (
                item.kind == "hooks" and item.harness == op.harness and item.target == op.target
                and old is not None and len(old) == 3 and old[2] == op.locator[2]
                and old[1] != op.locator[1]
            ):
                removes.append(Op(
                    "remove", item.target, "hooks", item.harness, item.source, None, old,
                    container="list",
                ))
    return removes


def plan_hooks(entries: Sequence[ManifestEntry], ctx: Context) -> KindPlan:
    resolver = SourceResolver(ctx)
    ops: list[Op] = []
    missing: list[ManifestEntry] = []
    seen: set[tuple[str, str]] = set()
    validated: list[tuple[ManifestEntry, dict[str, Any]]] = []
    for entry in entries:  # validate everything first, so a missing source can't hide an error
        raw = _validate(entry, seen)
        validated.append((entry, raw))
        seen.update((harness, raw["name"]) for harness in entry.harnesses)
    for entry, raw in validated:
        name: str = raw["name"]
        where = _where(entry, name)
        root: Path | None = None
        if "source" in raw:
            source_entry = _source_entry(raw["source"], entry, where)
            root = resolver.resolve(source_entry)
            if root is None:
                missing.append(source_entry)
                continue
        for harness in ("claude", "codex"):
            if harness not in entry.harnesses:
                continue
            if "pick" in raw:
                assert root is not None
                event, group, handler = _pick(_native_file(root, harness), raw["pick"], where)
                if not isinstance(handler.get("command"), str):
                    raise ValueError(f"{where}: the picked handler has no command to install")
                if "args" in handler:
                    raise ValueError(
                        f"{where}: the picked handler {str(handler.get('command'))!r} uses exec form "
                        "('args'), which has no shell to run the CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT "
                        "export prefix; declare the hook in the manifest instead."
                    )
                if handler.get("shell", "bash") not in _POSIX_SHELLS:
                    raise ValueError(
                        f"{where}: the picked handler {str(handler.get('command'))!r} runs in "
                        f"shell {handler['shell']!r}, which can't take a POSIX 'export' prefix; "
                        "declare the hook in the manifest instead."
                    )
                copied = copy.deepcopy(handler)
                copied["command"] = (
                    f"export {_ROOT_VARS[harness]}={shlex.quote(str(root))}; {handler['command']}"
                )
                payload: dict[str, Any] = {}
                if isinstance(group.get("matcher"), str):
                    payload["matcher"] = group["matcher"]
                payload["hooks"] = [copied]
            else:
                event = raw["event"]
                command = raw["command"]
                if root is not None:
                    command = command.replace("{source}", shlex.quote(str(root)))
                handler = {"type": "command", "command": command}
                if "timeout" in raw:
                    handler["timeout"] = raw["timeout"]
                payload = {}
                if "matcher" in raw:
                    payload["matcher"] = raw["matcher"]
                payload["hooks"] = [handler]
            _check_event(event, harness, where)
            ops.append(Op(
                "merge", _TARGETS[harness], "hooks", harness, f"hooks:{name}", payload,
                locator=("hooks", event, name), container="list",
            ))
    ops.extend(_moved_groups(ctx.state, ops))
    for target in dict.fromkeys(op.target for op in ops):
        if is_tracked(ctx.project, target):
            raise ValueError(
                f"{target} is tracked by git. agent-sync's hook commands contain paths into the "
                "local cache, so it won't write them into a file git tracks. Untrack the file "
                "(or add it to .gitignore and `git rm --cached` it) and run again."
            )
    return KindPlan(ops, missing, [])
