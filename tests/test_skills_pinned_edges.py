"""More characterization tests: npx per-skill runs, --update without --apply, a foreign
`.claude/skills` link, discovery rules, `--config`, `$SKILL_SYNC_CACHE` and self-healing clones."""

from __future__ import annotations

import os

from conftest import Sandbox, resolved, run_sync, snapshot

ALL_FROM_REPO = "skills:\n  common:\n    - github.com:owner/repo:all\n"


def skill_md(name: str) -> str:
    return f"# {name}\n"


# -- npx: one run per (package, skill) ------------------------------------------------------


def test_two_npx_skills_from_one_package_are_installed_by_one_npx_run_each(
    sandbox: Sandbox,
) -> None:
    sandbox.write_config("skills:\n  common:\n    - npx/pkg:first\n    - npx/pkg:second\n")

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sorted(sandbox.npx_calls()) == [
        "--yes pkg install --skills first --target claude-project",
        "--yes pkg install --skills second --target claude-project",
    ]
    assert sandbox.installed_skills() == ["first", "second"]
    package_cache = sandbox.cached_npx_package("pkg") / "skills"
    assert resolved(sandbox.skill_link("first")) == resolved(package_cache / "first")
    assert resolved(sandbox.skill_link("second")) == resolved(package_cache / "second")


# -- --update without --apply ---------------------------------------------------------------


def test_update_without_apply_does_not_pull_and_changes_nothing(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL_FROM_REPO)
    assert sandbox.sync("--apply").returncode == 0
    sandbox.add_skills("owner/repo", ["newcomer"])
    project_before = snapshot(sandbox.project)
    cache_before = snapshot(sandbox.cache)

    result = sandbox.sync("--update")

    assert result.returncode == 0, result.stderr
    assert "newcomer" not in result.stdout
    assert not (sandbox.cached_github_repo("owner/repo") / "skills" / "newcomer").exists()
    assert snapshot(sandbox.cache) == cache_before  # .git included, so HEAD did not move
    assert snapshot(sandbox.project) == project_before


# -- a foreign .claude/skills link ----------------------------------------------------------


def test_a_claude_skills_symlink_to_another_folder_is_an_error_and_stays(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL_FROM_REPO)
    elsewhere = sandbox.root / "elsewhere"
    elsewhere.mkdir()
    claude_skills = sandbox.project / ".claude" / "skills"
    claude_skills.parent.mkdir()
    claude_skills.symlink_to(elsewhere, target_is_directory=True)

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Target already exists" in result.stderr
    assert str(claude_skills) in result.stderr
    assert os.readlink(claude_skills) == str(elsewhere)


def test_a_dangling_claude_skills_symlink_is_an_error_and_stays(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL_FROM_REPO)
    claude_skills = sandbox.project / ".claude" / "skills"
    claude_skills.parent.mkdir()
    claude_skills.symlink_to(sandbox.root / "does-not-exist", target_is_directory=True)

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Target already exists" in result.stderr
    assert os.readlink(claude_skills) == str(sandbox.root / "does-not-exist")


def test_a_claude_skills_symlink_that_reaches_the_pool_by_another_path_is_left_alone(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL_FROM_REPO)
    assert sandbox.sync("--apply").returncode == 0
    claude_skills = sandbox.project / ".claude" / "skills"
    claude_skills.unlink()
    claude_skills.symlink_to(resolved(sandbox.skills_dir), target_is_directory=True)

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert os.readlink(claude_skills) == str(resolved(sandbox.skills_dir))


# -- discovery rules ------------------------------------------------------------------------


def test_a_repo_without_a_skills_folder_is_scanned_from_its_root(sandbox: Sandbox) -> None:
    sandbox.make_repo_with_files(
        "owner/repo",
        {
            "alpha/SKILL.md": skill_md("alpha"),
            "group/nested/beta/SKILL.md": skill_md("beta"),
        },
    )
    sandbox.write_config(ALL_FROM_REPO)

    result = sandbox.sync("--apply")

    cloned = sandbox.cached_github_repo("owner/repo")
    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha", "beta"]
    assert resolved(sandbox.skill_link("beta")) == resolved(cloned / "group" / "nested" / "beta")


def test_when_a_skills_folder_exists_only_it_is_scanned(sandbox: Sandbox) -> None:
    sandbox.make_repo_with_files(
        "owner/repo",
        {
            "skills/alpha/SKILL.md": skill_md("alpha"),
            "outside/beta/SKILL.md": skill_md("beta"),
        },
    )
    sandbox.write_config(ALL_FROM_REPO)

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha"]


def test_skills_under_hidden_folders_are_skipped_and_do_not_count_as_duplicates(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo_with_files(
        "owner/repo",
        {
            "x/SKILL.md": skill_md("x"),
            ".claude/skills/x/SKILL.md": skill_md("x hidden copy"),
            ".claude/skills/only-hidden/SKILL.md": skill_md("only-hidden"),
        },
    )
    sandbox.write_config(ALL_FROM_REPO)

    result = sandbox.sync("--apply")

    cloned = sandbox.cached_github_repo("owner/repo")
    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["x"]
    assert resolved(sandbox.skill_link("x")) == resolved(cloned / "x")


def test_skills_under_hidden_folders_inside_the_skills_folder_are_skipped(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo_with_files(
        "owner/repo",
        {
            "skills/alpha/SKILL.md": skill_md("alpha"),
            "skills/.drafts/draft/SKILL.md": skill_md("draft"),
        },
    )
    sandbox.write_config(ALL_FROM_REPO)

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha"]


def test_skills_under_node_modules_are_skipped_and_do_not_count_as_duplicates(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo_with_files(
        "owner/repo",
        {
            "skills/x/SKILL.md": skill_md("x"),
            "skills/node_modules/dep/x/SKILL.md": skill_md("x from a dependency"),
            "skills/node_modules/dep/vendored/SKILL.md": skill_md("vendored"),
        },
    )
    sandbox.write_config(ALL_FROM_REPO)

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["x"]


def test_a_skill_folder_with_an_invalid_name_is_an_error(sandbox: Sandbox) -> None:
    sandbox.make_repo_with_files(
        "owner/repo",
        {
            "skills/alpha/SKILL.md": skill_md("alpha"),
            "skills/bad name/SKILL.md": skill_md("bad"),
        },
    )
    sandbox.write_config(ALL_FROM_REPO)

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Invalid or duplicate skill: bad name" in result.stderr
    assert sandbox.installed_skills() == []


def test_two_folders_with_the_same_name_in_one_source_are_an_error(sandbox: Sandbox) -> None:
    sandbox.make_repo_with_files(
        "owner/repo",
        {
            "skills/first/same/SKILL.md": skill_md("same"),
            "skills/second/same/SKILL.md": skill_md("same"),
        },
    )
    sandbox.write_config(ALL_FROM_REPO)

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Invalid or duplicate skill: same" in result.stderr
    assert sandbox.installed_skills() == []


# -- --config -------------------------------------------------------------------------------


def test_config_outside_the_project_is_used_and_the_project_needs_no_config(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    manifest = sandbox.root / "manifests" / "mine.yaml"
    manifest.parent.mkdir()
    manifest.write_text(ALL_FROM_REPO, encoding="utf-8")
    assert not (sandbox.project / "config.yaml").exists()

    result = sandbox.sync("--apply", config=manifest)

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha"]


def test_a_relative_config_path_is_resolved_against_the_current_directory(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    (sandbox.cwd / "mine.yaml").write_text(ALL_FROM_REPO, encoding="utf-8")

    result = sandbox.sync("--apply", config="mine.yaml", cwd=sandbox.cwd)

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha"]


def test_a_relative_config_path_is_not_resolved_against_the_project(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    (sandbox.project / "mine.yaml").write_text(ALL_FROM_REPO, encoding="utf-8")

    result = sandbox.sync("--apply", config="mine.yaml", cwd=sandbox.cwd)

    assert result.returncode == 1
    assert "Config not found" in result.stderr
    assert sandbox.installed_skills() == []


# -- $SKILL_SYNC_CACHE ----------------------------------------------------------------------


def test_skill_sync_cache_variable_is_used_when_no_cache_dir_flag_is_given(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL_FROM_REPO)
    env_cache = sandbox.root / "cache-from-env"

    result = sandbox.sync("--apply", cache_from_env=env_cache)

    assert result.returncode == 0, result.stderr
    assert (sandbox.cached_github_repo("owner/repo", cache=env_cache) / "skills" / "alpha").is_dir()
    assert not sandbox.cache.exists()
    expected = sandbox.cached_github_repo("owner/repo", cache=env_cache) / "skills" / "alpha"
    assert resolved(sandbox.skill_link("alpha")) == resolved(expected)


def test_the_cache_dir_flag_wins_over_skill_sync_cache(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL_FROM_REPO)
    flag_cache = sandbox.root / "cache-from-flag"
    env_cache = sandbox.root / "cache-from-env"
    command = ["--project", str(sandbox.project), "--cache-dir", str(flag_cache), "--apply"]

    result = run_sync(
        command, env={**sandbox.env, "SKILL_SYNC_CACHE": str(env_cache)}, cwd=sandbox.cwd
    )

    assert result.returncode == 0, result.stderr
    assert flag_cache.is_dir()
    assert not env_cache.exists()


# -- clone self-healing ---------------------------------------------------------------------


def test_apply_restores_a_named_skill_deleted_from_the_cached_clone(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config("skills:\n  common:\n    - github.com:owner/repo:alpha\n")
    assert sandbox.sync("--apply").returncode == 0
    cloned = sandbox.cached_github_repo("owner/repo")
    skill_file = cloned / "skills" / "alpha" / "SKILL.md"
    skill_file.unlink()
    (cloned / "skills" / "alpha").rmdir()
    marker = cloned / "local-marker.txt"
    marker.write_text("a fresh clone drops this file", encoding="utf-8")

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert skill_file.is_file()  # restored although `git pull` itself succeeded
    assert not marker.exists()  # proves the repo was cloned again
    assert resolved(sandbox.skill_link("alpha")) == resolved(cloned / "skills" / "alpha")


def test_a_named_skill_missing_upstream_stays_an_error_after_the_clone_is_refreshed(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config("skills:\n  common:\n    - github.com:owner/repo:alpha\n")
    assert sandbox.sync("--apply").returncode == 0
    sandbox.write_config("skills:\n  common:\n    - github.com:owner/repo:ghost\n")

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Skill 'ghost' not in" in result.stderr
    assert "Available skills: [alpha]" in result.stderr
