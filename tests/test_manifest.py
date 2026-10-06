"""Manifest format v2: kinds, sections, harness resolution and selector grammar."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_sync.manifest import (
    KINDS,
    SECTIONS,
    Harness,
    ManifestEntry,
    load_manifest,
    parse_manifest,
)
from agent_sync.sources import parse_entry

REPO_ROOT = Path(__file__).resolve().parent.parent
BOTH: frozenset[Harness] = frozenset({"claude", "codex"})
CLAUDE: frozenset[Harness] = frozenset({"claude"})
CODEX: frozenset[Harness] = frozenset({"codex"})


def _skill_tuples(entries: list[ManifestEntry]) -> list[tuple[str, str | None, str]]:
    return [(e.source, e.repo, e.name) for e in entries]


def test_constants() -> None:
    assert KINDS == ("skills", "agents", "commands", "rules", "hooks", "mcp")
    assert SECTIONS == ("common", "claude", "codex")


@pytest.mark.parametrize("data", [None, {}])
def test_empty_manifest_has_no_entries(data: Any) -> None:
    assert parse_manifest(data) == []


def test_kind_and_section_may_be_empty() -> None:
    assert (
        parse_manifest({"skills": None, "agents": {}, "rules": {"common": None, "codex": []}}) == []
    )


# --- today's manifests keep working -------------------------------------------------------------


def test_repo_config_yaml_yields_same_skill_entries_as_parse_entry() -> None:
    data = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    expected = [
        entry
        for section in ("common", "claude", "codex")
        for item in (data["skills"].get(section) or [])
        for entry in parse_entry(item)
    ]
    entries = load_manifest(REPO_ROOT / "config.yaml")
    assert expected  # the sample must not be vacuous
    assert [(e.source, e.repo, e.name) for e in entries] == [
        (x.source, x.repo, x.skill) for x in expected
    ]
    assert {e.kind for e in entries} == {"skills"}
    assert {e.harnesses for e in entries} == {BOTH}


def test_skills_only_manifest_covers_every_selector_form() -> None:
    items: list[Any] = [
        "github.com:owner/repo:all",
        "personal:mine",
        "npx/@scope/pkg:skill-a",
        {"github": ["o/r:s1", {"o/r2": ["s2", "s3"]}]},
        {"personal": ["p1", "p2"]},
        {"npx": ["pkg:s4"]},
    ]
    entries = parse_manifest({"skills": {"common": items}})
    expected = [x for item in items for x in parse_entry(item)]
    assert _skill_tuples(entries) == [(x.source, x.repo, x.skill) for x in expected]


# --- harness resolution -------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["skills", "commands"])
def test_skills_and_commands_are_one_pool_for_every_section(kind: str) -> None:
    data = {
        kind: {
            "common": ["personal:a"],
            "claude": ["personal:b"],
            "codex": ["personal:c"],
        }
    }
    entries = parse_manifest(data)
    assert [(e.name, e.harnesses) for e in entries] == [("a", BOTH), ("b", BOTH), ("c", BOTH)]


@pytest.mark.parametrize("kind", ["agents", "rules", "mcp"])
def test_split_kinds_resolve_harness_from_section(kind: str) -> None:
    data = {
        kind: {
            "common": ["personal:a"],
            "claude": ["personal:b"],
            "codex": ["personal:c"],
        }
    }
    entries = parse_manifest(data)
    assert [(e.name, e.harnesses) for e in entries] == [
        ("a", BOTH),
        ("b", CLAUDE),
        ("c", CODEX),
    ]
    assert {e.kind for e in entries} == {kind}


def test_hooks_resolve_harness_from_section() -> None:
    data = {
        "hooks": {
            "common": [{"event": "x"}],
            "claude": [{"event": "y"}],
            "codex": [{"event": "z"}],
        }
    }
    assert [e.harnesses for e in parse_manifest(data)] == [BOTH, CLAUDE, CODEX]


def test_entry_fields_for_a_github_selector() -> None:
    (entry,) = parse_manifest({"agents": {"claude": ["github.com:o/r:reviewer"]}})
    assert entry == ManifestEntry("agents", CLAUDE, "github.com", "o/r", "reviewer")
    assert entry.raw is None


def test_personal_selector_has_no_repo() -> None:
    (entry,) = parse_manifest({"commands": {"common": ["personal:deploy"]}})
    assert (entry.source, entry.repo, entry.name) == ("personal", None, "deploy")


# --- hooks passthrough --------------------------------------------------------------------------


def test_hooks_pass_through_unparsed() -> None:
    raw = {"event": "PreToolUse", "weird": [1, {"a": None}], "npx/": "not a selector"}
    (entry,) = parse_manifest({"hooks": {"claude": [raw]}})
    assert entry == ManifestEntry("hooks", CLAUDE, "", None, "", raw)
    assert entry.raw is raw


def test_non_mapping_hooks_item_is_an_error() -> None:
    with pytest.raises(ValueError, match="hooks"):
        parse_manifest({"hooks": {"common": ["github.com:o/r:h"]}})


# --- ordering -----------------------------------------------------------------------------------


def test_order_is_kinds_then_sections_then_file_order() -> None:
    data = {  # deliberately written in the wrong order
        "mcp": {"codex": ["personal:m2"], "common": ["personal:m1"]},
        "rules": {"claude": ["personal:r2", "personal:r1"]},
        "skills": {"codex": ["personal:s3"], "claude": ["personal:s2"], "common": ["personal:s1"]},
        "hooks": {"common": [{"n": 2}, {"n": 1}]},
    }
    entries = parse_manifest(data)
    assert [(e.kind, e.name or e.raw["n"]) for e in entries] == [
        ("skills", "s1"),
        ("skills", "s2"),
        ("skills", "s3"),
        ("rules", "r2"),
        ("rules", "r1"),
        ("hooks", 2),
        ("hooks", 1),
        ("mcp", "m1"),
        ("mcp", "m2"),
    ]


# --- errors -------------------------------------------------------------------------------------


def test_unknown_top_level_key_is_named_and_valid_keys_listed() -> None:
    with pytest.raises(ValueError) as exc:
        parse_manifest({"skils": {"common": []}})
    message = str(exc.value)
    assert "skils" in message
    for kind in KINDS:
        assert kind in message


def test_unknown_section_names_kind_and_section() -> None:
    with pytest.raises(ValueError) as exc:
        parse_manifest({"rules": {"gemini": ["personal:a"]}})
    assert "rules" in str(exc.value)
    assert "gemini" in str(exc.value)


@pytest.mark.parametrize("data", [["skills"], "skills", 3])
def test_top_level_must_be_a_mapping(data: Any) -> None:
    with pytest.raises(ValueError, match="mapping"):
        parse_manifest(data)


def test_kind_must_be_a_mapping() -> None:
    with pytest.raises(ValueError, match="skills"):
        parse_manifest({"skills": ["personal:a"]})


def test_section_must_be_a_list() -> None:
    with pytest.raises(ValueError, match="common"):
        parse_manifest({"agents": {"common": "personal:a"}})


@pytest.mark.parametrize("kind", ["agents", "commands", "rules", "mcp"])
def test_npx_outside_skills_is_an_error_naming_kind_and_selector(kind: str) -> None:
    with pytest.raises(ValueError) as exc:
        parse_manifest({kind: {"common": ["npx/pkg:thing"]}})
    assert kind in str(exc.value)
    assert "npx/pkg:thing" in str(exc.value)


def test_npx_dict_form_outside_skills_is_an_error() -> None:
    with pytest.raises(ValueError, match="agents"):
        parse_manifest({"agents": {"codex": [{"npx": ["pkg:thing"]}]}})


def test_npx_is_allowed_for_skills() -> None:
    (entry,) = parse_manifest({"skills": {"common": ["npx/pkg:thing"]}})
    assert (entry.source, entry.repo, entry.name) == ("npx", "pkg", "thing")


def test_invalid_selector_error_propagates() -> None:
    with pytest.raises(ValueError, match="Invalid string selector"):
        parse_manifest({"commands": {"common": ["nonsense"]}})


# --- loading ------------------------------------------------------------------------------------


def test_load_manifest_reads_yaml_file(tmp_path: Path) -> None:
    path = tmp_path / "m.yaml"
    path.write_text("skills:\n  common:\n    - personal:a\nhooks:\n  codex:\n    - {k: v}\n")
    entries = load_manifest(path)
    assert [(e.kind, e.name, e.raw) for e in entries] == [
        ("skills", "a", None),
        ("hooks", "", {"k": "v"}),
    ]


def test_load_empty_file_has_no_entries(tmp_path: Path) -> None:
    path = tmp_path / "m.yaml"
    path.write_text("")
    assert load_manifest(path) == []


# --- malformed items and names ------------------------------------------------------------------


@pytest.mark.parametrize(
    "item",
    [{"github.com": [1]}, {"personal": {"a": 1}}, {"npx": [None]}, 42, {"github": [["x"]]}],
)
def test_malformed_item_becomes_a_value_error_naming_kind_section_and_item(item: Any) -> None:
    with pytest.raises(ValueError) as exc:
        parse_manifest({"rules": {"claude": [item]}})
    message = str(exc.value)
    assert "rules" in message
    assert "claude" in message
    assert repr(item) in message


def test_wrapped_error_keeps_the_original_selector_message() -> None:
    with pytest.raises(ValueError) as exc:
        parse_manifest({"skills": {"codex": ["nonsense"]}})
    message = str(exc.value)
    assert "Invalid string selector: nonsense" in message
    assert "skills" in message
    assert "codex" in message


@pytest.mark.parametrize(
    "item",
    [{"personal": {"a": 1}}, "personal:../evil", "personal:a/b", {"personal": ["ok", ""]}],
)
def test_invalid_item_name_is_an_error_naming_kind_section_and_item(item: Any) -> None:
    with pytest.raises(ValueError) as exc:
        parse_manifest({"commands": {"common": [item]}})
    message = str(exc.value)
    assert "commands" in message
    assert "common" in message
    assert repr(item) in message


def test_all_is_a_valid_name() -> None:
    (entry,) = parse_manifest({"agents": {"common": ["github.com:o/r:all"]}})
    assert entry.name == "all"


def test_invalid_github_repo_in_dict_form_names_kind_section_and_item() -> None:
    item = {"github.com": ["bad repo:x"]}
    with pytest.raises(ValueError) as exc:
        parse_manifest({"mcp": {"codex": [item]}})
    assert "mcp" in str(exc.value)
    assert "codex" in str(exc.value)
    assert "Invalid GitHub repo" in str(exc.value)


def test_npx_error_names_section_too() -> None:
    with pytest.raises(ValueError, match="rules.*codex|codex.*rules"):
        parse_manifest({"rules": {"codex": ["npx/pkg:x"]}})
