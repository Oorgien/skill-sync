"""Skills on the engine: state, git exclude, adoption of old installs, all-or-nothing.

CLI-level tests drive the tool through `Sandbox`; the planner tests call `plan_skills` directly on
a local `personal:` source, so they need no repository and no network.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import Sandbox, resolved, snapshot

from agent_sync.context import Context
from agent_sync.engine import Op
from agent_sync.kinds.skills import plan_skills
from agent_sync.manifest import Harness, ManifestEntry
from agent_sync.state import Item, State, fingerprint_link

ALL = "skills:\n  common:\n    - github.com:owner/repo:all\n"
ALPHA = "skills:\n  common:\n    - github.com:owner/repo:alpha\n"
EMPTY = "skills:\n  common: []\n"
CLAUDE_LINK_TEXT = os.path.join("..", ".agents", "skills")
STATE = Path(".agents") / "agent-sync.state.json"
LOCK = Path(".agents") / "agent-sync.lock"


def git_init(sandbox: Sandbox) -> Path:
    subprocess.run(
        ["git", "init", "--quiet", str(sandbox.project)],
        env=sandbox.env,
        check=True,
        capture_output=True,
    )
    return sandbox.project / ".git" / "info" / "exclude"


def state_items(sandbox: Sandbox) -> list[dict[str, object]]:
    data = json.loads((sandbox.project / STATE).read_text(encoding="utf-8"))
    items: list[dict[str, object]] = data["items"]
    return items


def state_targets(sandbox: Sandbox, *, kind: str | None = None) -> list[str]:
    return sorted(
        str(i["target"]) for i in state_items(sandbox) if kind is None or i["kind"] == kind
    )


def exclude_block(exclude: Path) -> list[str]:
    """The lines inside this tool's block of the exclude file."""
    lines = exclude.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# >>> agent-sync"))
    end = next(i for i, line in enumerate(lines) if line.startswith("# <<< agent-sync"))
    return lines[start + 1 : end]


def wipe_agent_folders(sandbox: Sandbox) -> None:
    for path in (sandbox.project / ".agents", sandbox.project / ".claude"):
        for child in sorted(path.rglob("*"), reverse=True):
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                child.rmdir()
        path.rmdir()


def old_install(sandbox: Sandbox, names: list[str], *, claude_link: bool = False) -> None:
    """What the pre-state tool left behind: links into the cache, no state, no lock."""
    assert sandbox.sync("--apply").returncode == 0  # fills the cache
    wipe_agent_folders(sandbox)
    sandbox.skills_dir.mkdir(parents=True)
    for name in names:
        source = sandbox.cached_github_repo("owner/repo") / "skills" / name
        sandbox.skill_link(name).symlink_to(source, target_is_directory=True)
    if claude_link:
        (sandbox.project / ".claude").mkdir()
        (sandbox.project / ".claude" / "skills").symlink_to(
            CLAUDE_LINK_TEXT, target_is_directory=True
        )


# -- state ----------------------------------------------------------------------------------


def test_state_file_lists_every_skill_link_and_the_claude_link(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(ALL)

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    links = [i for i in state_items(sandbox) if i["kind"] == "skills"]
    assert sorted(str(i["target"]) for i in links) == [
        ".agents/skills/alpha",
        ".agents/skills/beta",
        ".claude/skills",
    ]
    by_target = {str(i["target"]): i for i in links}
    assert by_target[".agents/skills/alpha"]["source"] == "github.com:owner/repo:alpha"
    assert by_target[".agents/skills/beta"]["source"] == "github.com:owner/repo:beta"
    assert by_target[".agents/skills/alpha"]["harness"] is None
    assert by_target[".claude/skills"]["sha256"] == fingerprint_link(CLAUDE_LINK_TEXT)


def test_state_records_personal_and_npx_selectors(sandbox: Sandbox) -> None:
    sandbox.write_config("skills:\n  common:\n    - npx/some-package:ecc-guide\n")

    assert sandbox.sync("--apply").returncode == 0

    items = {str(i["target"]): i for i in state_items(sandbox)}
    assert items[".agents/skills/ecc-guide"]["source"] == "npx/some-package:ecc-guide"


# -- git exclude ----------------------------------------------------------------------------


def test_exclude_block_lists_links_state_lock_and_the_claude_link_but_no_folders(
    sandbox: Sandbox,
) -> None:
    exclude = git_init(sandbox)
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(ALL)

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert exclude_block(exclude) == [
        "/.agents/agent-sync.lock",
        "/.agents/agent-sync.state.json",
        "/.agents/skills/alpha",
        "/.agents/skills/beta",
        "/.claude/skills",
    ]
    assert "dir" in {i["kind"] for i in state_items(sandbox)}  # folders are recorded, not excluded


def test_exclude_does_not_list_a_claude_link_that_agent_sync_did_not_create(
    sandbox: Sandbox,
) -> None:
    exclude = git_init(sandbox)
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL)
    (sandbox.project / ".claude").mkdir()
    (sandbox.project / ".claude" / "skills").symlink_to(CLAUDE_LINK_TEXT, target_is_directory=True)

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert "/.claude/skills" not in exclude_block(exclude)
    assert ".claude/skills" not in state_targets(sandbox)


def test_prune_drops_the_pruned_link_from_the_exclude_block(sandbox: Sandbox) -> None:
    exclude = git_init(sandbox)
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(ALL)
    assert sandbox.sync("--apply").returncode == 0
    sandbox.write_config(ALPHA)

    assert sandbox.sync("--apply", "--prune").returncode == 0

    assert "/.agents/skills/beta" not in exclude_block(exclude)
    assert "/.agents/skills/alpha" in exclude_block(exclude)


def test_outside_git_the_exclude_step_prints_a_note_and_does_not_fail(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL)

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert "not a git repository" in result.stdout
    assert sandbox.installed_skills() == ["alpha"]


def test_a_broken_exclude_block_fails_before_anything_is_written(sandbox: Sandbox) -> None:
    exclude = git_init(sandbox)
    marker = f"# >>> agent-sync (managed) project={resolved(sandbox.project)} >>>\n/unclosed\n"
    exclude.write_text(marker, encoding="utf-8")
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL)
    before = snapshot(sandbox.project)

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "closing marker" in result.stderr
    assert exclude.read_text(encoding="utf-8") == marker
    assert {k: v for k, v in snapshot(sandbox.project).items() if not k.startswith(".git/")} == {
        k: v for k, v in before.items() if not k.startswith(".git/")
    }
    assert sandbox.installed_skills() == []


# -- idempotence ----------------------------------------------------------------------------


def test_a_second_apply_reports_no_changes_and_rewrites_no_file(sandbox: Sandbox) -> None:
    exclude = git_init(sandbox)
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(ALL)
    assert sandbox.sync("--apply").returncode == 0
    watched = [sandbox.project / STATE, sandbox.project / LOCK, exclude]
    before = [path.stat().st_mtime_ns for path in watched]
    links = {name: os.lstat(sandbox.skill_link(name)).st_ino for name in ("alpha", "beta")}

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert "0 changes" in result.stdout
    assert "Link" not in result.stdout and "Adopt" not in result.stdout
    assert [path.stat().st_mtime_ns for path in watched] == before
    assert links == {name: os.lstat(sandbox.skill_link(name)).st_ino for name in links}


def test_dry_run_prints_the_plan_and_writes_nothing_even_in_git(sandbox: Sandbox) -> None:
    exclude = git_init(sandbox)
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL)
    assert sandbox.sync("--apply").returncode == 0
    sandbox.write_config("skills:\n  common:\n    - github.com:owner/repo:all\n")
    sandbox.skill_link("alpha").unlink()
    before = snapshot(sandbox.project)
    exclude_before = exclude.read_bytes()

    result = sandbox.sync()

    assert result.returncode == 0, result.stderr
    assert "Would link" in result.stdout and "alpha" in result.stdout
    assert snapshot(sandbox.project) == before
    assert exclude.read_bytes() == exclude_before


# -- old installs ---------------------------------------------------------------------------


def test_an_old_install_is_adopted_without_rewriting_any_link(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(ALL)
    old_install(sandbox, ["alpha", "beta"], claude_link=True)
    before = {
        name: (os.lstat(sandbox.skill_link(name)).st_ino, os.readlink(sandbox.skill_link(name)))
        for name in ("alpha", "beta")
    }

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert "Link " not in result.stdout  # nothing was created or replaced
    assert result.stdout.count("Adopt") == 2
    after = {
        name: (os.lstat(sandbox.skill_link(name)).st_ino, os.readlink(sandbox.skill_link(name)))
        for name in ("alpha", "beta")
    }
    assert after == before
    assert state_targets(sandbox, kind="skills") == [".agents/skills/alpha", ".agents/skills/beta"]
    again = sandbox.sync("--apply")
    assert "0 changes" in again.stdout


def test_an_old_install_dry_run_says_would_adopt_and_writes_nothing(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL)
    old_install(sandbox, ["alpha"], claude_link=True)
    before = snapshot(sandbox.project)

    result = sandbox.sync()

    assert result.returncode == 0, result.stderr
    assert "Would adopt" in result.stdout
    assert snapshot(sandbox.project) == before


def test_prune_removes_an_old_install_link_the_manifest_dropped(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(ALL)
    old_install(sandbox, ["alpha", "beta"], claude_link=True)
    sandbox.write_config(ALPHA)

    dry = sandbox.sync("--prune")
    assert "Would remove" in dry.stdout and "beta" in dry.stdout
    assert sandbox.installed_skills() == ["alpha", "beta"]

    result = sandbox.sync("--apply", "--prune")

    assert result.returncode == 0, result.stderr
    assert "Remove" in result.stdout
    assert sandbox.installed_skills() == ["alpha"]
    assert (sandbox.project / ".claude" / "skills").is_symlink()
    assert state_targets(sandbox, kind="skills") == [".agents/skills/alpha"]


def test_prune_keeps_an_old_link_that_the_plan_still_makes(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL)
    old_install(sandbox, ["alpha"])

    result = sandbox.sync("--apply", "--prune")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha"]


# -- the adoption guard ---------------------------------------------------------------------


def test_a_real_folder_at_a_skill_target_is_never_adopted_and_nothing_is_written(
    sandbox: Sandbox,
) -> None:
    exclude = git_init(sandbox)
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(ALL)
    (sandbox.skill_link("alpha")).mkdir(parents=True)
    (sandbox.skill_link("alpha") / "SKILL.md").write_text("mine", encoding="utf-8")
    exclude_before = exclude.read_bytes()

    result = sandbox.sync("--apply", "--prune")

    assert result.returncode == 1
    assert "Target already exists" in result.stderr
    assert not sandbox.skill_link("alpha").is_symlink()
    assert not sandbox.skill_link("beta").exists()  # all or nothing
    assert not (sandbox.project / ".claude").exists()
    assert sorted(p.name for p in (sandbox.project / ".agents").iterdir()) == ["skills"]
    assert exclude.read_bytes() == exclude_before


def test_a_foreign_symlink_at_a_skill_target_is_never_adopted(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL)
    elsewhere = sandbox.root / "elsewhere"
    elsewhere.mkdir()
    sandbox.skills_dir.mkdir(parents=True)
    sandbox.skill_link("alpha").symlink_to(elsewhere, target_is_directory=True)

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Target already exists" in result.stderr
    assert os.readlink(sandbox.skill_link("alpha")) == str(elsewhere)
    assert not (sandbox.project / STATE).exists()


def test_a_foreign_claude_skills_link_is_not_adopted_and_nothing_is_written(
    sandbox: Sandbox,
) -> None:
    exclude = git_init(sandbox)
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL)
    elsewhere = sandbox.root / "elsewhere"
    elsewhere.mkdir()
    (sandbox.project / ".claude").mkdir()
    (sandbox.project / ".claude" / "skills").symlink_to(elsewhere, target_is_directory=True)
    exclude_before = exclude.read_bytes()

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Target already exists" in result.stderr
    assert not sandbox.skill_link("alpha").exists()
    assert not (sandbox.project / STATE).exists()
    assert not (sandbox.project / LOCK).exists()
    assert exclude.read_bytes() == exclude_before


# -- .claude/skills survives prune ----------------------------------------------------------


def test_claude_skills_created_by_agent_sync_survives_prune_with_an_empty_list(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL)
    assert sandbox.sync("--apply").returncode == 0
    sandbox.write_config(EMPTY)

    result = sandbox.sync("--apply", "--prune")

    assert result.returncode == 0, result.stderr
    claude_skills = sandbox.project / ".claude" / "skills"
    assert os.readlink(claude_skills) == CLAUDE_LINK_TEXT
    assert state_targets(sandbox, kind="skills") == [".claude/skills"]
    assert not os.path.lexists(sandbox.skill_link("alpha"))
    assert "0 changes" in sandbox.sync("--apply", "--prune").stdout


def test_claude_skills_made_by_hand_survives_prune_with_an_empty_list(sandbox: Sandbox) -> None:
    sandbox.write_config(EMPTY)
    sandbox.skills_dir.mkdir(parents=True)
    claude_skills = sandbox.project / ".claude" / "skills"
    claude_skills.parent.mkdir()
    claude_skills.symlink_to(CLAUDE_LINK_TEXT, target_is_directory=True)

    result = sandbox.sync("--apply", "--prune")

    assert result.returncode == 0, result.stderr
    assert os.readlink(claude_skills) == CLAUDE_LINK_TEXT
    assert not (sandbox.project / STATE).exists()  # nothing to record, nothing to do


def test_an_empty_skills_list_still_creates_claude_skills(sandbox: Sandbox) -> None:
    sandbox.write_config(EMPTY)

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert os.readlink(sandbox.project / ".claude" / "skills") == CLAUDE_LINK_TEXT


# -- a source refreshed at most once, and honest hints ---------------------------------------


TWO_NAMED = (
    "skills:\n  common:\n    - github.com:owner/repo:alpha\n    - github.com:owner/repo:newcomer\n"
)


def test_two_named_skills_from_one_repo_install_when_the_second_was_added_upstream(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALPHA)
    assert sandbox.sync("--apply").returncode == 0
    sandbox.add_skills("owner/repo", ["newcomer"])
    sandbox.write_config(TWO_NAMED)

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha", "newcomer"]
    expected = sandbox.cached_github_repo("owner/repo") / "skills" / "newcomer"
    assert resolved(sandbox.skill_link("newcomer")) == resolved(expected)


def test_a_skill_missing_upstream_gets_the_honest_message_after_one_refresh(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALPHA)
    assert sandbox.sync("--apply").returncode == 0
    sandbox.write_config(
        "skills:\n  common:\n    - github.com:owner/repo:alpha\n    - github.com:owner/repo:ghost\n"
    )

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Skill 'ghost' not in" in result.stderr
    assert "Available skills: [alpha]" in result.stderr
    assert "just refreshed" in result.stderr
    assert "--update" not in result.stderr


def test_a_source_is_refreshed_once_even_when_several_named_skills_are_new(
    sandbox: Sandbox,
) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALPHA)
    assert sandbox.sync("--apply").returncode == 0
    clone = sandbox.cached_github_repo("owner/repo")
    marker = clone / "local-marker.txt"
    marker.write_text("only a fresh clone drops this", encoding="utf-8")
    sandbox.add_skills("owner/repo", ["new-one", "new-two"])
    sandbox.write_config(
        "skills:\n  common:\n    - github.com:owner/repo:alpha\n"
        "    - github.com:owner/repo:new-one\n    - github.com:owner/repo:new-two\n"
    )

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha", "new-one", "new-two"]
    assert marker.is_file()  # pulled into the clone, never cloned again


# -- the planner's hints in conflicts -------------------------------------------------------


def test_a_real_claude_skills_folder_blocks_the_run_and_the_message_says_what_to_do(
    sandbox: Sandbox,
) -> None:
    sandbox.write_config(EMPTY)
    (sandbox.project / ".claude" / "skills" / "mine").mkdir(parents=True)

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert result.stderr.count("Target already exists") == 1
    assert "move" in result.stderr and ".agents/skills" in result.stderr
    assert "delete the folder" in result.stderr


def test_a_foreign_skill_symlink_message_mentions_old_installs(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALPHA)
    old_cache = sandbox.root / "old-checkout" / ".skill-sources"
    (old_cache / "x").mkdir(parents=True)
    sandbox.skills_dir.mkdir(parents=True)
    sandbox.skill_link("alpha").symlink_to(old_cache / "x", target_is_directory=True)

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Error: Target already exists" in result.stderr
    assert "--cache-dir" in result.stderr and "--personal-dir" in result.stderr
    assert "older skill-sync" in result.stderr


def test_passing_the_old_cache_adopts_an_old_default_location_install(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALPHA)
    old_cache = sandbox.root / "old-checkout" / ".skill-sources"
    assert sandbox.sync("--apply", cache=old_cache).returncode == 0
    wipe_agent_folders(sandbox)
    sandbox.skills_dir.mkdir(parents=True)
    source = sandbox.cached_github_repo("owner/repo", old_cache) / "skills" / "alpha"
    sandbox.skill_link("alpha").symlink_to(source, target_is_directory=True)
    (sandbox.project / ".claude").mkdir()
    (sandbox.project / ".claude" / "skills").symlink_to(CLAUDE_LINK_TEXT, target_is_directory=True)

    refused = sandbox.sync("--apply")  # the new default cache is somewhere else
    adopted = sandbox.sync("--apply", cache=old_cache)

    assert refused.returncode == 1 and "Target already exists" in refused.stderr
    assert adopted.returncode == 0, adopted.stderr
    assert "Adopt" in adopted.stdout and "Link " not in adopted.stdout


# -- symlink loops --------------------------------------------------------------------------


def test_a_looping_skill_link_is_a_conflict_not_a_crash(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALPHA)
    sandbox.skills_dir.mkdir(parents=True)
    sandbox.skill_link("alpha").symlink_to("alpha")

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Error: Target already exists" in result.stderr
    assert os.readlink(sandbox.skill_link("alpha")) == "alpha"
    assert not (sandbox.project / STATE).exists()


def test_a_looping_link_is_not_pruned(sandbox: Sandbox) -> None:
    sandbox.write_config(EMPTY)
    sandbox.skills_dir.mkdir(parents=True)
    sandbox.skill_link("loop").symlink_to("loop")

    result = sandbox.sync("--apply", "--prune")

    assert result.returncode == 0, result.stderr
    assert os.readlink(sandbox.skill_link("loop")) == "loop"


def test_a_looping_claude_skills_link_is_a_clear_conflict(sandbox: Sandbox) -> None:
    sandbox.write_config(EMPTY)
    (sandbox.project / ".claude").mkdir()
    (sandbox.project / ".claude" / "skills").symlink_to("skills")

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "Target already exists" in result.stderr
    assert "symlink loop" in result.stderr
    assert "remove it" in result.stderr
    assert os.readlink(sandbox.project / ".claude" / "skills") == "skills"


def test_planner_treats_loops_as_not_resolvable(world: World) -> None:
    skills = world.skills()
    (skills / "alpha").symlink_to("alpha")
    (skills / "stray").symlink_to("stray")
    (world.project / ".claude").mkdir()
    (world.project / ".claude" / "skills").symlink_to("skills")

    plan = plan_skills(personal("alpha"), world.ctx(prune=True))

    ops = ops_by_target(plan.ops)
    assert not ops[".agents/skills/alpha"].adopt
    assert ".agents/skills/stray" not in ops
    assert "symlink loop" in ops[".claude/skills"].hint
    assert not ops[".claude/skills"].adopt


# -- an apply with nothing to do ------------------------------------------------------------


def test_an_apply_with_nothing_to_do_creates_nothing_and_leaves_the_exclude_alone(
    sandbox: Sandbox,
) -> None:
    exclude = git_init(sandbox)
    exclude_before = exclude.read_bytes()
    sandbox.write_config(EMPTY)
    sandbox.skills_dir.mkdir(parents=True)
    claude_skills = sandbox.project / ".claude" / "skills"
    claude_skills.parent.mkdir()
    claude_skills.symlink_to(CLAUDE_LINK_TEXT, target_is_directory=True)

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert "0 changes" in result.stdout
    assert exclude.read_bytes() == exclude_before
    assert sorted(p.name for p in (sandbox.project / ".agents").iterdir()) == ["skills"]


# -- old installs in a git project ----------------------------------------------------------


def test_adopting_and_pruning_an_old_install_keeps_the_exclude_block_exact(
    sandbox: Sandbox,
) -> None:
    exclude = git_init(sandbox)
    original = exclude.read_text(encoding="utf-8") + "my-own-line\n"
    exclude.write_text(original, encoding="utf-8")
    sandbox.make_repo("owner/repo", ["alpha", "beta"])
    sandbox.write_config(ALL)
    old_install(sandbox, ["alpha", "beta"], claude_link=True)  # its first run wrote a block
    exclude.write_text(original, encoding="utf-8")  # back to "before": no agent-sync block
    sandbox.write_config(ALPHA)
    assert "agent-sync (managed)" not in exclude.read_text(encoding="utf-8")

    result = sandbox.sync("--apply", "--prune")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha"]
    assert exclude_block(exclude) == [
        "/.agents/agent-sync.lock",
        "/.agents/agent-sync.state.json",
        "/.agents/skills/alpha",
    ]
    assert "my-own-line" in exclude.read_text(encoding="utf-8")
    assert "0 changes" in sandbox.sync("--apply", "--prune").stdout


# -- --force and foreign .claude/skills -----------------------------------------------------


@pytest.mark.parametrize("kind", ["symlink", "folder"])
def test_force_does_not_touch_a_foreign_claude_skills(sandbox: Sandbox, kind: str) -> None:
    sandbox.write_config(EMPTY)
    claude_skills = sandbox.project / ".claude" / "skills"
    claude_skills.parent.mkdir()
    if kind == "symlink":
        elsewhere = sandbox.root / "elsewhere"
        elsewhere.mkdir()
        claude_skills.symlink_to(elsewhere, target_is_directory=True)
    else:
        claude_skills.mkdir()
        (claude_skills / "mine").write_text("mine", encoding="utf-8")
    before = snapshot(sandbox.project / ".claude")

    result = sandbox.sync("--apply", "--prune", "--force")

    assert result.returncode == 1
    assert "Target already exists" in result.stderr
    assert snapshot(sandbox.project / ".claude") == before
    assert not (sandbox.project / STATE).exists()


# -- other kinds ----------------------------------------------------------------------------


def test_a_manifest_with_mcp_is_not_supported_yet_and_writes_nothing(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL + "mcp:\n  common:\n    - github.com:owner/repo:architect\n")
    before = snapshot(sandbox.project)

    result = sandbox.sync("--apply")

    assert result.returncode == 1
    assert "mcp is not supported yet" in result.stderr
    assert snapshot(sandbox.project) == before
    assert not sandbox.cache.exists()


def test_an_empty_section_of_another_kind_is_fine(sandbox: Sandbox) -> None:
    sandbox.make_repo("owner/repo", ["alpha"])
    sandbox.write_config(ALL + "agents:\n  common: []\nmcp:\n")

    result = sandbox.sync("--apply")

    assert result.returncode == 0, result.stderr
    assert sandbox.installed_skills() == ["alpha"]


# -- planner --------------------------------------------------------------------------------


def personal(*names: str) -> list[ManifestEntry]:
    both: frozenset[Harness] = frozenset({"claude", "codex"})
    return [ManifestEntry("skills", both, "personal", None, name) for name in names]


class World:
    """A personal folder holding <name>/SKILL.md, an empty cache and a project, under tmp_path."""

    def __init__(self, root: Path) -> None:
        self.personal = root / "personal"
        self.cache = root / "cache"
        self.project = root / "project"
        for name in ("alpha", "beta"):
            (self.personal / name).mkdir(parents=True)
            (self.personal / name / "SKILL.md").write_text(name, encoding="utf-8")
        self.cache.mkdir()
        self.project.mkdir()

    def ctx(self, *, prune: bool = False, state: State | None = None) -> Context:
        return Context(
            project=self.project,
            personal_dir=self.personal,
            cache_root=self.cache,
            apply=False,
            update=False,
            prune=prune,
            state=state or State(()),
        )

    def source(self, name: str) -> Path:
        return self.personal / name

    def skills(self) -> Path:
        path = self.project / ".agents" / "skills"
        path.mkdir(parents=True, exist_ok=True)
        return path


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def ops_by_target(ops: list[Op]) -> dict[str, Op]:
    return {op.target: op for op in ops}


def test_planner_emits_one_link_per_skill_and_the_claude_link(world: World) -> None:
    plan = plan_skills(personal("alpha", "beta"), world.ctx())

    assert plan.missing == []
    assert plan.ops == [
        Op("link", ".agents/skills/alpha", "skills", None, "personal:alpha", str(world.source("alpha"))),
        Op("link", ".agents/skills/beta", "skills", None, "personal:beta", str(world.source("beta"))),
        Op("link", ".claude/skills", "skills", None, "", CLAUDE_LINK_TEXT),
    ]


def test_planner_emits_the_claude_link_for_an_empty_list(world: World) -> None:
    plan = plan_skills([], world.ctx())

    assert [(o.target, o.payload, o.adopt) for o in plan.ops] == [
        (".claude/skills", CLAUDE_LINK_TEXT, False)
    ]


def test_planner_lists_sources_missing_from_the_cache_in_a_dry_run(world: World) -> None:
    entry = ManifestEntry("skills", frozenset({"claude"}), "github.com", "o/r", "alpha")

    plan = plan_skills([entry], world.ctx())

    assert plan.missing == [entry]
    assert [o.target for o in plan.ops] == [".claude/skills"]


def test_planner_collision_is_an_error(world: World) -> None:
    other = world.personal.parent / "other"
    (other / "alpha").mkdir(parents=True)
    (other / "alpha" / "SKILL.md").write_text("x", encoding="utf-8")
    cached = world.cache / "github.com" / "o%2Fr"
    (cached / "skills" / "alpha").mkdir(parents=True)
    (cached / "skills" / "alpha" / "SKILL.md").write_text("x", encoding="utf-8")
    entries = [
        *personal("alpha"),
        ManifestEntry("skills", frozenset({"claude"}), "github.com", "o/r", "alpha"),
    ]

    with pytest.raises(ValueError, match="Collision for skill: alpha"):
        plan_skills(entries, world.ctx())


def test_planner_adopts_a_link_into_the_cache_or_custom_skills_or_a_dangling_one(
    world: World,
) -> None:
    skills = world.skills()
    (world.cache / "old").mkdir()
    (skills / "alpha").symlink_to(world.cache / "old")  # into the cache
    (skills / "beta").symlink_to(world.source("beta"))  # into the personal folder
    entries = personal("alpha", "beta")
    ops = ops_by_target(plan_skills(entries, world.ctx()).ops)
    assert ops[".agents/skills/alpha"].adopt
    assert ops[".agents/skills/beta"].adopt

    (skills / "alpha").unlink()
    (skills / "alpha").symlink_to(world.project / "nowhere")  # dangling
    assert ops_by_target(plan_skills(entries, world.ctx()).ops)[".agents/skills/alpha"].adopt


def test_planner_never_adopts_a_real_folder_or_a_foreign_symlink(world: World) -> None:
    skills = world.skills()
    (skills / "alpha").mkdir()
    foreign = world.project / "foreign"
    foreign.mkdir()
    (skills / "beta").symlink_to(foreign)

    ops = ops_by_target(plan_skills(personal("alpha", "beta"), world.ctx()).ops)

    assert not ops[".agents/skills/alpha"].adopt
    assert not ops[".agents/skills/beta"].adopt


def test_planner_does_not_adopt_what_the_state_already_owns(world: World) -> None:
    skills = world.skills()
    (skills / "alpha").symlink_to(world.cache / "old")
    owned = Item("skills", None, ".agents/skills/alpha", None, "personal:alpha", "x")

    ops = ops_by_target(plan_skills(personal("alpha"), world.ctx(state=State((owned,)))).ops)

    assert not ops[".agents/skills/alpha"].adopt


def test_planner_claude_link_cases(world: World) -> None:
    claude = world.project / ".claude"
    claude.mkdir()
    link = claude / "skills"
    owned = Item("skills", None, ".claude/skills", None, "", "x")

    # unrecorded and in place: left alone
    link.symlink_to(CLAUDE_LINK_TEXT)
    assert ".claude/skills" not in ops_by_target(plan_skills([], world.ctx()).ops)

    # recorded and reaching the pool by another path: a link op with the CURRENT text
    link.unlink()
    link.symlink_to(world.project / ".agents" / "skills")
    op = ops_by_target(plan_skills([], world.ctx(state=State((owned,)))).ops)[".claude/skills"]
    assert (op.payload, op.adopt) == (str(world.project / ".agents" / "skills"), False)

    # a foreign link: a plain link op, never adopted
    link.unlink()
    link.symlink_to(world.project)
    op = ops_by_target(plan_skills([], world.ctx()).ops)[".claude/skills"]
    assert (op.payload, op.adopt) == (CLAUDE_LINK_TEXT, False)

    # a real folder: the same
    link.unlink()
    link.mkdir()
    op = ops_by_target(plan_skills([], world.ctx()).ops)[".claude/skills"]
    assert (op.payload, op.adopt) == (CLAUDE_LINK_TEXT, False)


def test_planner_prune_removes_only_unowned_unplanned_managed_links(world: World) -> None:
    skills = world.skills()
    (world.cache / "old").mkdir()
    (skills / "gone").symlink_to(world.cache / "old")  # managed, dropped: removed
    (skills / "dangling").symlink_to(world.cache / "nowhere")  # dangling into the cache: removed
    (skills / "stray").symlink_to(world.project / "nowhere")  # dangling elsewhere: not ours
    (skills / "alpha").symlink_to(world.source("alpha"))  # planned: kept
    (skills / "foreign").symlink_to(world.project)  # foreign: kept
    (skills / "folder").mkdir()  # real folder: kept
    (skills / "owned").symlink_to(world.cache / "old")  # in state: the engine's business
    owned = Item("skills", None, ".agents/skills/owned", None, "s", "x")

    plan = plan_skills(personal("alpha"), world.ctx(prune=True, state=State((owned,))))

    removes = [op for op in plan.ops if op.action == "remove"]
    assert sorted((o.target, o.adopt) for o in removes) == [
        (".agents/skills/dangling", True),
        (".agents/skills/gone", True),
    ]
    assert plan_skills(personal("alpha"), world.ctx(prune=False)).ops[0].action == "link"
    assert not [o for o in plan_skills(personal("alpha"), world.ctx()).ops if o.action == "remove"]
