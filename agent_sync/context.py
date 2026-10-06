"""What a planner needs to know about this run."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from agent_sync.state import State


class Context(NamedTuple):
    """The project, the places sources live, the run's flags and the state read at the start."""

    project: Path  # resolved project root
    personal_dir: Path | None  # scanned like a source for `personal:`; None = not configured
    cache_root: Path  # downloaded github/npx sources
    apply: bool  # download missing sources (a dry run never downloads)
    update: bool  # refresh sources that are already downloaded
    prune: bool  # also plan the removal of old installs' links that have no record
    state: State  # what agent-sync already owns in this project
