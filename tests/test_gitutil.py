"""is_tracked: whether git tracks a project path."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_sync.gitexclude import is_tracked


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


def test_tracked_untracked_and_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    (repo / "a.json").write_text("{}")
    (repo / "b.json").write_text("{}")
    git(repo, "add", "a.json")
    assert is_tracked(repo, "a.json") is True
    assert is_tracked(repo, "b.json") is False
    assert is_tracked(repo, "nope.json") is False


def test_outside_git_is_false(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text("{}")
    assert is_tracked(tmp_path, "a.json") is False


def test_git_missing_is_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert is_tracked(tmp_path, "a.json") is False


def _fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, message: str) -> None:
    bin_dir = tmp_path / f"fakebin{code}"
    bin_dir.mkdir()
    script = bin_dir / "git"
    script.write_text(f"#!/bin/sh\necho '{message}' >&2\nexit {code}\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))


def test_git_fatal_error_is_raised_not_treated_as_untracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(tmp_path, monkeypatch, 128, "fatal: detected dubious ownership in repository")
    with pytest.raises(ValueError, match="dubious ownership"):
        is_tracked(tmp_path, "a.json")


def test_git_exit_codes_one_and_not_a_repository_are_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(tmp_path, monkeypatch, 1, "error: pathspec did not match")
    assert is_tracked(tmp_path, "a.json") is False
    _fake_git(tmp_path, monkeypatch, 128, "fatal: not a git repository (or any parent)")
    assert is_tracked(tmp_path, "a.json") is False


def test_localized_git_message_is_not_misread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "fakebin-de"
    bin_dir.mkdir()
    script = bin_dir / "git"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$LC_ALL" = C ]; then echo "fatal: not a git repository" >&2\n'
        'else echo "fatal: kein Git-Repository" >&2; fi\n'
        "exit 128\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    assert is_tracked(tmp_path, "a.json") is False
