"""``prepare_release_pr.py`` over crafted text: the release-please block is
dropped and ``## [Unreleased]`` becomes the dated section with repeated
``###`` headings merged, the transform is idempotent, and ``Cargo.lock``
gets the version on exactly the three workspace crates. ``port_release_commit``
runs against a throwaway git repository: a release-please commit hanging from
an older ``main`` is ported onto the current one without reverting it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import prepare_release_pr as prp

CHANGELOG = """# Changelog

Intro.

## [0.4.0](https://example.invalid/compare/a...b) (2026-10-01)


### Fixed

* generated note ([abc](https://example.invalid))

## [Unreleased]

### Fixed

- Primer arreglo.

### Changed

- Un cambio.

### Fixed

- Segundo arreglo.

## [0.3.2] - 2026-09-29

### Fixed

- Viejo.
"""


def test_changelog_becomes_a_dated_section_with_merged_headings() -> None:
    out = prp.normalize_changelog(CHANGELOG, "0.4.0", "2026-10-01")
    assert "generated note" not in out
    assert "(https://example.invalid/compare" not in out
    head, _, rest = out.partition("## [Unreleased]\n")
    assert head.endswith("Intro.\n\n")
    section, _, older = rest.partition("## [0.3.2] - 2026-09-29")
    assert section.startswith("\n## [0.4.0] - 2026-10-01\n")
    assert section.count("### Fixed") == 1
    assert (
        section.index("Primer arreglo.")
        < section.index("Segundo arreglo.")
        < section.index("### Changed")
    )
    assert "- Viejo." in older
    assert "\n\n\n" not in out


def test_changelog_transform_is_idempotent() -> None:
    once = prp.normalize_changelog(CHANGELOG, "0.4.0", "2026-10-01")
    assert prp.normalize_changelog(once, "0.4.0", "2026-10-01") == once


def test_changelog_without_unreleased_is_rejected() -> None:
    with pytest.raises(ValueError):
        prp.normalize_changelog(
            "# Changelog\n\n## [0.3.2] - 2026-09-29\n", "0.4.0", "2026-10-01"
        )


def test_cargo_lock_bumps_only_the_workspace_crates() -> None:
    lock = "".join(
        f'[[package]]\nname = "{name}"\nversion = "0.3.2"\n\n'
        for name in (*prp.WORKSPACE_CRATES, "tokio")
    )
    out = prp.bump_cargo_lock(lock, "0.4.0")
    for name in prp.WORKSPACE_CRATES:
        assert f'name = "{name}"\nversion = "0.4.0"' in out
    assert 'name = "tokio"\nversion = "0.3.2"' in out


def test_cargo_lock_missing_crate_is_an_error() -> None:
    with pytest.raises(ValueError):
        prp.bump_cargo_lock('[[package]]\nname = "rayd"\nversion = "0.3.2"\n', "0.4.0")


def test_compat_row_is_required_for_the_release_series() -> None:
    source = (prp.Path(prp.__file__).resolve().parents[1] / prp.COMPAT_TABLE).read_text(
        encoding="utf-8"
    )
    assert prp.has_compat_row(source, "0.3.3")
    assert prp.has_compat_row(source, "0.4.0")
    assert not prp.has_compat_row(source, "9.9.0")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def test_release_commit_on_a_stale_base_is_ported_onto_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "t"),
        ("user.email", "t@example.invalid"),
        ("commit.gpgsign", "false"),
    ):
        _git(repo, "config", key, value)
    changelog = prp.CHANGELOGS[0]
    unreleased = "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- Uno.\n"
    _commit(repo, {"Cargo.toml": 'version = "0.1.0"\n', changelog: unreleased}, "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "release")
    _commit(
        repo,
        {
            "Cargo.toml": 'version = "0.1.1"\n',
            changelog: unreleased.replace(
                "## [Unreleased]",
                "## [0.1.1](https://example.invalid) (x)\n\n## [Unreleased]",
            ),
        },
        "chore: release main",
    )
    _git(repo, "switch", "-q", "main")
    _commit(
        repo,
        {"later.txt": "merged after\n", changelog: unreleased + "- Dos.\n"},
        "merged after release-please",
    )
    main = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "merge-base", "main", "release") == base

    monkeypatch.chdir(repo)
    prp.port_release_commit("main", "release")

    assert _git(repo, "rev-parse", "HEAD") == main
    assert _git(repo, "branch", "--show-current") == prp.RELEASE_BRANCH
    assert (repo / "later.txt").exists()
    assert (repo / "Cargo.toml").read_text(encoding="utf-8") == 'version = "0.1.1"\n'
    assert (repo / changelog).read_text(encoding="utf-8") == unreleased + "- Dos.\n"
    assert _git(repo, "diff", "--cached", "--name-only") == "Cargo.toml"
