"""`rayito.cli._artifact` sin AWS: el zip full no lleva marcador y mantiene
las exclusiones, slim añade sólo el marcador `warmup_variant`, poly sólo
`kernels_variant`, un zip con ambos se rechaza, el zip es idéntico byte a
byte al repetirlo, `copy_tree` aplica las mismas exclusiones y el módulo
sólo importa la biblioteca estándar."""

from __future__ import annotations

import ast
import sys
import zipfile
from pathlib import Path

import pytest

from rayito.cli import _artifact

MARKER = _artifact.WARMUP_MARKER_ENTRY
KERNELS_MARKER = _artifact.KERNELS_MARKER_ENTRY
MODULE_PATH = Path(_artifact.__file__)


@pytest.fixture
def image_dir(tmp_path: Path) -> Path:
    root = tmp_path / "image"
    (root / "kernel-sidecar" / "ipython" / "startup").mkdir(parents=True)
    (root / "kernel-sidecar" / "tests").mkdir()
    (root / "kernel-sidecar" / "__pycache__").mkdir()
    (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / "rayd").write_bytes(b"\x7fELF")
    (root / "kernel-sidecar" / "ipython" / "startup" / "0004_warmup.py").write_text(
        "pass\n", encoding="utf-8"
    )
    (root / "kernel-sidecar" / "tests" / "test_x.py").write_text("", encoding="utf-8")
    (root / "kernel-sidecar" / "__pycache__" / "x.pyc").write_bytes(b"")
    (root / "kernel-sidecar" / "uv.lock").write_text("", encoding="utf-8")
    return root


@pytest.fixture
def sidecar_dir(tmp_path: Path) -> Path:
    root = tmp_path / "sidecar"
    (root / "tests").mkdir(parents=True)
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / "__pycache__").mkdir()
    (root / "requirements.txt").write_text("ipykernel==6\n", encoding="utf-8")
    (root / "requirements-poly.txt").write_text("bash_kernel==0.9\n", encoding="utf-8")
    (root / "sidecar.py").write_text("print()\n", encoding="utf-8")
    (root / "tests" / "test_x.py").write_text("", encoding="utf-8")
    (root / ".venv" / "lib" / "x.py").write_text("", encoding="utf-8")
    (root / "__pycache__" / "x.pyc").write_bytes(b"")
    (root / "uv.lock").write_text("", encoding="utf-8")
    return root


def names(archive: Path) -> list[str]:
    with zipfile.ZipFile(archive) as zf:
        return zf.namelist()


def test_full_zip_has_no_marker_and_keeps_exclusions(image_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "full.zip"
    assert _artifact.write_zip(image_dir, out) == 3
    listed = names(out)
    assert MARKER not in listed
    assert listed == ["Dockerfile", "kernel-sidecar/ipython/startup/0004_warmup.py", "rayd"]
    assert _artifact.marker_variant(out) == "full"
    assert not (image_dir / "kernel-sidecar" / "ipython" / "startup" / "warmup_variant").exists()


def test_slim_zip_adds_only_the_marker(image_dir: Path, tmp_path: Path) -> None:
    full, slim = tmp_path / "full.zip", tmp_path / "slim.zip"
    _artifact.write_zip(image_dir, full)
    assert _artifact.write_zip(image_dir, slim, "slim") == 4
    assert set(names(slim)) - set(names(full)) == {MARKER}
    with zipfile.ZipFile(slim) as zf:
        info = zf.getinfo(MARKER)
        assert zf.read(MARKER) == b"slim\n"
        assert info.date_time == _artifact.FIXED_DATE_TIME
        assert (info.external_attr >> 16) & 0o777 == 0o644
    assert _artifact.marker_variant(slim) == "slim"
    assert full.read_bytes() != slim.read_bytes()


def test_poly_zip_adds_only_the_kernels_marker(image_dir: Path, tmp_path: Path) -> None:
    full, poly = tmp_path / "full.zip", tmp_path / "poly.zip"
    _artifact.write_zip(image_dir, full)
    assert _artifact.write_zip(image_dir, poly, "poly") == 4
    assert set(names(poly)) - set(names(full)) == {KERNELS_MARKER}
    assert MARKER not in names(poly)
    with zipfile.ZipFile(poly) as zf:
        assert zf.read(KERNELS_MARKER) == b"poly\n"
    assert _artifact.marker_variant(poly) == "poly"
    assert not (image_dir / "kernel-sidecar" / "kernels_variant").exists()


def test_marker_variant_for_the_three_archives(image_dir: Path, tmp_path: Path) -> None:
    archives = {variant: tmp_path / f"{variant}.zip" for variant in _artifact.VARIANTS}
    for variant, path in archives.items():
        _artifact.write_zip(image_dir, path, variant)
        assert _artifact.marker_variant(path) == variant
    assert len({path.read_bytes() for path in archives.values()}) == 3


def test_two_markers_are_refused(tmp_path: Path) -> None:
    both = tmp_path / "both.zip"
    with zipfile.ZipFile(both, "w") as zf:
        zf.writestr("Dockerfile", "FROM scratch\n")
        zf.writestr(MARKER, "slim\n")
        zf.writestr(KERNELS_MARKER, "poly\n")
    with pytest.raises(SystemExit, match=r"both\.zip"):
        _artifact.marker_variant(both)


def test_marker_with_unexpected_content_counts_as_full(tmp_path: Path) -> None:
    odd = tmp_path / "odd.zip"
    with zipfile.ZipFile(odd, "w") as zf:
        zf.writestr("Dockerfile", "FROM scratch\n")
        zf.writestr(KERNELS_MARKER, "later\n")
    assert _artifact.marker_variant(odd) == "full"


def test_zip_main_variant_flag(
    image_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "cli.zip"
    assert _artifact.zip_main([str(image_dir), str(out), "--variant", "slim"]) == 0
    assert _artifact.marker_variant(out) == "slim"
    assert _artifact.zip_main([str(image_dir), str(out), "--variant", "poly"]) == 0
    assert _artifact.marker_variant(out) == "poly"
    assert _artifact.zip_main([str(image_dir), str(out)]) == 0
    assert _artifact.marker_variant(out) == "full"
    assert "(variant full)" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        _artifact.zip_main([str(image_dir), str(out), "--variant", "caps"])


def test_missing_dockerfile_is_refused(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit, match="Dockerfile is missing"):
        _artifact.write_zip(tmp_path / "empty", tmp_path / "x.zip")


def test_zip_is_byte_identical_on_rerun(image_dir: Path, tmp_path: Path) -> None:
    first, second = tmp_path / "a.zip", tmp_path / "b.zip"
    _artifact.write_zip(image_dir, first, "slim")
    _artifact.write_zip(image_dir, second, "slim")
    assert first.read_bytes() == second.read_bytes()
    assert _artifact.artifact_sha256(first) == _artifact.artifact_sha256(second)
    assert len(_artifact.artifact_sha256(first)) == 64


def test_copy_tree_applies_the_same_exclusions(sidecar_dir: Path, tmp_path: Path) -> None:
    destination = tmp_path / "image" / "kernel-sidecar"
    destination.mkdir(parents=True)
    (destination / "stale.py").write_text("", encoding="utf-8")
    assert _artifact.copy_tree(sidecar_dir, destination) == 3
    copied = sorted(p.relative_to(destination).as_posix() for p in destination.rglob("*"))
    assert copied == ["requirements-poly.txt", "requirements.txt", "sidecar.py"]


def test_unreadable_entries_inside_excluded_directories_are_never_touched(
    sidecar_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Linux venv copied next to the sidecar carries symlinks Windows cannot
    stat (`WinError 1920`): the exclusion list must win before `is_file()`."""
    broken = sidecar_dir / ".venv" / "bin" / "python"
    broken.parent.mkdir()
    broken.write_text("", encoding="utf-8")
    original_is_file = Path.is_file

    def is_file(self: Path, *args: object, **kwargs: object) -> bool:
        if self == broken:
            raise OSError(1920, "The file cannot be accessed by the system")
        return original_is_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)
    destination = tmp_path / "image" / "kernel-sidecar"
    assert _artifact.copy_tree(sidecar_dir, destination) == 3
    assert [p.name for p in _artifact.shipped_files(sidecar_dir)] == [
        "requirements-poly.txt",
        "requirements.txt",
        "sidecar.py",
    ]


def test_copy_main_requires_both_requirements_files(
    sidecar_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "out"
    assert _artifact.copy_main([str(sidecar_dir), str(destination)]) == 0
    assert "3 files" in capsys.readouterr().out
    (sidecar_dir / "requirements-poly.txt").unlink()
    with pytest.raises(SystemExit, match=r"requirements-poly\.txt is missing"):
        _artifact.copy_main([str(sidecar_dir), str(destination)])
    assert _artifact.copy_main([str(sidecar_dir)]) == 2
    assert "usage" in capsys.readouterr().err


def test_artifact_module_is_stdlib_only() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in sys.stdlib_module_names, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "sin imports relativos"
            assert node.module is not None
            assert node.module.split(".")[0] in sys.stdlib_module_names, node.module
