"""Tests for the git exclude block, run against real repositories created with `git init`."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_sync.gitexclude import ExcludeResult, sync_exclude

START = "# >>> agent-sync (managed) project={} >>>\n"
END = "# <<< agent-sync (managed) project={} <<<\n"


def block(project: Path, *lines: str) -> str:
    """The block for a project: its marker carries the absolute, resolved project path."""
    marker = str(project.resolve())
    body = "".join(f"{line}\n" for line in lines)
    return START.format(marker) + body + END.format(marker)


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "author@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test Committer")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "committer@example.invalid")


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return done.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "--quiet", "-b", "main")
    exclude_file(path).write_text("", encoding="utf-8")  # drop the template's comment lines
    return path


def exclude_file(repo: Path) -> Path:
    return repo / ".git" / "info" / "exclude"


def test_block_is_created_with_anchored_sorted_deduplicated_lines(repo: Path) -> None:
    result = sync_exclude(
        repo, [".agents/skills/b", ".agents/skills/a", ".agents/skills/b"], apply=True
    )

    assert result.changed is True
    assert result.note is None
    assert result.path is not None and result.path.resolve() == exclude_file(repo).resolve()
    text = exclude_file(repo).read_text(encoding="utf-8")
    assert block(repo, "/.agents/skills/a", "/.agents/skills/b") in text
    assert text.count("agent-sync (managed)") == 2


def test_created_when_info_directory_is_missing(repo: Path) -> None:
    info = repo / ".git" / "info"
    if info.exists():
        for child in info.iterdir():
            child.unlink()
        info.rmdir()

    sync_exclude(repo, [".agents/x"], apply=True)

    assert exclude_file(repo).read_text(encoding="utf-8") == block(repo, "/.agents/x")


def test_block_is_updated_in_place(repo: Path) -> None:
    sync_exclude(repo, [".agents/a", ".agents/b"], apply=True)
    before = exclude_file(repo).read_text(encoding="utf-8")
    exclude_file(repo).write_text(before + "tail-line\n", encoding="utf-8")

    result = sync_exclude(repo, [".agents/b", ".agents/c"], apply=True)

    assert result.changed is True
    assert exclude_file(repo).read_text(encoding="utf-8") == (
        block(repo, "/.agents/b", "/.agents/c") + "tail-line\n"
    )


def test_block_is_removed_when_the_set_is_empty(repo: Path) -> None:
    exclude_file(repo).parent.mkdir(exist_ok=True)
    exclude_file(repo).write_text("# mine\n*.log\n", encoding="utf-8")
    sync_exclude(repo, [".agents/a"], apply=True)

    result = sync_exclude(repo, [], apply=True)

    assert result.changed is True
    assert exclude_file(repo).read_text(encoding="utf-8") == "# mine\n*.log\n"


def test_empty_set_without_a_block_changes_nothing(repo: Path) -> None:
    before = exclude_file(repo).read_bytes() if exclude_file(repo).exists() else None

    result = sync_exclude(repo, [], apply=True)

    assert result.changed is False
    after = exclude_file(repo).read_bytes() if exclude_file(repo).exists() else None
    assert after == before


def test_user_lines_and_another_projects_block_are_untouched(repo: Path) -> None:
    (repo / "other" / "proj").mkdir(parents=True)
    other = block(repo / "other" / "proj", "/other/proj/.agents/skills/z")
    original = f"# user\n*.log\n\n{other}\n!keep.log\n"
    exclude_file(repo).parent.mkdir(exist_ok=True)
    exclude_file(repo).write_text(original, encoding="utf-8")

    sync_exclude(repo, [".agents/a"], apply=True)
    created = exclude_file(repo).read_text(encoding="utf-8")
    assert created == original + block(repo, "/.agents/a")

    sync_exclude(repo, [".agents/a", ".agents/b"], apply=True)
    updated = exclude_file(repo).read_text(encoding="utf-8")
    assert updated == original + block(repo, "/.agents/a", "/.agents/b")

    sync_exclude(repo, [], apply=True)
    assert exclude_file(repo).read_text(encoding="utf-8") == original


def test_block_is_appended_after_a_separating_newline(repo: Path) -> None:
    exclude_file(repo).parent.mkdir(exist_ok=True)
    exclude_file(repo).write_bytes(b"no-newline-at-eof")

    sync_exclude(repo, [".agents/a"], apply=True)

    assert exclude_file(repo).read_text(encoding="utf-8") == (
        "no-newline-at-eof\n" + block(repo, "/.agents/a")
    )


def test_second_call_reports_no_change_and_leaves_the_file_alone(repo: Path) -> None:
    sync_exclude(repo, [".agents/a", ".agents/b"], apply=True)
    stat_before = exclude_file(repo).stat()
    content = exclude_file(repo).read_bytes()

    result = sync_exclude(repo, [".agents/b", ".agents/a"], apply=True)

    assert result.changed is False
    assert exclude_file(repo).read_bytes() == content
    assert exclude_file(repo).stat().st_mtime_ns == stat_before.st_mtime_ns


def test_dry_run_reports_the_change_but_writes_nothing(repo: Path) -> None:
    exclude_file(repo).parent.mkdir(exist_ok=True)
    exclude_file(repo).write_text("*.log\n", encoding="utf-8")

    result = sync_exclude(repo, [".agents/a"], apply=False)

    assert result.changed is True
    assert exclude_file(repo).read_text(encoding="utf-8") == "*.log\n"


def test_dry_run_creates_no_file_or_directory(repo: Path) -> None:
    info = repo / ".git" / "info"
    if info.exists():
        for child in info.iterdir():
            child.unlink()
        info.rmdir()

    result = sync_exclude(repo, [".agents/a"], apply=False)

    assert result.changed is True
    assert not info.exists()


def test_write_leaves_no_temp_files_behind(repo: Path) -> None:
    sync_exclude(repo, [".agents/a"], apply=True)
    sync_exclude(repo, [".agents/b"], apply=True)

    assert [p.name for p in exclude_file(repo).parent.iterdir()] == ["exclude"]


def test_project_in_a_subfolder_gets_prefixed_lines_and_its_own_marker(repo: Path) -> None:
    sub = repo / "apps" / "web"
    sub.mkdir(parents=True)

    sync_exclude(sub, [".agents/skills/a"], apply=True)

    assert exclude_file(repo).read_text(encoding="utf-8") == block(
        sub, "/apps/web/.agents/skills/a"
    )
    (sub / ".agents" / "skills").mkdir(parents=True)
    (sub / ".agents" / "skills" / "a").write_text("x", encoding="utf-8")
    assert git(repo, "check-ignore", "apps/web/.agents/skills/a").strip() == (
        "apps/web/.agents/skills/a"
    )


def test_two_projects_in_one_repository_keep_separate_blocks(repo: Path) -> None:
    one = repo / "one"
    two = repo / "two"
    one.mkdir()
    two.mkdir()

    sync_exclude(repo, [".agents/r"], apply=True)
    sync_exclude(one, [".agents/a"], apply=True)
    sync_exclude(two, [".agents/b"], apply=True)
    sync_exclude(one, [".agents/a2"], apply=True)

    text = exclude_file(repo).read_text(encoding="utf-8")
    assert block(repo, "/.agents/r") in text
    assert block(one, "/one/.agents/a2") in text
    assert block(two, "/two/.agents/b") in text
    assert "/one/.agents/a\n" not in text


def test_linked_worktree_writes_to_the_common_exclude_file(repo: Path, tmp_path: Path) -> None:
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "--quiet", "-m", "initial")
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "--quiet", "-b", "feature", str(worktree))

    result = sync_exclude(worktree, [".agents/skills/a"], apply=True)

    assert result.changed is True
    assert result.path is not None and result.path.resolve() == exclude_file(repo).resolve()
    assert exclude_file(repo).read_text(encoding="utf-8") == block(worktree, "/.agents/skills/a")
    (worktree / ".agents" / "skills").mkdir(parents=True)
    (worktree / ".agents" / "skills" / "a").write_text("x", encoding="utf-8")
    assert git(worktree, "check-ignore", ".agents/skills/a").strip() == ".agents/skills/a"
    assert sync_exclude(worktree, [".agents/skills/a"], apply=True).changed is False


def test_main_checkout_and_linked_worktree_keep_their_own_blocks(
    repo: Path, tmp_path: Path
) -> None:
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "--quiet", "-m", "initial")
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "--quiet", "-b", "feature", str(worktree))

    sync_exclude(repo, [".agents/skills/a", ".agents/skills/b"], apply=True)
    sync_exclude(worktree, [".agents/skills/a", ".agents/skills/c"], apply=True)
    main_block = block(repo, "/.agents/skills/a", "/.agents/skills/b")
    worktree_block = block(worktree, "/.agents/skills/a", "/.agents/skills/c")
    assert exclude_file(repo).read_text(encoding="utf-8") == main_block + worktree_block

    # Each run is idempotent and rewrites only its own block.
    assert sync_exclude(repo, [".agents/skills/a", ".agents/skills/b"], apply=True).changed is False
    assert sync_exclude(worktree, [".agents/skills/a", ".agents/skills/c"], apply=True).changed is (
        False
    )
    sync_exclude(worktree, [".agents/skills/c"], apply=True)
    assert exclude_file(repo).read_text(encoding="utf-8") == main_block + block(
        worktree, "/.agents/skills/c"
    )
    sync_exclude(repo, [], apply=True)
    assert exclude_file(repo).read_text(encoding="utf-8") == block(worktree, "/.agents/skills/c")


def test_moved_project_gets_a_new_block_and_the_old_one_goes_with_its_folder(
    repo: Path,
) -> None:
    old = repo / "old-home"
    new = repo / "new-home"
    old.mkdir()
    new.mkdir()
    sync_exclude(old, [".agents/skills/a"], apply=True)
    old_block = block(old, "/old-home/.agents/skills/a")

    result = sync_exclude(new, [".agents/skills/a"], apply=True)

    assert result.changed is True
    new_block = block(new, "/new-home/.agents/skills/a")
    assert exclude_file(repo).read_text(encoding="utf-8") == old_block + new_block

    shutil.rmtree(old)
    dry = sync_exclude(new, [".agents/skills/a"], apply=False)
    assert dry.changed is True
    assert exclude_file(repo).read_text(encoding="utf-8") == old_block + new_block

    sync_exclude(new, [".agents/skills/a"], apply=True)
    assert exclude_file(repo).read_text(encoding="utf-8") == new_block
    assert sync_exclude(new, [".agents/skills/a"], apply=True).changed is False


def test_block_of_a_removed_worktree_is_cleaned_up_by_the_next_run(
    repo: Path, tmp_path: Path
) -> None:
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "--quiet", "-m", "initial")
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "--quiet", "-b", "feature", str(worktree))
    sync_exclude(worktree, [".agents/skills/c"], apply=True)
    (repo / ".agents" / "skills").mkdir(parents=True)
    (repo / ".agents" / "skills" / "c").write_text("x", encoding="utf-8")
    assert git(repo, "check-ignore", ".agents/skills/c").strip() == ".agents/skills/c"
    git(repo, "worktree", "remove", "--force", str(worktree))

    result = sync_exclude(repo, [".agents/skills/a"], apply=True)

    assert result.changed is True
    assert exclude_file(repo).read_text(encoding="utf-8") == block(repo, "/.agents/skills/a")
    done = subprocess.run(
        ["git", "check-ignore", ".agents/skills/c"], cwd=str(repo), capture_output=True, check=False
    )
    assert done.returncode == 1


def test_cleanup_only_drops_well_formed_blocks_of_missing_folders(
    repo: Path, tmp_path: Path
) -> None:
    gone = tmp_path / "gone"
    alive = tmp_path / "alive"
    alive.mkdir()
    keep = (
        "# user\n"
        + block(alive, "/alive/.agents/x")
        + "# >>> agent-sync (managed) project=relative/path >>>\n/y\n"
        + "# <<< agent-sync (managed) project=relative/path <<<\n"
        + f"# >>> agent-sync (managed) project={gone}/open >>>\n/unterminated\n"
    )
    exclude_file(repo).write_text(keep + block(gone, "/gone/.agents/x"), encoding="utf-8")

    sync_exclude(repo, [".agents/a"], apply=True)

    assert exclude_file(repo).read_text(encoding="utf-8") == keep + block(repo, "/.agents/a")


def test_outside_a_git_repository_returns_a_note(tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()

    result = sync_exclude(folder, [".agents/a"], apply=True)

    assert result == ExcludeResult(None, False, result.note)
    assert result.note is not None
    assert result.note.startswith(f"not a git repository: {folder}")
    assert "skipped .git/info/exclude" in result.note
    assert list(folder.iterdir()) == []


def test_missing_git_executable_returns_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    result = sync_exclude(folder, [".agents/a"], apply=True)

    assert result.path is None and result.changed is False
    assert result.note is not None and "git" in result.note


def test_project_path_with_a_newline_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "bad\nname"
    project.mkdir()
    git(project, "init", "--quiet")

    with pytest.raises(ValueError, match="newline"):
        sync_exclude(project, [".agents/a"], apply=True)


@pytest.mark.parametrize(
    "bad",
    ["a\nb", "a\rb", "/abs/path", "../escape", "a/../../b", "a/..", "", ".."],
)
def test_invalid_paths_are_rejected(repo: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        sync_exclude(repo, [".agents/ok", bad], apply=True)

    assert not exclude_file(repo).exists() or "agent-sync" not in exclude_file(repo).read_text(
        encoding="utf-8"
    )


def test_invalid_paths_are_rejected_outside_git_too(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        sync_exclude(tmp_path, ["../x"], apply=False)


def test_glob_characters_in_paths_match_literally(repo: Path) -> None:
    sync_exclude(repo, [".agents/skills/a*b"], apply=True)

    (repo / ".agents" / "skills").mkdir(parents=True)
    (repo / ".agents" / "skills" / "a*b").write_text("x", encoding="utf-8")
    (repo / ".agents" / "skills" / "axxb").write_text("x", encoding="utf-8")
    assert git(repo, "check-ignore", ".agents/skills/a*b").strip() == ".agents/skills/a*b"
    done = subprocess.run(
        ["git", "check-ignore", ".agents/skills/axxb"],
        cwd=str(repo),
        capture_output=True,
        check=False,
    )
    assert done.returncode == 1


def test_unterminated_block_is_reported_not_rewritten(repo: Path) -> None:
    exclude_file(repo).parent.mkdir(exist_ok=True)
    broken = START.format(str(repo.resolve())) + "/.agents/a\n"
    exclude_file(repo).write_text(broken, encoding="utf-8")

    with pytest.raises(ValueError, match="no closing marker"):
        sync_exclude(repo, [".agents/a"], apply=True)

    assert exclude_file(repo).read_text(encoding="utf-8") == broken
