"""The README and the sample manifest must stay valid manifests."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from agent_sync.manifest import load_manifest, parse_manifest

ROOT = Path(__file__).resolve().parent.parent
FENCE = re.compile(r"^```ya?ml\n(.*?)^```", re.DOTALL | re.MULTILINE)


def readme_manifests() -> list[str]:
    """Every yaml code block in the README that has a supported top-level kind."""
    blocks = FENCE.findall((ROOT / "README.md").read_text(encoding="utf-8"))
    found = []
    for block in blocks:
        data = yaml.safe_load(block)
        if isinstance(data, dict) and any(kind in data for kind in ("skills", "agents", "commands", "rules")):
            found.append(block)
    return found


def test_the_readme_has_manifest_examples() -> None:
    assert len(readme_manifests()) >= 2  # the basic and the expanded form


def test_every_manifest_example_in_the_readme_parses() -> None:
    for block in readme_manifests():
        assert parse_manifest(yaml.safe_load(block)), block


def test_the_sample_config_parses() -> None:
    assert load_manifest(ROOT / "config.yaml")
