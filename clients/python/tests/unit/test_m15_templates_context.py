"""`ContextHasher` (m15-templates): qué ficheros locales entran en el
contexto de build, filtrados por `.dockerignore`, y un hash determinista
sobre su contenido."""

from __future__ import annotations

from pathlib import Path

import pytest

from rayito._templates._context import DockerIgnore, collect_context_files, files_hash
from rayito._templates._instructions import CopyStep
from rayito.exceptions import BuildException


def test_dockerignore_matches_simple_globs_and_negation() -> None:
    ignore = DockerIgnore.from_text("*.pyc\n!keep.pyc\n")
    assert ignore.matches("a.pyc") is True
    assert ignore.matches("keep.pyc") is False
    assert ignore.matches("a.py") is False


def test_collect_context_files_reads_a_single_file_and_a_directory(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print(1)")
    (tmp_path / "app" / "__pycache__").mkdir()
    (tmp_path / "app" / "__pycache__" / "main.pyc").write_bytes(b"\x00")
    (tmp_path / "requirements.txt").write_text("pandas\n")
    (tmp_path / ".dockerignore").write_text("**/__pycache__/*\n__pycache__/*\n")

    entries = collect_context_files(
        tmp_path,
        [CopyStep("app/", "/srv/app/"), CopyStep("requirements.txt", "/srv/requirements.txt")],
    )
    paths = [relpath for relpath, _ in entries]
    assert "app/main.py" in paths
    assert "requirements.txt" in paths
    assert not any("pyc" in path for path in paths)
    assert paths == sorted(paths)


def test_collect_context_files_raises_build_exception_for_a_missing_source(tmp_path: Path) -> None:
    with pytest.raises(BuildException, match="no existe"):
        collect_context_files(tmp_path, [CopyStep("missing/", "/srv/missing/")])


def test_files_hash_is_order_independent_and_content_sensitive() -> None:
    a = [("x.py", b"1"), ("y.py", b"2")]
    b = [("y.py", b"2"), ("x.py", b"1")]
    c = [("x.py", b"1"), ("y.py", b"3")]
    assert files_hash(a) == files_hash(b)
    assert files_hash(a) != files_hash(c)
