"""Command line interface: argument parsing, orchestration, and error handling."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

from agent_sync.kinds.skills import ensure_claude_skills_link, link_skills, prune_managed_links
from agent_sync.sources import Entry, discover_skills, parse_entry, resolve_source


def main() -> None:
    parser = argparse.ArgumentParser(description="Skill sync tool")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to the YAML manifest. Defaults to config.yaml in --project (the current project directory).",
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=Path.cwd(),
        help="Project to install skills into. Defaults to the current directory.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help=(
            "Shared cache for downloaded sources (github/npx). Defaults to $SKILL_SYNC_CACHE, "
            "or .skill-sources/ next to this script (the hub). One cache for all projects."
        ),
    )
    parser.add_argument("--apply", action="store_true", help="Download and symlink skills")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Refresh already-downloaded sources (git pull / redownload) to pick up new upstream skills",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Remove tool-managed symlinks in .agents/skills that are no longer in the manifest",
    )
    args = parser.parse_args()

    hub_root = Path(__file__).resolve().parent.parent
    project_root = args.project.resolve()
    if args.cache_dir is not None:
        cache_root = args.cache_dir.expanduser().resolve()
    elif os.environ.get("SKILL_SYNC_CACHE"):
        cache_root = Path(os.environ["SKILL_SYNC_CACHE"]).expanduser().resolve()
    else:
        cache_root = hub_root / ".skill-sources"
    config_path = args.config if args.config is not None else project_root / "config.yaml"
    if not config_path.is_file():
        raise ValueError(
            f"Config not found: {config_path}. Copy config.yaml from the hub to {project_root}, "
            "or specify its path with --config."
        )
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    skills_sec = data.get("skills") or {}

    links: dict[Path, Path] = {}
    missing: list[Entry] = []

    # The same source (GitHub repository / npm package) may appear multiple times
    # in the manifest, once for each requested skill. Resolve it only once per run
    # to avoid a separate `git pull` on the same clone for every entry with --update.
    # For npx, the skill affects the download, so it is part of the key;
    # for GitHub, it is not.
    resolved: dict[tuple[str, str | None, str | None], Path | None] = {}

    for agent in ("common", "codex", "claude"):
        for item in (skills_sec.get(agent) or []):
            for entry in parse_entry(item):
                cache_key = (
                    entry.source,
                    entry.repo,
                    entry.skill if entry.source == "npx" else None,
                )
                if cache_key not in resolved:
                    resolved[cache_key] = resolve_source(
                        entry, hub_root, cache_root, args.apply, args.update
                    )
                src_root = resolved[cache_key]
                if not src_root:
                    missing.append(entry)
                    continue

                disc = discover_skills(src_root)
                targets = disc if entry.skill == "all" else {entry.skill: disc[entry.skill]} if entry.skill in disc else None
                if targets is None:
                    available = ", ".join(sorted(disc.keys())) or "none"
                    hint = " Re-run with --update to refresh the cached source." if not args.update else ""
                    raise ValueError(f"Skill '{entry.skill}' not in {src_root}. Available skills: [{available}].{hint}")

                for name, src_path in targets.items():
                    target_link = project_root / ".agents" / "skills" / name
                    if target_link in links and links[target_link] != src_path:
                        raise ValueError(f"Collision for skill: {name}")
                    links[target_link] = src_path

    for entry in missing:
        print(f"Missing source (run with --apply): {entry.source}/{entry.repo}")

    pruned = 0
    if args.prune:
        pruned = prune_managed_links(
            project_root, hub_root, cache_root, {t.name for t in links}, args.apply
        )

    link_skills(links, hub_root, cache_root, args.apply)

    summary = f"{'Applied' if args.apply else 'Dry-run:'} {len(links)} links, {len(missing)} missing"
    if args.prune:
        summary += f", {pruned} pruned"
    print(summary + ".")

    ensure_claude_skills_link(project_root, args.apply)


def cli() -> None:
    try:
        main()
    except Exception as err:  # noqa: BLE001
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
