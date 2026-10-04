"""``.github/dependabot.yml`` parsed as data: every ``updates`` entry waits
``cooldown.default-days`` (≥ 7) before proposing a version published on the
registry, and ≥ 14 for a semver major where the ecosystem has one. A version
published hours earlier is the window in which hijacked releases do their
damage (the 2025 npm worms and PyPI account takeovers were pulled within
days); once merged, the new code runs in every job on ``main``. Security
updates bypass the cooldown (GitHub documents it), so a published advisory
still gets its PR at once.

The hash-pinned tool requirements under ``.github/release/`` (twine,
pip-audit, cfn-lint) are watched by a ``pip`` entry too, so a CVE in one of
their pins opens a PR like any other dependency (``sec-supply-chain-followups``,
SC-A12); the ``github-actions`` entry also reads the local composite actions,
whose ``uses:`` pin third-party actions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
MIN_DEFAULT_DAYS = 7
MIN_MAJOR_DAYS = 14
SEMVER_ECOSYSTEMS = frozenset({"cargo", "uv", "npm", "pip"})
TOOL_REQUIREMENTS_DIRECTORY = "/.github/release"
LOCAL_ACTIONS_DIRECTORY = "/.github/actions/*"


def updates() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))[
        "updates"
    ]
    return entries


def directories(entry: dict[str, Any]) -> list[str]:
    if "directories" in entry:
        listed: list[str] = entry["directories"]
        return listed
    return [entry["directory"]]


def test_every_ecosystem_waits_before_proposing_a_fresh_release() -> None:
    entries = updates()

    assert entries
    for entry in entries:
        cooldown = entry.get("cooldown") or {}
        label = f"{entry['package-ecosystem']} {directories(entry)}"
        assert cooldown.get("default-days", 0) >= MIN_DEFAULT_DAYS, label
        if entry["package-ecosystem"] in SEMVER_ECOSYSTEMS:
            assert cooldown.get("semver-major-days", 0) >= MIN_MAJOR_DAYS, label


def test_the_hash_pinned_tool_requirements_are_watched() -> None:
    watched = [
        entry
        for entry in updates()
        if entry["package-ecosystem"] == "pip"
        and TOOL_REQUIREMENTS_DIRECTORY in directories(entry)
    ]

    assert len(watched) == 1


def test_the_local_composite_actions_are_watched() -> None:
    actions = [
        entry for entry in updates() if entry["package-ecosystem"] == "github-actions"
    ]

    assert len(actions) == 1
    assert {"/", LOCAL_ACTIONS_DIRECTORY} <= set(directories(actions[0]))
