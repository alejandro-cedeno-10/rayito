"""``check_third_party_licenses.py`` on synthetic inputs: the committed
``THIRD_PARTY_LICENSES.md`` parses, the notices and the ``cargo tree`` graph
must be the same set (workspace crates skipped), every listed crate must be
in the binary's ``.dep-v0`` (a superset), and a listed crate shipping a
``NOTICE`` file fails until the root ``NOTICE`` names it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_third_party_licenses as notices
from test_check_auditable import auditable_elf

REPO_ROOT = SCRIPTS.parent
COMMITTED = REPO_ROOT / "THIRD_PARTY_LICENSES.md"
NOTICES = """# Third-party licenses of rayd

### MIT License (`MIT`)

Used by:

- `tokio 1.53.1` <https://github.com/tokio-rs/tokio>
- `axum 0.8.9`

```text
MIT text
```
"""


def graph(*extra: dict[str, object]) -> list[dict[str, object]]:
    return [
        {"name": "rayd", "version": "0.7.0", "source": "local", "root": True},
        {"name": "rayd-core", "version": "0.7.0", "source": "local"},
        {"name": "tokio", "version": "1.53.1", "source": "crates.io"},
        {"name": "axum", "version": "0.8.9", "source": "crates.io"},
        {"name": "cc", "version": "1.2.0", "source": "crates.io", "kind": "build"},
        *extra,
    ]


def write(tmp_path: Path, name: str, content: str | bytes) -> Path:
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def test_the_committed_file_lists_the_core_crates() -> None:
    listed = {
        name for name, _ in notices.listed_crates(COMMITTED.read_text(encoding="utf-8"))
    }
    assert {"tokio", "tonic", "axum", "nix", "rustls"} <= listed
    assert "rayd" not in listed
    assert "rayd-core" not in listed


TREE = """axum v0.8.9
rayd-core v0.7.0 (/work/crates/rayd-core)
tokio v1.53.1
async-trait v0.1.92 (proc-macro)
tokio v1.53.1 (*)
"""


def test_tree_coverage_passes_and_skips_path_dependencies(tmp_path: Path) -> None:
    md = write(
        tmp_path, "THIRD_PARTY_LICENSES.md", NOTICES + "- `async-trait 0.1.92`\n"
    )
    tree = write(tmp_path, "tree.txt", TREE)
    assert notices.check(md, None, None, tree_path=tree) == 3


def test_a_compiled_crate_missing_from_the_notices_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    md = write(tmp_path, "THIRD_PARTY_LICENSES.md", NOTICES)
    tree = write(tmp_path, "tree.txt", TREE)
    assert notices.main([str(md), "--tree", str(tree)]) == 1
    assert (
        "async-trait 0.1.92: compiled into rayd but not listed"
        in capsys.readouterr().err
    )


def test_a_stale_extra_entry_fails(tmp_path: Path) -> None:
    md = write(tmp_path, "THIRD_PARTY_LICENSES.md", NOTICES + "- `ring 0.17.14`\n")
    tree = write(
        tmp_path, "tree.txt", TREE.replace("async-trait v0.1.92 (proc-macro)\n", "")
    )
    with pytest.raises(
        notices.NoticesError, match=r"ring 0\.17\.14: listed but not compiled"
    ):
        notices.check(md, None, None, tree_path=tree)


def test_a_version_drift_fails(tmp_path: Path) -> None:
    md = write(tmp_path, "THIRD_PARTY_LICENSES.md", NOTICES.replace("1.53.1", "1.52.0"))
    tree = write(tmp_path, "tree.txt", "axum v0.8.9\ntokio v1.53.1\n")
    with pytest.raises(notices.NoticesError, match=r"tokio 1\.53\.1: compiled"):
        notices.check(md, None, None, tree_path=tree)


def test_the_binary_records_every_listed_crate(tmp_path: Path) -> None:
    """``.dep-v0`` is a superset (dev-dependency features unify in ``cargo
    metadata``), so an extra recorded crate such as ``ring`` passes."""
    md = write(tmp_path, "THIRD_PARTY_LICENSES.md", NOTICES)
    extra = {"name": "ring", "version": "0.17.14", "source": "crates.io"}
    binary = write(tmp_path, "rayd", auditable_elf(graph(extra)))
    assert notices.check(md, binary, None) == 2


def test_a_listed_crate_absent_from_the_binary_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    md = write(tmp_path, "THIRD_PARTY_LICENSES.md", NOTICES + "- `serde 1.0.228`\n")
    binary = write(tmp_path, "rayd", auditable_elf(graph()))
    assert notices.main([str(md), "--binary", str(binary)]) == 1
    assert "serde 1.0.228: listed but absent from the .dep-v0 of rayd" in (
        capsys.readouterr().err
    )


def test_an_empty_file_fails(tmp_path: Path) -> None:
    md = write(tmp_path, "THIRD_PARTY_LICENSES.md", "# nothing\n")
    with pytest.raises(notices.NoticesError, match="lists no crate"):
        notices.check(md, None, None)


def metadata_with_notice(tmp_path: Path) -> Path:
    crate_dir = tmp_path / "registry" / "axum-0.8.9"
    crate_dir.mkdir(parents=True)
    (crate_dir / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    (crate_dir / "NOTICE").write_text("axum notice\n", encoding="utf-8")
    plain_dir = tmp_path / "registry" / "tokio-1.53.1"
    plain_dir.mkdir(parents=True)
    (plain_dir / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    metadata = {
        "packages": [
            {
                "name": "axum",
                "version": "0.8.9",
                "manifest_path": str(crate_dir / "Cargo.toml"),
            },
            {
                "name": "tokio",
                "version": "1.53.1",
                "manifest_path": str(plain_dir / "Cargo.toml"),
            },
        ]
    }
    return write(tmp_path, "metadata.json", json.dumps(metadata))


def test_a_crate_notice_file_must_be_carried_in_the_root_notice(tmp_path: Path) -> None:
    md = write(tmp_path, "THIRD_PARTY_LICENSES.md", NOTICES)
    metadata = metadata_with_notice(tmp_path)
    bare = write(tmp_path, "NOTICE", "Rayito\nmentions axum-extra only\n")
    with pytest.raises(notices.NoticesError, match=r"axum 0\.8\.9 ships NOTICE"):
        notices.check(md, None, metadata, bare)
    carried = write(tmp_path, "NOTICE", "Rayito\n- axum 0.8.9: axum notice\n")
    assert notices.check(md, None, metadata, carried) == 2
