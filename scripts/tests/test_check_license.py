"""``check_license.py`` against a minimal repo tree built in ``tmp_path``: the
clean tree passes, and every drift (a manifest still on MIT, a surviving
``License ::`` classifier, missing ``license-files``, ``files`` without
``NOTICE``, a ``LICENSE`` or ``NOTICE`` copy that differs, a root ``LICENSE``
with the wrong checksum or with CRLF line endings) fails naming the offending
path, and the real tree keeps the ``.gitattributes`` LF rule the gate relies
on."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_license

REPO_ROOT = SCRIPTS.parent
APACHE_TEXT = (REPO_ROOT / "LICENSE").read_bytes()
NOTICE_TEXT = (
    "Rayito\nCopyright 2026 Rayito contributors\n\n- e2b_charts 1.0.0 (MIT License)\n"
)

CARGO_TOML = '[workspace]\nmembers = ["crates/rayd"]\n\n[workspace.package]\nlicense = "{license}"\npublish = false\n'
PYPROJECT = (
    '[project]\nname = "rayito"\nversion = "0.1.0"\nlicense = "{license}"\n'
    "{license_files}classifiers = [\n{classifiers}]\n"
)
SIDECAR_PYPROJECT = '[project]\nname = "rayito-kernel-sidecar"\nlicense = "{license}"\n'


def write(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="\n")
    return path


def write_pyproject(
    root: Path,
    *,
    license_value: str = "Apache-2.0",
    license_files: str = 'license-files = ["LICENSE", "NOTICE"]\n',
    classifiers: tuple[str, ...] = ("Typing :: Typed",),
) -> None:
    body = "".join(f'    "{classifier}",\n' for classifier in classifiers)
    write(
        root,
        check_license.PYTHON_MANIFEST,
        PYPROJECT.format(
            license=license_value, license_files=license_files, classifiers=body
        ),
    )


def write_package_json(
    root: Path, *, license_value: str = "Apache-2.0", files: list[str] | None = None
) -> None:
    manifest = {
        "name": "rayito",
        "license": license_value,
        "files": ["dist", "README.md", "LICENSE", "NOTICE"] if files is None else files,
    }
    write(
        root, check_license.TYPESCRIPT_MANIFEST, json.dumps(manifest, indent=2) + "\n"
    )


@pytest.fixture
def clean_tree(tmp_path: Path) -> Path:
    write(
        tmp_path, check_license.CARGO_MANIFEST, CARGO_TOML.format(license="Apache-2.0")
    )
    write_pyproject(tmp_path)
    write(
        tmp_path,
        check_license.SIDECAR_MANIFEST,
        SIDECAR_PYPROJECT.format(license="Apache-2.0"),
    )
    write_package_json(tmp_path)
    for relative in (check_license.ROOT_LICENSE, *check_license.LICENSE_COPIES):
        write(tmp_path, relative, APACHE_TEXT)
    for relative in (check_license.ROOT_NOTICE, *check_license.NOTICE_COPIES):
        write(tmp_path, relative, NOTICE_TEXT)
    return tmp_path


def run(root: Path) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = check_license.main(["--root", str(root)])
    return code, buffer.getvalue()


def test_clean_tree_is_ok(clean_tree: Path) -> None:
    assert run(clean_tree) == (0, "OK\n")


def test_cargo_still_mit(clean_tree: Path) -> None:
    write(clean_tree, check_license.CARGO_MANIFEST, CARGO_TOML.format(license="MIT"))
    code, output = run(clean_tree)
    assert code == 1
    assert "KO Cargo.toml: license todavía declara MIT" in output
    assert "KO Cargo.toml: license es 'MIT'" in output


def test_surviving_license_classifier(clean_tree: Path) -> None:
    write_pyproject(
        clean_tree,
        classifiers=("License :: OSI Approved :: MIT License", "Typing :: Typed"),
    )
    code, output = run(clean_tree)
    assert code == 1
    assert output.count("KO ") == 1
    assert "clients/python/pyproject.toml: clasificador de licencia sobrante" in output
    assert "License :: OSI Approved :: MIT License" in output


def test_missing_license_files(clean_tree: Path) -> None:
    write_pyproject(clean_tree, license_files="")
    code, output = run(clean_tree)
    assert code == 1
    assert "KO clients/python/pyproject.toml: license-files es None" in output


def test_package_json_files_without_notice(clean_tree: Path) -> None:
    write_package_json(clean_tree, files=["dist", "README.md", "LICENSE"])
    code, output = run(clean_tree)
    assert code == 1
    assert output == "KO clients/typescript/package.json: files no incluye NOTICE\n"


def test_package_json_still_mit(clean_tree: Path) -> None:
    write_package_json(clean_tree, license_value="MIT")
    code, output = run(clean_tree)
    assert code == 1
    assert "KO clients/typescript/package.json: license todavía declara MIT" in output


def test_sidecar_still_mit(clean_tree: Path) -> None:
    write(
        clean_tree,
        check_license.SIDECAR_MANIFEST,
        SIDECAR_PYPROJECT.format(license="MIT"),
    )
    code, output = run(clean_tree)
    assert code == 1
    assert "KO kernel-sidecar/pyproject.toml: license todavía declara MIT" in output


def test_license_copy_differs_by_one_byte(clean_tree: Path) -> None:
    write(clean_tree, "crates/rayd/LICENSE", APACHE_TEXT[:-1] + b"x")
    code, output = run(clean_tree)
    assert code == 1
    assert output == "KO crates/rayd/LICENSE: no es idéntico byte a byte a LICENSE\n"


def test_notice_copy_differs(clean_tree: Path) -> None:
    write(clean_tree, "clients/typescript/NOTICE", NOTICE_TEXT + "extra\n")
    code, output = run(clean_tree)
    assert code == 1
    assert (
        output == "KO clients/typescript/NOTICE: no es idéntico byte a byte a NOTICE\n"
    )


def test_root_license_wrong_checksum(clean_tree: Path) -> None:
    write(clean_tree, check_license.ROOT_LICENSE, b"MIT License\n")
    code, output = run(clean_tree)
    assert code == 1
    assert "KO LICENSE: sha256 " in output
    assert "no es el texto de apache.org" in output
    for relative in check_license.LICENSE_COPIES:
        assert f"KO {relative}: no es idéntico byte a byte a LICENSE" in output


def test_root_license_with_crlf_names_line_endings(clean_tree: Path) -> None:
    crlf_text = APACHE_TEXT.replace(b"\n", b"\r\n")
    for relative in (check_license.ROOT_LICENSE, *check_license.LICENSE_COPIES):
        write(clean_tree, relative, crlf_text)
    code, output = run(clean_tree)
    assert code == 1
    assert output.count("KO ") == 1
    assert "KO LICENSE: sha256 " in output
    assert "tiene finales CRLF" in output
    assert "CONTRIBUTING.md" in output


def test_lf_license_has_no_line_ending_hint(clean_tree: Path) -> None:
    write(clean_tree, check_license.ROOT_LICENSE, b"MIT License\n")
    _, output = run(clean_tree)
    assert "finales CRLF" not in output


def test_root_notice_without_vendored_mention(clean_tree: Path) -> None:
    for relative in (check_license.ROOT_NOTICE, *check_license.NOTICE_COPIES):
        write(clean_tree, relative, "Rayito\n")
    code, output = run(clean_tree)
    assert code == 1
    assert output == "KO NOTICE: no menciona e2b_charts\n"


def test_missing_manifest_is_named(clean_tree: Path) -> None:
    (clean_tree / check_license.CARGO_MANIFEST).unlink()
    code, output = run(clean_tree)
    assert code == 1
    assert output == "KO Cargo.toml: no existe\n"


def test_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert check_license.main(["--bogus"]) == 2
    assert "uso: check_license.py" in capsys.readouterr().err


def test_real_tree_is_ok() -> None:
    assert run(REPO_ROOT) == (0, "OK\n")


def test_real_tree_forces_lf_line_endings() -> None:
    rules = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8").split()
    assert rules.count("eol=lf") >= 3
    for name in ("*", "LICENSE", "NOTICE"):
        assert name in rules
