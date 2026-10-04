"""``prepare_release_pr.py``.

Pure transforms over crafted text (the release-please block is dropped and
``## [Unreleased]`` becomes the dated section with repeated ``###`` headings
merged, idempotently; compare links in the footer move to the new tag;
version updaters derived from ``release-please-config.json``; trailers), and
the whole flow against throwaway git repositories: a release-please branch
cut before newer merges to ``main`` must not revert them, the bumps must
match release-please's own, and the scope guard aborts before anything
reaches the remote."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import prepare_release_pr as prp

REPO_ROOT = SCRIPTS.parent
OLD, NEW, DATE = "0.6.0", "0.6.1", "2026-10-03"
BASE_URL = "https://example.invalid/repo"

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


# --- CHANGELOG ---------------------------------------------------------------


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


FOOTER = (
    f"[Unreleased]: {BASE_URL}/compare/python-v0.6.0...HEAD\n"
    f"[0.6.0]: {BASE_URL}/compare/python-v0.5.1...python-v0.6.0\n"
)


def test_link_refs_compare_the_previous_tag_with_the_new_one() -> None:
    out = prp.update_link_refs(
        "# C\n\n## [Unreleased]\n\n" + FOOTER, NEW, "python-v0.6.1"
    )
    assert out.endswith(
        f"[Unreleased]: {BASE_URL}/compare/python-v0.6.1...HEAD\n"
        f"[0.6.1]: {BASE_URL}/compare/python-v0.6.0...python-v0.6.1\n"
        f"[0.6.0]: {BASE_URL}/compare/python-v0.5.1...python-v0.6.0\n"
    )
    assert prp.update_link_refs(out, NEW, "python-v0.6.1") == out


def test_link_refs_leave_a_changelog_without_footer_alone() -> None:
    assert prp.update_link_refs(CHANGELOG, NEW, "python-v0.6.1") == CHANGELOG


# --- plan y actualizadores ---------------------------------------------------


def test_plan_is_derived_from_the_repository_config() -> None:
    config = json.loads((REPO_ROOT / prp.CONFIG).read_text(encoding="utf-8"))
    plan = prp.release_plan(config, REPO_ROOT)
    assert {entry.path for entry in plan.version_files} == {
        "clients/python/pyproject.toml",
        "clients/python/src/rayito/_version.py",
        "clients/typescript/package.json",
        "clients/typescript/src/version.ts",
        "Cargo.toml",
        "Cargo.lock",
    }
    assert plan.allowed_paths == {entry.path for entry in plan.version_files} | {
        prp.MANIFEST,
        "clients/python/uv.lock",
        "clients/python/CHANGELOG.md",
        "clients/typescript/CHANGELOG.md",
        "crates/rayd/CHANGELOG.md",
    }
    assert plan.tag("rayd", NEW) == "rayd-v0.6.1"


def test_every_repository_version_file_bumps() -> None:
    config = json.loads((REPO_ROOT / prp.CONFIG).read_text(encoding="utf-8"))
    for entry in prp.release_plan(config, REPO_ROOT).version_files:
        text = (REPO_ROOT / entry.path).read_text(encoding="utf-8")
        assert prp.bump_version_file(text, entry, "9.8.7") != text, entry


def test_cargo_lock_bumps_only_the_named_crate() -> None:
    lock = "".join(
        f'[[package]]\nname = "{name}"\nversion = "0.3.2"\n\n'
        for name in ("rayd", "tokio")
    )
    entry = prp.VersionFile(
        "Cargo.lock", "toml", "$.package[?(@.name=='rayd')].version"
    )
    out = prp.bump_version_file(lock, entry, "0.4.0")
    assert 'name = "rayd"\nversion = "0.4.0"' in out
    assert 'name = "tokio"\nversion = "0.3.2"' in out


def test_missing_version_entry_is_an_error() -> None:
    entry = prp.VersionFile(
        "Cargo.lock", "toml", "$.package[?(@.name=='rayd-core')].version"
    )
    with pytest.raises(prp.ReleaseError):
        prp.bump_version_file(
            '[[package]]\nname = "rayd"\nversion = "0.3.2"\n', entry, "0.4.0"
        )


def test_toml_table_key_only_touches_its_table() -> None:
    text = '[package]\nversion = "1.0.0"\n\n[workspace.package]\nedition = "2024"\nversion = "0.6.0"\n'
    entry = prp.VersionFile("Cargo.toml", "toml", "$.workspace.package.version")
    out = prp.bump_version_file(text, entry, NEW)
    assert out == text.replace('version = "0.6.0"', 'version = "0.6.1"')


def test_unsupported_updater_is_an_error() -> None:
    with pytest.raises(prp.ReleaseError):
        prp.bump_version_file("x", prp.VersionFile("a.yaml", "yaml"), NEW)


def test_compat_row_is_required_for_the_release_series() -> None:
    source = (REPO_ROOT / prp.COMPAT_TABLE).read_text(encoding="utf-8")
    assert prp.has_compat_row(source, "0.3.3")
    assert prp.has_compat_row(source, "0.4.0")
    assert not prp.has_compat_row(source, "9.9.0")


# --- trailers ----------------------------------------------------------------


def test_trailers_come_from_the_cli_and_the_environment() -> None:
    trailers = prp.parse_trailers(
        ["Co-Authored-By: A <a@example.invalid>"],
        "Claude-Session: https://example.invalid/s\n\nCo-Authored-By: A <a@example.invalid>\n",
    )
    assert trailers == [
        "Co-Authored-By: A <a@example.invalid>",
        "Claude-Session: https://example.invalid/s",
    ]
    message = prp.commit_message(NEW, trailers)
    assert message.endswith("\n\n" + "\n".join(trailers))


def test_malformed_trailer_is_rejected() -> None:
    with pytest.raises(prp.ReleaseError):
        prp.parse_trailers(["not a trailer"], "")


# --- flujo completo sobre repositorios git de usar y tirar --------------------

CONFIG = {
    "include-component-in-tag": True,
    "include-v-in-tag": True,
    "tag-separator": "-",
    "packages": {
        "clients/python": {
            "component": "python",
            "release-type": "python",
            "extra-files": [{"type": "generic", "path": "src/pkg/_version.py"}],
        },
        "clients/typescript": {
            "component": "typescript",
            "release-type": "node",
            "extra-files": [{"type": "generic", "path": "src/version.ts"}],
        },
        "crates/rayd": {
            "component": "rayd",
            "release-type": "simple",
            "extra-files": [
                {
                    "type": "toml",
                    "path": "/Cargo.toml",
                    "jsonpath": "$.workspace.package.version",
                },
                {
                    "type": "toml",
                    "path": "/Cargo.lock",
                    "jsonpath": "$.package[?(@.name=='rayd')].version",
                },
            ],
        },
    },
}
COMPONENTS = {
    "clients/python": "python",
    "clients/typescript": "typescript",
    "crates/rayd": "rayd",
}


def _changelog(component: str, note: str) -> str:
    return (
        f"# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- {note}\n\n"
        f"## [{OLD}] - 2026-10-01\n\n- Viejo.\n\n"
        f"[Unreleased]: {BASE_URL}/compare/{component}-v{OLD}...HEAD\n"
        f"[{OLD}]: {BASE_URL}/compare/{component}-v0.5.1...{component}-v{OLD}\n"
    )


def _files(version: str) -> dict[str, str]:
    files = {
        prp.CONFIG: json.dumps(CONFIG, indent=2) + "\n",
        prp.MANIFEST: json.dumps(dict.fromkeys(COMPONENTS, version), indent=2) + "\n",
        prp.COMPAT_TABLE: 'COMPATIBILITY = (\n    CompatibilityRow(\n        "0.6",\n    ),\n)\n',
        "clients/python/pyproject.toml": f'[project]\nname = "pkg"\nversion = "{version}"\n',
        "clients/python/src/pkg/_version.py": f'__version__ = "{version}"  # x-release-please-version\n',
        "clients/python/uv.lock": f'[[package]]\nname = "pkg"\nversion = "{version}"\n',
        "clients/typescript/package.json": f'{{\n  "name": "pkg",\n  "version": "{version}"\n}}\n',
        "clients/typescript/src/version.ts": f'export const VERSION = "{version}"; // x-release-please-version\n',
        "Cargo.toml": f'[workspace.package]\nversion = "{version}"\n',
        "Cargo.lock": f'[[package]]\nname = "rayd"\nversion = "{version}"\n',
        "README.md": "Rayito.\n",
    }
    for path, component in COMPONENTS.items():
        files[f"{path}/CHANGELOG.md"] = _changelog(component, "Arreglo del ciclo.")
    return files


def _run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _commit(root: Path, message: str) -> None:
    _run(root, "add", "-A")
    _run(root, "commit", "--quiet", "-m", message)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Un `origin` con `main` en OLD y la rama de release-please cortada de
    ese `main`; después `main` recibe un merge nuevo (rama atrasada). Devuelve
    un clon limpio en `main`, que pasa a ser el directorio actual."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = Test\n\temail = test@example.invalid\n"
        "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv(prp.TRAILERS_ENV, raising=False)
    monkeypatch.setattr(prp, "COMMIT_FLAGS", ("-s",))
    monkeypatch.setattr(prp, "refresh_lockfile", _fake_uv_lock)

    origin, seed, work = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "work"
    _run(tmp_path, "init", "--quiet", "--bare", str(origin))
    _run(tmp_path, "init", "--quiet", str(seed))
    _write(seed, _files(OLD))
    _commit(seed, f"chore: release {OLD}")
    _run(seed, "remote", "add", "origin", str(origin))
    _run(seed, "push", "--quiet", "origin", "HEAD:main")

    # release-please: versiones y su bloque generado, sobre el main de entonces.
    release = {k: v for k, v in _files(NEW).items() if not k.endswith("CHANGELOG.md")}
    del release["clients/python/uv.lock"]
    _write(seed, release)
    _commit(seed, "chore: release main")
    _run(seed, "push", "--quiet", "origin", f"HEAD:{prp.RELEASE_BRANCH}")

    # main avanza después: un merge que la rama de release-please no tiene.
    _run(seed, "reset", "--quiet", "--hard", "HEAD~1")
    _write(
        seed,
        {
            "openspec/changes/archive/2026-10-01-x/proposal.md": "Archivado.\n",
            "README.md": "Rayito, con más docs.\n",
            "clients/python/CHANGELOG.md": _changelog(
                "python", "Arreglo del ciclo."
            ).replace(
                "- Arreglo del ciclo.\n",
                "- Arreglo del ciclo.\n- Arreglo fusionado después.\n",
            ),
        },
    )
    _commit(seed, "feat: newer merge")
    _run(seed, "push", "--quiet", "origin", "HEAD:main")

    _run(tmp_path, "clone", "--quiet", str(origin), str(work))
    monkeypatch.chdir(work)
    return work


def _fake_uv_lock(package: prp.Package) -> None:
    lock = Path(package.path) / prp.UV_LOCK
    version = json.loads(Path(prp.MANIFEST).read_text(encoding="utf-8"))[package.path]
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(OLD, version), encoding="utf-8"
    )


def _remote_release(work: Path) -> str:
    _run(work, "fetch", "--quiet", "origin")
    return _run(work, "rev-parse", f"origin/{prp.RELEASE_BRANCH}")


def test_stale_release_branch_keeps_everything_merged_to_main(repo: Path) -> None:
    trailers = [
        "Co-Authored-By: A <a@example.invalid>",
        "Claude-Session: https://example.invalid/s",
    ]
    assert (
        prp.main(["--date", DATE, "--trailer", trailers[0], "--trailer", trailers[1]])
        == 0
    )

    _run(repo, "fetch", "--quiet", "origin")
    release = f"origin/{prp.RELEASE_BRANCH}"
    assert _run(repo, "rev-parse", f"{release}^") == _run(
        repo, "rev-parse", "origin/main"
    )
    changed = set(
        _run(repo, "diff", "--name-only", "origin/main", release).splitlines()
    )
    assert changed == set(_files(NEW)) - {prp.CONFIG, prp.COMPAT_TABLE, "README.md"}

    show = lambda path: _run(repo, "show", f"{release}:{path}")
    assert show("openspec/changes/archive/2026-10-01-x/proposal.md") == "Archivado."
    assert show("README.md") == "Rayito, con más docs."
    for path, text in _files(NEW).items():
        if (
            path not in (prp.CONFIG, prp.COMPAT_TABLE, "README.md")
            and "CHANGELOG" not in path
        ):
            assert show(path) == text.strip(), path

    python_changelog = show("clients/python/CHANGELOG.md")
    unreleased, _, rest = python_changelog.partition(f"## [{NEW}] - {DATE}")
    assert unreleased.endswith("## [Unreleased]\n\n")
    assert "- Arreglo fusionado después." in rest.split(f"## [{OLD}]")[0]
    assert f"[Unreleased]: {BASE_URL}/compare/python-v{NEW}...HEAD" in python_changelog
    assert (
        f"[{NEW}]: {BASE_URL}/compare/python-v{OLD}...python-v{NEW}" in python_changelog
    )
    assert f"[{NEW}]: {BASE_URL}/compare/rayd-v{OLD}...rayd-v{NEW}" in show(
        "crates/rayd/CHANGELOG.md"
    )

    message = _run(repo, "log", "-1", "--format=%B", release)
    assert message.startswith("chore: release main\n")
    assert message.endswith(
        "\n".join([*trailers, "Signed-off-by: Test <test@example.invalid>"])
    )


def test_trailers_also_come_from_the_environment(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(prp.TRAILERS_ENV, "Co-Authored-By: B <b@example.invalid>")
    assert prp.main(["--date", DATE]) == 0
    _run(repo, "fetch", "--quiet", "origin")
    message = _run(repo, "log", "-1", "--format=%B", f"origin/{prp.RELEASE_BRANCH}")
    assert "\nCo-Authored-By: B <b@example.invalid>\nSigned-off-by: " in message


def test_guard_aborts_on_files_outside_the_release(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _remote_release(repo)

    def lock_and_stray(package: prp.Package) -> None:
        _fake_uv_lock(package)
        Path("README.md").write_text("Cambio colado.\n", encoding="utf-8")
        Path("stray.txt").write_text("nuevo\n", encoding="utf-8")

    monkeypatch.setattr(prp, "refresh_lockfile", lock_and_stray)
    assert prp.main(["--date", DATE]) == prp.EXIT_ABORT
    err = capsys.readouterr().err
    assert "README.md, stray.txt" in err
    assert "No se ha commiteado ni subido nada" in err
    assert _remote_release(repo) == before
    assert _run(repo, "rev-parse", "HEAD") == _run(repo, "rev-parse", "origin/main")


def test_release_please_touching_an_unknown_file_aborts(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(repo, "switch", "--quiet", "-c", "rp", f"origin/{prp.RELEASE_BRANCH}")
    _write(repo, {"crates/rayd/version.yaml": f"version: {NEW}\n"})
    _commit(repo, "chore: release main")
    _run(repo, "push", "--quiet", "--force", "origin", f"HEAD:{prp.RELEASE_BRANCH}")
    _run(repo, "switch", "--quiet", "main")
    before = _remote_release(repo)

    assert prp.main(["--date", DATE]) == prp.EXIT_ABORT
    assert "crates/rayd/version.yaml" in capsys.readouterr().err
    assert _remote_release(repo) == before


def test_dirty_tree_aborts(repo: Path) -> None:
    Path("README.md").write_text("sin commit\n", encoding="utf-8")
    assert prp.main(["--date", DATE]) == prp.EXIT_ABORT


def test_dry_run_leaves_the_release_uncommitted(repo: Path) -> None:
    before = _remote_release(repo)
    assert prp.main(["--date", DATE, "--dry-run"]) == 0
    assert _run(repo, "rev-parse", "HEAD") == _run(repo, "rev-parse", "origin/main")
    assert _run(repo, "branch", "--show-current") == prp.RELEASE_BRANCH
    assert f'version = "{NEW}"' in Path("Cargo.toml").read_text(encoding="utf-8")
    assert _remote_release(repo) == before


def test_a_bump_that_disagrees_with_release_please_aborts(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(repo, "switch", "--quiet", "-c", "rp", f"origin/{prp.RELEASE_BRANCH}")
    _write(
        repo,
        {"Cargo.toml": f'[workspace.package]\nversion = "{NEW}"\nedition = "2024"\n'},
    )
    _commit(repo, "chore: release main")
    _run(repo, "push", "--quiet", "--force", "origin", f"HEAD:{prp.RELEASE_BRANCH}")
    _run(repo, "switch", "--quiet", "main")
    before = _remote_release(repo)

    assert prp.main(["--date", DATE]) == prp.EXIT_ABORT
    err = capsys.readouterr().err
    assert "no coincide" in err
    assert "Cargo.toml" in err
    assert _remote_release(repo) == before
