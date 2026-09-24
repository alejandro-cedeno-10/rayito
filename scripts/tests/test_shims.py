"""The four shims of ``scripts/`` as CI runs them: ``copy_sidecar.py`` and
``image_zip.py`` on an isolated bare interpreter (``python -I -S``: no
site-packages, no ``PYTHONPATH``) over a temporary image tree, the
``marker_variant`` re-export, and ``publish_image.py`` / ``image_prune.py``
with no arguments inside the client environment (typer's usage error, exit
2, no AWS call)."""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
MARKER = "kernel-sidecar/ipython/startup/warmup_variant"


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    (image_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (image_dir / "rayd").write_bytes(b"\x7fELF")
    sidecar = tmp_path / "kernel-sidecar"
    (sidecar / "ipython" / "startup").mkdir(parents=True)
    (sidecar / "tests").mkdir()
    (sidecar / ".venv").mkdir()
    (sidecar / "__pycache__").mkdir()
    (sidecar / "requirements.txt").write_text("ipykernel==6\n", encoding="utf-8")
    (sidecar / "requirements-poly.txt").write_text(
        "bash_kernel==0.9\n", encoding="utf-8"
    )
    (sidecar / "ipython" / "startup" / "0004_warmup.py").write_text(
        "pass\n", encoding="utf-8"
    )
    (sidecar / "tests" / "test_x.py").write_text("", encoding="utf-8")
    (sidecar / ".venv" / "x.py").write_text("", encoding="utf-8")
    (sidecar / "__pycache__" / "x.pyc").write_bytes(b"")
    (sidecar / "uv.lock").write_text("", encoding="utf-8")
    return image_dir, sidecar


def isolated(*args: str) -> subprocess.CompletedProcess[str]:
    """``-I -S``: no ``PYTHONPATH``, no user site and no site-packages at all,
    the bare interpreter the CI ``build`` and release jobs provide."""
    return subprocess.run(
        [sys.executable, "-I", "-S", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def in_client_env(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        env={**os.environ, "_TYPER_FORCE_DISABLE_TERMINAL": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_stdlib_shims_run_isolated(tree: tuple[Path, Path], tmp_path: Path) -> None:
    image_dir, sidecar = tree
    copied = isolated(
        str(SCRIPTS / "copy_sidecar.py"),
        str(sidecar),
        str(image_dir / "kernel-sidecar"),
    )
    assert copied.returncode == 0, copied.stderr
    assert "3 files" in copied.stdout
    names = sorted(
        p.relative_to(image_dir / "kernel-sidecar").as_posix()
        for p in (image_dir / "kernel-sidecar").rglob("*")
        if p.is_file()
    )
    assert names == [
        "ipython/startup/0004_warmup.py",
        "requirements-poly.txt",
        "requirements.txt",
    ]
    destination = tmp_path / "slim.zip"
    zipped = isolated(
        str(SCRIPTS / "image_zip.py"),
        str(image_dir),
        str(destination),
        "--variant",
        "slim",
    )
    assert zipped.returncode == 0, zipped.stderr
    assert "(variant slim)" in zipped.stdout
    with zipfile.ZipFile(destination) as archive:
        entries = archive.namelist()
        assert archive.read(MARKER) == b"slim\n"
    assert not [
        e for e in entries if "__pycache__" in e or "/tests/" in e or ".venv" in e
    ]
    assert "kernel-sidecar/uv.lock" not in entries
    assert "kernel-sidecar/requirements-poly.txt" in entries
    check = isolated(
        "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); from image_zip import marker_variant; "
        "print(marker_variant(sys.argv[2]))",
        str(SCRIPTS),
        str(destination),
    )
    assert check.returncode == 0, check.stderr
    assert check.stdout.strip() == "slim"


def test_stdlib_shims_refuse_bad_input(tree: tuple[Path, Path], tmp_path: Path) -> None:
    image_dir, sidecar = tree
    (sidecar / "requirements-poly.txt").unlink()
    copied = isolated(
        str(SCRIPTS / "copy_sidecar.py"), str(sidecar), str(tmp_path / "out")
    )
    assert (
        copied.returncode == 1 and "requirements-poly.txt is missing" in copied.stderr
    )
    usage = isolated(str(SCRIPTS / "copy_sidecar.py"), str(sidecar))
    assert usage.returncode == 2 and "usage" in usage.stderr
    zipped = isolated(
        str(SCRIPTS / "image_zip.py"),
        str(image_dir),
        str(tmp_path / "x.zip"),
        "--variant",
        "caps",
    )
    assert zipped.returncode == 2


def test_aws_shims_share_the_cli_parser() -> None:
    publish = in_client_env(str(SCRIPTS / "publish_image.py"))
    assert publish.returncode == 2, publish.stderr
    assert "--artifact" in publish.stderr
    partial = in_client_env(str(SCRIPTS / "publish_image.py"), "--artifact", "x.zip")
    assert partial.returncode == 2
    assert "--base-image-version" in partial.stderr
    prune = in_client_env(str(SCRIPTS / "image_prune.py"), "--keep", "0")
    assert prune.returncode == 2, prune.stderr
    assert "--keep" in prune.stderr
    helped = in_client_env(str(SCRIPTS / "image_prune.py"), "--help")
    assert helped.returncode == 0
    assert "--image-name" in helped.stdout and "--dry-run" in helped.stdout


def test_aws_shims_hint_without_the_client_env() -> None:
    publish = isolated(str(SCRIPTS / "publish_image.py"), "--help")
    assert publish.returncode == 2
    assert (
        "uv run --project clients/python python scripts/publish_image.py"
        in publish.stderr
    )
