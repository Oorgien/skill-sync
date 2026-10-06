"""Test harness for the skill sync tool.

Every test drives the tool through `run_sync`, the single adapter that knows how the tool is
started. The tool always runs as a subprocess with an explicit environment: an empty HOME, no
network (GitHub is redirected to local git repos), a fake `npx` on PATH and no inherited
SKILL_SYNC_CACHE.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

FAKE_NPX = """\
#!/bin/sh
# Fake npx: records its arguments and working directory, then "installs" one skill the way
# `npx <pkg> install --skills <skill> --target claude-project` would.
{
    echo "cwd=$(pwd)"
    echo "args=$*"
} >> "$FAKE_NPX_LOG"
skill=""
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--skills" ]; then
        skill="$2"
    fi
    shift
done
mkdir -p ".claude/skills/$skill"
echo "# $skill installed by fake npx" > ".claude/skills/$skill/SKILL.md"
"""

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_COMMITTER_NAME": "Test Committer",
    "GIT_COMMITTER_EMAIL": "committer@example.invalid",
}

# A snapshot entry: ("dir",), ("file", sha256) or ("link", raw target).
SnapshotEntry = tuple[str, ...]


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


def run_sync(
    args: Sequence[str],
    *,
    env: dict[str, str],
    cwd: Path | None = None,
) -> Result:
    """The one place that knows how to start the tool under test."""
    command = [sys.executable, "-m", "agent_sync", *args]
    completed = subprocess.run(
        command,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    return Result(completed.returncode, completed.stdout, completed.stderr)


def snapshot(root: Path) -> dict[str, SnapshotEntry]:
    """Describe a directory tree without following symlinks (missing root -> empty)."""
    entries: dict[str, SnapshotEntry] = {}
    if not root.is_symlink() and not root.exists():
        return entries
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            entries[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            entries[rel] = ("dir",)
        else:
            entries[rel] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
    return entries


class Sandbox:
    """An isolated world for one test: project, cache, local 'GitHub', fake npx, empty HOME."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.cache = root / "cache"
        self.project = root / "project"
        self.repos = root / "repos"
        self.bin = root / "bin"
        self.cwd = root / "cwd"
        self.npx_log = root / "npx.log"
        for folder in (self.home, self.project, self.repos, self.bin, self.cwd):
            folder.mkdir(parents=True)
        self._install_fake_npx()
        self.env = self._build_env()

    # -- environment -------------------------------------------------------------------------

    def _install_fake_npx(self) -> None:
        npx = self.bin / "npx"
        npx.write_text(FAKE_NPX, encoding="utf-8")
        npx.chmod(npx.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _build_env(self) -> dict[str, str]:
        """Explicit environment: nothing from the developer's shell except PATH and locale."""
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.file://{self.repos}/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/",
            "FAKE_NPX_LOG": str(self.npx_log),
            **GIT_IDENTITY,
        }
        assert "SKILL_SYNC_CACHE" not in env
        return env

    # -- running the tool --------------------------------------------------------------------

    def sync(
        self,
        *args: str,
        cache: Path | None = None,
        config: Path | str | None = None,
        cwd: Path | None = None,
        cache_from_env: Path | None = None,
    ) -> Result:
        """Run the tool on the sandbox project with the sandbox cache (never the real ones).

        `cache_from_env` omits `--cache-dir` and sets `SKILL_SYNC_CACHE` for this one run instead.
        """
        command = ["--project", str(self.project)]
        env = dict(self.env)
        if cache_from_env is not None:
            env["SKILL_SYNC_CACHE"] = str(cache_from_env)
        else:
            command += ["--cache-dir", str(cache or self.cache)]
        if config is not None:
            command += ["--config", str(config)]
        return run_sync([*command, *args], env=env, cwd=cwd or self.cwd)

    # -- project files -----------------------------------------------------------------------

    def write_config(self, text: str) -> Path:
        path = self.project / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    @property
    def skills_dir(self) -> Path:
        return self.project / ".agents" / "skills"

    def skill_link(self, name: str) -> Path:
        return self.skills_dir / name

    def installed_skills(self) -> list[str]:
        if not self.skills_dir.is_dir():
            return []
        return sorted(child.name for child in self.skills_dir.iterdir())

    # -- local "GitHub" ----------------------------------------------------------------------

    def repo_path(self, slug: str) -> Path:
        return self.repos / f"{slug}.git"

    def make_repo(self, slug: str, skills: Sequence[str]) -> Path:
        """Create a local git repo for `owner/repo` with skills/<name>/SKILL.md for each skill."""
        repo = self.repo_path(slug)
        repo.mkdir(parents=True)
        self._git(repo, "init", "--quiet", "-b", "main")
        self.add_skills(slug, skills, message="initial skills")
        return repo

    def make_repo_with_files(self, slug: str, files: dict[str, str]) -> Path:
        """Create a local git repo for `owner/repo` with exactly the given files."""
        repo = self.repo_path(slug)
        repo.mkdir(parents=True)
        self._git(repo, "init", "--quiet", "-b", "main")
        for rel, content in files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self._git(repo, "add", "--all")
        self._git(repo, "commit", "--quiet", "-m", "initial files")
        return repo

    def add_skills(self, slug: str, skills: Sequence[str], message: str = "add skills") -> None:
        repo = self.repo_path(slug)
        for name in skills:
            skill_dir = repo / "skills" / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(f"# {name}\nfrom {slug}\n", encoding="utf-8")
        self._git(repo, "add", "--all")
        self._git(repo, "commit", "--quiet", "-m", message)

    def rewrite_history_with_skills(self, slug: str, skills: Sequence[str]) -> None:
        """Amend the upstream's only commit, like a force push: a clone cannot fast-forward."""
        repo = self.repo_path(slug)
        for name in skills:
            skill_dir = repo / "skills" / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(f"# {name}\nfrom {slug}\n", encoding="utf-8")
        self._git(repo, "add", "--all")
        self._git(repo, "commit", "--quiet", "--amend", "-m", "rewritten history")

    def cached_github_repo(self, slug: str, cache: Path | None = None) -> Path:
        """Where the tool keeps its clone of `owner/repo`."""
        return (cache or self.cache) / "github.com" / slug.replace("/", "%2F")

    def cached_npx_package(self, package: str, cache: Path | None = None) -> Path:
        return (cache or self.cache) / "npx" / package.replace("/", "%2F")

    def npx_calls(self) -> list[str]:
        """The `args=` lines recorded by the fake npx, one per invocation."""
        if not self.npx_log.exists():
            return []
        lines = self.npx_log.read_text(encoding="utf-8").splitlines()
        return [line[len("args="):] for line in lines if line.startswith("args=")]

    def npx_working_dirs(self) -> list[Path]:
        lines = self.npx_log.read_text(encoding="utf-8").splitlines()
        return [Path(line[len("cwd="):]) for line in lines if line.startswith("cwd=")]

    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )


def resolved(path: str | Path) -> Path:
    return Path(path).resolve()


@pytest.fixture
def sandbox(tmp_path: Path) -> Iterator[Sandbox]:
    box = Sandbox(tmp_path)
    yield box
    leftovers = sorted(p.name for p in box.home.iterdir())
    assert leftovers == [], f"the tool wrote into HOME: {leftovers}"
