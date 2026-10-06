"""Characterization tests: what `skill_sync.py` does today.

They pin links, errors and key output only; the exact summary wording, `personal:` sources and
the default cache location are deliberately not pinned because they change by design.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import Sandbox, resolved, snapshot

ALL_ALPHA_BETA = """\
skills:
  common:
    - github.com:owner/repo:all
"""


def single_skill_config(slug: str, skill: str) -> str:
    return f"skills:\n  common:\n    - github.com:{slug}:{skill}\n"


# -- selecting skills -----------------------------------------------------------------------


def test_all_links_every_skill_of_the_repo(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta", "gamma"])
    sandbox.write_config(ALL_ALPHA_BETA)

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha", "beta", "gamma"]
    skills_in_cache = sandbox.cached_github_repo("owner/repo") / "skills"
    for name in ("alpha", "beta", "gamma"):
        link = sandbox.skill_link(name)
        assert link.is_symlink()
        assert resolved(link) == resolved(skills_in_cache / name)


def test_named_skill_links_only_that_skill(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(single_skill_config("owner/repo", "beta"))

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["beta"]
    expected = sandbox.cached_github_repo("owner/repo") / "skills" / "beta"
    assert resolved(sandbox.skill_link("beta")) == resolved(expected)


def test_unknown_skill_fails_and_lists_the_available_ones(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(single_skill_config("owner/repo", "nope"))

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Error:" in result.stderr
    assert "nope" in result.stderr
    assert "alpha" in result.stderr and "beta" in result.stderr
    assert sandbox.installed_skills() == []


def test_common_codex_and_claude_sections_are_merged_into_one_pool(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta", "gamma"])
    sandbox.write_config(
        "skills:\n"
        "  common:\n"
        "    - github.com:owner/repo:alpha\n"
        "  codex:\n"
        "    - github.com:owner/repo:beta\n"
        "  claude:\n"
        "    - github.com:owner/repo:gamma\n"
    )

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha", "beta", "gamma"]


def test_the_same_skill_listed_twice_from_one_source_is_not_a_collision(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(
        "skills:\n"
        "  common:\n"
        "    - github.com:owner/repo:all\n"
        "  claude:\n"
        "    - github.com:owner/repo:alpha\n"
    )

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha", "beta"]


def test_same_skill_name_from_two_sources_is_a_collision_error(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/first", ["shared"])
    sandbox.make_repo("owner/second", ["shared"])
    sandbox.write_config(
        "skills:\n"
        "  common:\n"
        "    - github.com:owner/first:shared\n"
        "  codex:\n"
        "    - github.com:owner/second:shared\n"
    )

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Collision" in result.stderr
    assert "shared" in result.stderr
    assert sandbox.installed_skills() == []


@pytest.mark.parametrize(
    "selector",
    [
        "bogus:owner/repo:alpha",
        "github.com:owner/repo",
        "github.com:owner/repo:alpha:extra",
        "github.com:not a repo:alpha",
        "personal",
    ],
)
def test_invalid_string_selector_is_an_error(sandbox: Sandbox, selector: str) -> None:
    sandbox.write_config(f"skills:\n  common:\n    - {selector}\n")

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Invalid string selector" in result.stderr
    assert sandbox.installed_skills() == []


def test_a_manifest_entry_that_is_neither_a_string_nor_a_mapping_is_an_error(
    sandbox: Sandbox,
) -> None:
    sandbox.write_config("skills:\n  common:\n    - 42\n")

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Unsupported entry format" in result.stderr


def test_an_unsupported_source_in_a_mapping_entry_is_an_error(sandbox: Sandbox) -> None:
    sandbox.write_config("skills:\n  common:\n    - gitlab:\n        - owner/repo:alpha\n")

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Unsupported source" in result.stderr


def test_mapping_form_with_the_github_alias_links_the_listed_skills(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta", "gamma"])
    sandbox.write_config(
        "skills:\n"
        "  common:\n"
        "    - github:\n"
        "        - owner/repo:\n"
        "            - alpha\n"
        "            - gamma\n"
    )

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha", "gamma"]


def test_missing_config_is_an_error(sandbox: Sandbox) -> None:
    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Config not found" in result.stderr


# -- the .claude/skills link ----------------------------------------------------------------


def test_apply_links_claude_skills_to_the_shared_agents_pool(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL_ALPHA_BETA)

    result = sandbox.sync("--apply")

    claude_skills = sandbox.project / ".claude" / "skills"
    assert result.returncode == 0, result.stderr
    assert claude_skills.is_symlink()
    assert os.readlink(claude_skills) == os.path.join("..", ".agents", "skills")
    assert resolved(claude_skills) == resolved(sandbox.skills_dir)
    assert (claude_skills / "alpha" / "SKILL.md").is_file()


def test_a_real_claude_skills_folder_is_an_error_and_stays_untouched(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL_ALPHA_BETA)
    claude_skills = sandbox.project / ".claude" / "skills"
    (claude_skills / "mine").mkdir(parents=True)
    (claude_skills / "mine" / "SKILL.md").write_text("mine", encoding="utf-8")
    before = snapshot(sandbox.project / ".claude")

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Target already exists" in result.stderr
    assert not claude_skills.is_symlink()
    assert snapshot(sandbox.project / ".claude") == before


# -- dry run and idempotence ----------------------------------------------------------------


def test_dry_run_with_a_populated_cache_prints_the_plan_and_writes_nothing(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(ALL_ALPHA_BETA)
    assert sandbox.sync("--apply").returncode == 0
    for link in sandbox.skills_dir.iterdir():
        link.unlink()
    (sandbox.project / ".claude" / "skills").unlink()
    project_before = snapshot(sandbox.project)
    cache_before = snapshot(sandbox.cache)

    result = sandbox.sync()

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("Would link") >= 3  # alpha, beta and .claude/skills
    assert "alpha" in result.stdout and "beta" in result.stdout
    assert ".claude/skills" in result.stdout
    assert snapshot(sandbox.project) == project_before
    assert snapshot(sandbox.cache) == cache_before


def test_dry_run_on_a_fresh_cache_reports_the_missing_source_and_writes_nothing(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    config = sandbox.write_config(ALL_ALPHA_BETA)
    project_before = snapshot(sandbox.project)

    result = sandbox.sync()

    assert result.returncode == 0, result.stderr
    assert "Missing source (run with --apply)" in result.stdout
    assert "owner/repo" in result.stdout
    assert snapshot(sandbox.project) == project_before
    assert config.is_file()
    assert not sandbox.cache.exists()


def test_apply_clones_from_the_local_repo_not_from_github(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL_ALPHA_BETA)

    result = sandbox.sync("--apply")

    cloned = sandbox.cached_github_repo("owner/repo")
    assert result.returncode == 0, result.stderr
    assert (cloned / "skills" / "alpha" / "SKILL.md").read_text(encoding="utf-8") == (
        "# alpha\nfrom owner/repo\n"
    )
    assert (cloned / ".git").is_dir()


def test_a_repo_that_does_not_exist_locally_fails_instead_of_reaching_github(
    sandbox: Sandbox,
) -> None:
    sandbox.write_config(single_skill_config("ghost/missing", "alpha"))

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert str(sandbox.repos) in result.stderr  # git tried the redirected local path
    assert sandbox.installed_skills() == []


def test_a_second_apply_changes_nothing(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(ALL_ALPHA_BETA)
    assert sandbox.sync("--apply").returncode == 0
    project_before = snapshot(sandbox.project)
    cache_before = snapshot(sandbox.cache)

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert snapshot(sandbox.project) == project_before
    assert snapshot(sandbox.cache) == cache_before


# -- conflicts at the target ----------------------------------------------------------------


def test_a_real_folder_at_a_skill_target_is_an_error_and_stays_untouched(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(single_skill_config("owner/repo", "alpha"))
    target = sandbox.skill_link("alpha")
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("my own alpha", encoding="utf-8")
    before = snapshot(sandbox.skills_dir)

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Target already exists" in result.stderr
    assert not target.is_symlink()
    assert snapshot(sandbox.skills_dir) == before


def test_an_unmanaged_symlink_at_a_skill_target_is_an_error_and_stays_untouched(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(single_skill_config("owner/repo", "alpha"))
    elsewhere = sandbox.root / "elsewhere" / "alpha"
    elsewhere.mkdir(parents=True)
    (elsewhere / "SKILL.md").write_text("not managed", encoding="utf-8")
    target = sandbox.skill_link("alpha")
    target.parent.mkdir(parents=True)
    target.symlink_to(elsewhere, target_is_directory=True)

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Target already exists" in result.stderr
    assert os.readlink(target) == str(elsewhere)


def test_a_stale_managed_link_into_the_cache_is_relinked(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(single_skill_config("owner/repo", "alpha"))
    stale_source = sandbox.cache / "old-layout" / "alpha"
    stale_source.mkdir(parents=True)
    target = sandbox.skill_link("alpha")
    target.parent.mkdir(parents=True)
    target.symlink_to(stale_source, target_is_directory=True)

    result = sandbox.sync("--apply")

    expected = sandbox.cached_github_repo("owner/repo") / "skills" / "alpha"
    assert result.returncode == 0, result.stderr
    assert resolved(target) == resolved(expected)


def test_a_dangling_link_left_by_a_removed_cache_is_relinked_to_a_new_cache(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(single_skill_config("owner/repo", "alpha"))
    old_cache = sandbox.root / "old-cache"
    new_cache = sandbox.root / "new-cache"
    assert sandbox.sync("--apply", cache=old_cache).returncode == 0
    for path in sorted(old_cache.rglob("*"), reverse=True):  # remove the old cache entirely
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            path.rmdir()
    old_cache.rmdir()
    assert not sandbox.skill_link("alpha").exists()  # now dangling

    result = sandbox.sync("--apply", cache=new_cache)

    expected = sandbox.cached_github_repo("owner/repo", cache=new_cache) / "skills" / "alpha"
    assert result.returncode == 0, result.stderr
    assert resolved(sandbox.skill_link("alpha")) == resolved(expected)


# -- prune ----------------------------------------------------------------------------------


def prepare_two_skills_then_keep_only_alpha(sandbox: Sandbox) -> Path:
    """Install alpha and beta, then shrink the manifest to alpha; return an unmanaged link."""
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(ALL_ALPHA_BETA)
    assert sandbox.sync("--apply").returncode == 0
    sandbox.write_config(single_skill_config("owner/repo", "alpha"))
    return sandbox.skill_link("alpha")


def test_prune_removes_managed_links_that_left_the_manifest(sandbox: Sandbox) -> None:
    kept = prepare_two_skills_then_keep_only_alpha(sandbox)

    result = sandbox.sync("--apply", "--prune")

    assert result.returncode == 0, result.stderr
    assert "Remove" in result.stdout
    assert sandbox.installed_skills() == ["alpha"]
    assert kept.is_symlink()


def test_prune_keeps_real_folders_and_unmanaged_symlinks(sandbox: Sandbox) -> None:
    prepare_two_skills_then_keep_only_alpha(sandbox)
    real_folder = sandbox.skill_link("hand-made")
    real_folder.mkdir()
    (real_folder / "SKILL.md").write_text("hand made", encoding="utf-8")
    outside = sandbox.root / "outside" / "external"
    outside.mkdir(parents=True)
    unmanaged = sandbox.skill_link("external")
    unmanaged.symlink_to(outside, target_is_directory=True)

    result = sandbox.sync("--apply", "--prune")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha", "external", "hand-made"]
    assert os.readlink(unmanaged) == str(outside)
    assert (real_folder / "SKILL.md").read_text(encoding="utf-8") == "hand made"


def test_prune_without_apply_only_reports(sandbox: Sandbox) -> None:
    prepare_two_skills_then_keep_only_alpha(sandbox)
    before = snapshot(sandbox.project)

    result = sandbox.sync("--prune")

    assert result.returncode == 0, result.stderr
    assert "Would remove" in result.stdout
    assert snapshot(sandbox.project) == before


def test_without_prune_links_that_left_the_manifest_stay(sandbox: Sandbox) -> None:
    prepare_two_skills_then_keep_only_alpha(sandbox)

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha", "beta"]


# -- --update -------------------------------------------------------------------------------


def test_update_picks_up_a_skill_added_upstream_after_the_first_clone(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL_ALPHA_BETA)
    assert sandbox.sync("--apply").returncode == 0
    sandbox.add_skills("owner/repo", ["newcomer"])

    without_update = sandbox.sync("--apply")
    assert without_update.returncode == 0, without_update.stderr
    assert sandbox.installed_skills() == ["alpha"]

    with_update = sandbox.sync("--apply", "--update")

    assert with_update.returncode == 0, with_update.stderr
    assert sandbox.installed_skills() == ["alpha", "newcomer"]
    expected = sandbox.cached_github_repo("owner/repo") / "skills" / "newcomer"
    assert resolved(sandbox.skill_link("newcomer")) == resolved(expected)


def test_a_named_skill_added_upstream_is_found_on_apply_without_update(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(single_skill_config("owner/repo", "alpha"))
    assert sandbox.sync("--apply").returncode == 0
    sandbox.add_skills("owner/repo", ["newcomer"])
    sandbox.write_config(single_skill_config("owner/repo", "newcomer"))

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha", "newcomer"]


def test_update_pulls_into_the_existing_clone_instead_of_cloning_again(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL_ALPHA_BETA)
    assert sandbox.sync("--apply").returncode == 0
    marker = sandbox.cached_github_repo("owner/repo") / "local-marker.txt"
    marker.write_text("only a pull keeps this file", encoding="utf-8")
    sandbox.add_skills("owner/repo", ["newcomer"])

    result = sandbox.sync("--apply", "--update")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha", "newcomer"]
    assert marker.is_file()


def test_update_clones_again_when_the_upstream_history_was_rewritten(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL_ALPHA_BETA)
    assert sandbox.sync("--apply").returncode == 0
    marker = sandbox.cached_github_repo("owner/repo") / "local-marker.txt"
    marker.write_text("a fresh clone drops this file", encoding="utf-8")
    sandbox.rewrite_history_with_skills("owner/repo", ["newcomer"])

    result = sandbox.sync("--apply", "--update")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha", "newcomer"]
    assert not marker.exists()


# -- npx ------------------------------------------------------------------------------------


def test_npx_installs_exactly_the_requested_skill(sandbox: Sandbox) -> None:
    sandbox.write_config("skills:\n  common:\n    - npx/some-package:ecc-guide\n")

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sandbox.npx_calls() == [
        "--yes some-package install --skills ecc-guide --target claude-project"
    ]
    assert sandbox.installed_skills() == ["ecc-guide"]
    expected = sandbox.cached_npx_package("some-package") / "skills" / "ecc-guide"
    assert resolved(sandbox.skill_link("ecc-guide")) == resolved(expected)
    assert (expected / "SKILL.md").is_file()


def test_npx_runs_in_a_staging_folder_not_in_the_project(sandbox: Sandbox) -> None:
    sandbox.write_config("skills:\n  common:\n    - npx/some-package:ecc-guide\n")

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    (staging,) = sandbox.npx_working_dirs()
    assert resolved(staging) != resolved(sandbox.project)
    assert resolved(sandbox.project) not in resolved(staging).parents
    assert (sandbox.project / ".claude" / "skills").is_symlink()  # only the tool's own link


def test_npx_is_not_run_again_once_the_skill_is_cached(sandbox: Sandbox) -> None:
    sandbox.write_config("skills:\n  common:\n    - npx/some-package:ecc-guide\n")
    assert sandbox.sync("--apply").returncode == 0

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert len(sandbox.npx_calls()) == 1


def test_npx_is_not_run_without_apply(sandbox: Sandbox) -> None:
    sandbox.write_config("skills:\n  common:\n    - npx/some-package:ecc-guide\n")

    result = sandbox.sync()

    assert result.returncode == 0, result.stderr
    assert "Missing source (run with --apply)" in result.stdout
    assert sandbox.npx_calls() == []


def test_npx_with_all_is_an_error(sandbox: Sandbox) -> None:
    sandbox.write_config("skills:\n  common:\n    - npx/some-package:all\n")

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "explicit skill name" in result.stderr
    assert sandbox.npx_calls() == []
