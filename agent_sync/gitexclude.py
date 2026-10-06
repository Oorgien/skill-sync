"""The agent-sync block in a repository's `.git/info/exclude`.

Each agent-sync project owns one marked block, named by the project's absolute path. Projects
in one repository, including its linked worktrees (which share one exclude file), therefore never
overwrite each other. The path only has to be unique on this machine: the file is never
committed.

Only this project's block is rewritten, plus the blocks of projects whose folder no longer exists
(removed worktrees, moved projects). Every other line of the file is kept byte for byte.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import NamedTuple

START_FORMAT = "# >>> agent-sync (managed) project={} >>>"
END_FORMAT = "# <<< agent-sync (managed) project={} <<<"
FOREIGN_START_RE = re.compile(r"^# >>> agent-sync \(managed\) project=(.+) >>>$")
GLOB_CHARS_RE = re.compile(r"([\\*?\[])")


class ExcludeResult(NamedTuple):
    path: Path | None  # the exclude file; None outside git
    changed: bool  # True if the file content would change / changed
    note: str | None  # why the step was skipped, if it was


def sync_exclude(project: Path, paths: Iterable[str], apply: bool) -> ExcludeResult:
    """Make this project's block list exactly `paths` (project-relative POSIX paths)."""
    entries = sorted({_normalize(path) for path in paths})
    marker = str(project.resolve())
    if "\n" in marker or "\r" in marker:
        raise ValueError(
            f"Project path {marker!r} contains a newline, which cannot go into a marker line; "
            "move the project to a path without newlines."
        )
    located = _locate(project)
    if isinstance(located, str):
        return ExcludeResult(None, False, located)
    exclude, prefix = located

    lines = [f"/{_escape(prefix + entry)}" for entry in entries]
    old = _read(exclude)
    new = _rewrite(old, marker, lines)
    if new == old:
        return ExcludeResult(exclude, False, None)
    if apply:
        _write_atomic(exclude, new)
    return ExcludeResult(exclude, True, None)


def _normalize(path: str) -> str:
    """Check one project-relative path and return it in plain POSIX form."""
    if not path or "\n" in path or "\r" in path:
        raise ValueError(f"Invalid exclude path {path!r}: use a non-empty path without newlines.")
    if path.startswith("/") or "\\" in path:
        raise ValueError(f"Invalid exclude path {path!r}: use a project-relative POSIX path.")
    posix = PurePosixPath(path)
    if ".." in posix.parts:
        raise ValueError(f"Invalid exclude path {path!r}: remove the '..' segments.")
    if not posix.parts:
        raise ValueError(f"Invalid exclude path {path!r}: name a file or folder in the project.")
    return posix.as_posix()


def _escape(pattern: str) -> str:
    """Make git treat the pattern literally (no globbing, no trailing-space stripping)."""
    escaped = GLOB_CHARS_RE.sub(r"\\\1", pattern)
    return escaped[:-1] + "\\ " if escaped.endswith(" ") else escaped


def _locate(project: Path) -> tuple[Path, str] | str:
    """Return (exclude file, project prefix such as 'apps/web/'), or a note why not."""
    command = ["git", "rev-parse", "--git-path", "info/exclude", "--show-prefix"]
    try:
        done = subprocess.run(
            command, cwd=str(project), capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        if project.is_dir():
            return "git not found; skipped .git/info/exclude"
        return f"not a git repository: {project}; skipped .git/info/exclude"
    except OSError as error:
        return f"cannot run git in {project}: {error}; skipped .git/info/exclude"
    answer = done.stdout.split("\n")
    if done.returncode != 0 or len(answer) < 2 or not answer[0]:
        return f"not a git repository: {project}; skipped .git/info/exclude"
    exclude = Path(answer[0])
    if not exclude.is_absolute():
        exclude = project / exclude
    return exclude, answer[1]


def _read(exclude: Path) -> str:
    if not exclude.exists():
        return ""
    # surrogateescape keeps undecodable bytes in user lines unchanged on the way back out.
    return exclude.read_bytes().decode("utf-8", errors="surrogateescape")


def _is_dead_block_start(stripped: str) -> str | None:
    """Return the project path if this line opens a block of a project whose folder is gone."""
    found = FOREIGN_START_RE.match(stripped)
    if found is None:
        return None
    project = found.group(1)
    if not os.path.isabs(project) or os.path.isdir(project):
        return None
    return project


def _rewrite(text: str, marker: str, lines: list[str]) -> str:
    """Replace, add or drop this project's block in `text`, and drop blocks of missing folders.

    Every other line is kept as it is. A block of another project that has no closing marker or
    whose path is not absolute is left alone.
    """
    start = START_FORMAT.format(marker)
    end = END_FORMAT.format(marker)
    source = text.splitlines(keepends=True)
    kept: list[str] = []
    at: int | None = None  # where the block goes among the kept lines
    inside = False
    index = 0
    while index < len(source):
        line = source[index]
        stripped = line.rstrip("\r\n")
        index += 1
        if inside:
            inside = stripped != end
            continue
        if stripped == start:
            inside = True
            if at is None:
                at = len(kept)
            continue
        dead = _is_dead_block_start(stripped)
        if dead is not None and dead != marker:
            dead_end = END_FORMAT.format(dead)
            closing = next(
                (i for i in range(index, len(source)) if source[i].rstrip("\r\n") == dead_end),
                None,
            )
            if closing is not None:
                index = closing + 1
                continue
        kept.append(line)
    if inside:
        raise ValueError(
            f"The block for project {marker!r} in the git exclude file has no closing marker "
            f"{end!r}; add it or delete the block by hand, then run agent-sync again."
        )
    if not lines:
        return "".join(kept)
    block = [start + "\n", *(f"{line}\n" for line in lines), end + "\n"]
    if at is not None:
        return "".join(kept[:at] + block + kept[at:])
    if kept and not kept[-1].endswith("\n"):
        kept[-1] += "\n"
    return "".join(kept + block)


def _write_atomic(exclude: Path, text: str) -> None:
    target = exclude.resolve() if exclude.is_symlink() else exclude
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
    handle, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".exclude-")
    try:
        with os.fdopen(handle, "wb") as temp:
            temp.write(text.encode("utf-8", errors="surrogateescape"))
        os.chmod(temp_name, mode)
        os.replace(temp_name, target)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise
