"""Checks of the test harness itself: isolation guarantees every other test relies on."""

from __future__ import annotations

import pytest
from conftest import Sandbox


def test_the_developers_cache_variable_does_not_leak_into_the_tool_environment(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKILL_SYNC_CACHE", "/should/not/be/used")

    fresh = Sandbox(sandbox.root / "second")

    assert "SKILL_SYNC_CACHE" not in fresh.env
    assert fresh.env["HOME"] == str(fresh.home)
    assert list(fresh.home.iterdir()) == []
