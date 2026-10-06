"""CLI options and locations: cache and personal precedence, --force, messages, exit codes, --help.

Every test drives the tool through `run_sync` as a subprocess with an explicit environment.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from conftest import Result, Sandbox, resolved, run_sync

from agent_sync.kinds.skills import unknown_skill_message

ALPHA = "skills:\n  common:\n    - github.com:owner/repo:alpha\n"
ALL = "skills:\n  common:\n    - github.com:owner/repo:all\n"
PERSONAL = "skills:\n  common:\n    - personal:mine\n"


def run(sandbox: Sandbox, *args: str, env: dict[str, str] | None = None) -> Result:
    """Run on the sandbox project; no cache flag or variable unless the test passes one."""
    command = ["--project", str(sandbox.project), *args]
    return run_sync(command, env={**sandbox.env, **(env or {})}, cwd=sandbox.cwd)


def make_personal(root: Path, layout: str = "flat") -> Path:
    """A personal folder with the skill `mine`: like the old custom-skills/, or with skills/."""
    base = root / ("skills" if layout == "nested" else ".") / "mine"
    base.mkdir(parents=True)
    (base / "SKILL.md").write_text("# mine\n", encoding="utf-8")
    return root


# -- cache location: every step of the precedence ---------------------------------------------

CACHE_STEPS = ["flag", "agent-sync-cache", "skill-sync-cache", "xdg", "home"]


@pytest.mark.parametrize("step", range(len(CACHE_STEPS)), ids=CACHE_STEPS)
def test_cache_precedence_each_step_wins_over_the_ones_below_it(
    sandbox: Sandbox, step: int
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALPHA)
    home = sandbox.root / "alt-home"
    home.mkdir()
    places = {
        "flag": sandbox.root / "c-flag",
        "agent-sync-cache": sandbox.root / "c-agent",
        "skill-sync-cache": sandbox.root / "c-skill",
        "xdg": sandbox.root / "c-xdg" / "agent-sync",
        "home": home / ".cache" / "agent-sync",
    }
    args = ["--apply"]
    env = {"HOME": str(home)}
    if step <= 0:
        args += ["--cache-dir", str(places["flag"])]
    if step <= 1:
        env["AGENT_SYNC_CACHE"] = str(places["agent-sync-cache"])
    if step <= 2:
        env["SKILL_SYNC_CACHE"] = str(places["skill-sync-cache"])
    if step <= 3:
        env["XDG_CACHE_HOME"] = str(sandbox.root / "c-xdg")

    result = run(sandbox, *args, env=env)

    assert result.returncode == 0, result.stderr
    cloned = {
        name: (place / "github.com" / "owner%2Frepo").is_dir() for name, place in places.items()
    }
    assert cloned == {name: name == CACHE_STEPS[step] for name in CACHE_STEPS}
    expected = places[CACHE_STEPS[step]] / "github.com" / "owner%2Frepo" / "skills" / "alpha"
    assert resolved(sandbox.skill_link("alpha")) == resolved(expected)


def test_empty_cache_variables_count_as_unset(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALPHA)
    home = sandbox.root / "alt-home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "AGENT_SYNC_CACHE": "",
        "SKILL_SYNC_CACHE": str(sandbox.root / "c-skill"),
        "XDG_CACHE_HOME": "",
    }

    result = run(sandbox, "--apply", env=env)

    assert result.returncode == 0, result.stderr
    assert (sandbox.root / "c-skill" / "github.com" / "owner%2Frepo").is_dir()
    assert list(home.iterdir()) == []


def test_empty_xdg_cache_home_falls_back_to_the_home_cache(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALPHA)
    home = sandbox.root / "alt-home"
    home.mkdir()

    result = run(sandbox, "--apply", env={"HOME": str(home), "XDG_CACHE_HOME": ""})

    assert result.returncode == 0, result.stderr
    assert (home / ".cache" / "agent-sync" / "github.com" / "owner%2Frepo").is_dir()


def test_the_default_cache_is_under_home_and_nowhere_else(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALPHA)
    home = sandbox.root / "alt-home"
    home.mkdir()

    result = run(sandbox, "--apply", env={"HOME": str(home)})

    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in home.iterdir()) == [".cache"]
    assert [p.name for p in (home / ".cache").iterdir()] == ["agent-sync"]
    assert not (sandbox.project / ".skill-sources").exists()


# -- personal folder --------------------------------------------------------------------------


def test_personal_dir_flag_supplies_personal_skills(sandbox: Sandbox) -> None:
    folder = make_personal(sandbox.root / "mine-dir")
    sandbox.write_config(PERSONAL)

    result = run(sandbox, "--apply", "--cache-dir", str(sandbox.cache), "--personal-dir", str(folder))

    assert result.returncode == 0, result.stderr
    assert resolved(sandbox.skill_link("mine")) == resolved(folder / "mine")
    assert not sandbox.cache.exists()  # nothing was downloaded


def test_personal_dir_with_a_skills_subfolder_is_scanned_there(sandbox: Sandbox) -> None:
    folder = make_personal(sandbox.root / "mine-dir", "nested")
    sandbox.write_config(PERSONAL)

    result = run(sandbox, "--apply", "--cache-dir", str(sandbox.cache), "--personal-dir", str(folder))

    assert result.returncode == 0, result.stderr
    assert resolved(sandbox.skill_link("mine")) == resolved(folder / "skills" / "mine")


def test_personal_env_variable_is_used_without_the_flag(sandbox: Sandbox) -> None:
    folder = make_personal(sandbox.root / "from-env")
    sandbox.write_config(PERSONAL)

    result = run(
        sandbox, "--apply", "--cache-dir", str(sandbox.cache),
        env={"AGENT_SYNC_PERSONAL": str(folder)},
    )  # fmt: skip

    assert result.returncode == 0, result.stderr
    assert resolved(sandbox.skill_link("mine")) == resolved(folder / "mine")


def test_personal_dir_flag_wins_over_the_env_variable(sandbox: Sandbox) -> None:
    flag_folder = make_personal(sandbox.root / "from-flag")
    env_folder = make_personal(sandbox.root / "from-env")
    sandbox.write_config(PERSONAL)

    result = run(
        sandbox, "--apply", "--cache-dir", str(sandbox.cache), "--personal-dir", str(flag_folder),
        env={"AGENT_SYNC_PERSONAL": str(env_folder)},
    )  # fmt: skip

    assert result.returncode == 0, result.stderr
    assert resolved(sandbox.skill_link("mine")) == resolved(flag_folder / "mine")


@pytest.mark.parametrize("env", [{}, {"AGENT_SYNC_PERSONAL": ""}], ids=["unset", "empty"])
def test_a_personal_selector_without_a_personal_folder_says_how_to_set_one(
    sandbox: Sandbox, env: dict[str, str]
) -> None:
    sandbox.write_config(PERSONAL)

    result = run(sandbox, "--apply", "--cache-dir", str(sandbox.cache), env=env)

    assert result.returncode == 1
    assert "--personal-dir" in result.stderr
    assert "AGENT_SYNC_PERSONAL" in result.stderr
    assert sandbox.installed_skills() == []
    assert not (sandbox.project / ".agents").exists()


def test_a_missing_personal_folder_is_an_error(sandbox: Sandbox) -> None:
    sandbox.write_config(PERSONAL)
    missing = sandbox.root / "nowhere"

    result = run(sandbox, "--apply", "--cache-dir", str(sandbox.cache), "--personal-dir", str(missing))

    assert result.returncode == 1
    assert str(missing) in result.stderr
    assert not (sandbox.project / ".agents").exists()


def test_a_missing_personal_folder_is_reported_before_any_download(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(
        "skills:\n  common:\n    - github.com:owner/repo:alpha\n    - personal:mine\n"
    )

    result = run(sandbox, "--apply", "--cache-dir", str(sandbox.cache))

    assert result.returncode == 1
    assert "--personal-dir" in result.stderr
    assert not sandbox.cache.exists()  # nothing was cloned first
    assert not (sandbox.project / ".agents").exists()


def test_tilde_is_expanded_in_the_cache_and_personal_variables(sandbox: Sandbox) -> None:
    home = sandbox.root / "alt-home"
    make_personal(home / "my-skills")
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(PERSONAL + "  claude:\n    - github.com:owner/repo:alpha\n")
    env = {
        "HOME": str(home),
        "AGENT_SYNC_CACHE": "~/the-cache",
        "AGENT_SYNC_PERSONAL": "~/my-skills",
    }

    result = run(sandbox, "--apply", env=env)

    assert result.returncode == 0, result.stderr
    assert (home / "the-cache" / "github.com" / "owner%2Frepo").is_dir()
    assert resolved(sandbox.skill_link("mine")) == resolved(home / "my-skills" / "mine")
    assert not (sandbox.project / "~").exists() and not (sandbox.cwd / "~").exists()


def test_a_personal_dir_without_personal_selectors_is_not_needed(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALPHA)

    result = run(sandbox, "--apply", "--cache-dir", str(sandbox.cache))

    assert result.returncode == 0, result.stderr


def test_a_personal_link_from_an_old_install_is_adopted_and_pruned(sandbox: Sandbox) -> None:
    folder = make_personal(sandbox.root / "mine-dir")
    (folder / "gone").mkdir()
    (folder / "gone" / "SKILL.md").write_text("# gone\n", encoding="utf-8")
    sandbox.write_config(PERSONAL)
    sandbox.skills_dir.mkdir(parents=True)
    for name in ("mine", "gone"):
        sandbox.skill_link(name).symlink_to(folder / name, target_is_directory=True)
    flags = ["--cache-dir", str(sandbox.cache), "--personal-dir", str(folder)]

    result = run(sandbox, "--apply", "--prune", *flags)

    assert result.returncode == 0, result.stderr
    assert "Adopt" in result.stdout
    assert sandbox.installed_skills() == ["mine"]
    assert os.readlink(sandbox.skill_link("mine")) == str(folder / "mine")


def test_a_full_run_with_a_personal_dir_leaves_home_empty(sandbox: Sandbox) -> None:
    folder = make_personal(sandbox.root / "mine-dir")
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(PERSONAL + "  claude:\n    - github.com:owner/repo:alpha\n")

    result = run(sandbox, "--apply", "--cache-dir", str(sandbox.cache), "--personal-dir", str(folder))

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha", "mine"]
    assert list(sandbox.home.iterdir()) == []


# -- --force ----------------------------------------------------------------------------------


def test_an_edited_link_is_an_error_without_force_and_restored_with_it(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALPHA)
    assert sandbox.sync("--apply").returncode == 0
    link = sandbox.skill_link("alpha")
    expected = os.readlink(link)
    elsewhere = sandbox.root / "elsewhere"
    elsewhere.mkdir()
    link.unlink()
    link.symlink_to(elsewhere, target_is_directory=True)

    refused = sandbox.sync("--apply")

    assert refused.returncode == 1
    assert "--force" in refused.stderr
    assert ".agents/skills/alpha" in refused.stderr
    assert os.readlink(link) == str(elsewhere)

    restored = sandbox.sync("--apply", "--force")

    assert restored.returncode == 0, restored.stderr
    assert os.readlink(link) == expected
    assert "0 changes" in sandbox.sync("--apply").stdout


def test_force_prunes_an_edited_owned_link_that_left_the_manifest(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(ALL)
    assert sandbox.sync("--apply").returncode == 0
    beta = sandbox.skill_link("beta")
    beta.unlink()
    beta.symlink_to(sandbox.root, target_is_directory=True)
    sandbox.write_config(ALPHA)

    assert sandbox.sync("--apply", "--prune").returncode == 1
    assert beta.is_symlink()

    result = sandbox.sync("--apply", "--prune", "--force")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha"]


def test_force_never_touches_a_foreign_target(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALPHA)
    mine = sandbox.skill_link("alpha")
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("mine", encoding="utf-8")

    result = sandbox.sync("--apply", "--force")

    assert result.returncode == 1
    assert "Target already exists" in result.stderr
    assert (mine / "SKILL.md").read_text(encoding="utf-8") == "mine"
    assert not mine.is_symlink()


# -- summary line -----------------------------------------------------------------------------


def test_summary_uses_the_singular_for_one_change(sandbox: Sandbox) -> None:
    sandbox.write_config("skills:\n  common: []\n")

    dry = sandbox.sync()
    applied = sandbox.sync("--apply")

    assert "Dry-run: 1 change, 0 missing." in dry.stdout
    assert "Applied: 1 change, 0 missing." in applied.stdout
    assert "1 changes" not in dry.stdout + applied.stdout


def test_summary_uses_the_plural_otherwise(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(ALL)

    assert "Applied: 3 changes, 0 missing." in sandbox.sync("--apply").stdout
    assert "Applied: 0 changes, 0 missing." in sandbox.sync("--apply").stdout


def test_summary_counts_pruned_links_only_when_something_was_pruned(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(ALL)
    assert sandbox.sync("--apply").returncode == 0
    sandbox.write_config(ALPHA)

    nothing_to_prune = sandbox.sync("--apply")
    pruned = sandbox.sync("--apply", "--prune")
    nothing_left = sandbox.sync("--apply", "--prune")

    assert not re.search(r", \d+ pruned", nothing_to_prune.stdout)
    assert "Applied: 1 change, 0 missing, 1 pruned." in pruned.stdout
    assert not re.search(r", \d+ pruned", nothing_left.stdout)


# -- error messages ---------------------------------------------------------------------------


def test_config_not_found_says_how_to_fix_it_without_the_hub(sandbox: Sandbox) -> None:
    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Config not found" in result.stderr
    assert str(sandbox.project / "config.yaml") in result.stderr
    assert "--config" in result.stderr
    assert not re.search(r"\bhub\b", result.stderr, re.IGNORECASE)


def test_unknown_skill_after_apply_does_not_suggest_update(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config("skills:\n  common:\n    - github.com:owner/repo:ghost\n")

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Skill 'ghost' not in" in result.stderr
    assert "Available skills: [alpha]" in result.stderr
    assert "--update" not in result.stderr
    assert "just refreshed" in result.stderr


def test_unknown_skill_after_apply_with_update_says_the_same(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config("skills:\n  common:\n    - github.com:owner/repo:ghost\n")

    result = sandbox.sync("--apply", "--update")

    assert result.returncode == 1
    assert "--update" not in result.stderr
    assert "just refreshed" in result.stderr


def test_unknown_skill_message_in_each_situation(tmp_path: Path) -> None:
    where = tmp_path / "cache" / "repo"

    dry = unknown_skill_message("ghost", where, ["alpha"], refreshed=False, personal=False)
    applied = unknown_skill_message("ghost", where, ["alpha"], refreshed=True, personal=False)
    personal = unknown_skill_message("ghost", where, [], refreshed=False, personal=True)

    assert dry.startswith(f"Skill 'ghost' not in {where}. Available skills: [alpha].")
    assert "--apply --update" in dry
    assert applied.startswith(f"Skill 'ghost' not in {where}. Available skills: [alpha].")
    assert "just refreshed" in applied and "--update" not in applied and "--apply" not in applied
    assert personal == f"Skill 'ghost' not in {where}. Available skills: [none]."


# -- exit codes and --help ----------------------------------------------------------------------


def test_exit_code_is_zero_on_success_and_one_on_any_error(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALPHA)

    ok = sandbox.sync()
    dry_apply = sandbox.sync("--apply")
    bad_manifest = sandbox.write_config("skills:\n  common:\n    - nonsense\n")
    failed = sandbox.sync("--apply", config=bad_manifest)
    bad_flag = sandbox.sync("--no-such-flag")

    assert (ok.returncode, dry_apply.returncode) == (0, 0)
    assert failed.returncode == 1 and failed.stderr.startswith("Error:")
    assert bad_flag.returncode == 2  # argparse's own usage error


def test_help_names_agent_sync_and_documents_every_flag(sandbox: Sandbox) -> None:
    result = run_sync(["--help"], env=sandbox.env, cwd=sandbox.cwd)  # python -m agent_sync

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: agent-sync ")
    for flag in (
        "--project", "--config", "--apply", "--update", "--prune", "--force",
        "--cache-dir", "--personal-dir",
    ):  # fmt: skip
        assert flag in result.stdout
    for gone in ("next to this script", "Skill sync tool", "skill-sync"):
        assert gone not in result.stdout
    assert not re.search(r"\bhub\b", result.stdout, re.IGNORECASE)
    flat = " ".join(result.stdout.split())
    assert "relative" in flat and "current directory" in flat
    assert "AGENT_SYNC_CACHE" in flat and "AGENT_SYNC_PERSONAL" in flat
    assert "~/.cache/agent-sync" in flat
