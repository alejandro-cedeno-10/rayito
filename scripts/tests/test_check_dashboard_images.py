"""``check_dashboard_images.py`` sobre PNG fabricados: un PNG limpio pasa y
cada defecto (chunk de texto, EXIF, fecha, otro formato, exceso de peso o PNG
truncado) es un hallazgo; las capturas reales del repositorio están limpias."""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_dashboard_images as images

PIXEL = b"\x00\x00\x00\x00"


def chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload))
    )


def png(*extra: tuple[bytes, bytes]) -> bytes:
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    pixels = chunk(b"IDAT", zlib.compress(PIXEL))
    return (
        images.PNG_SIGNATURE
        + header
        + b"".join(chunk(k, p) for k, p in extra)
        + pixels
        + chunk(b"IEND", b"")
    )


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_a_clean_png_passes(tmp_path: Path) -> None:
    assert images.check_image(write(tmp_path, "ok.png", png())) == []


def test_text_exif_and_time_chunks_are_findings(tmp_path: Path) -> None:
    for kind in (b"tEXt", b"iTXt", b"zTXt", b"eXIf", b"tIME"):
        problems = images.check_image(write(tmp_path, "meta.png", png((kind, b"x"))))
        assert len(problems) == 1
        assert kind.decode() in problems[0]


def test_an_image_over_the_size_limit_is_a_finding(tmp_path: Path) -> None:
    heavy = png((b"PLTE", b"\x00" * images.MAX_IMAGE_BYTES))
    problems = images.check_image(write(tmp_path, "heavy.png", heavy))
    assert any("pesa" in problem for problem in problems)


def test_other_formats_and_truncated_pngs_are_findings(tmp_path: Path) -> None:
    jpeg = write(tmp_path, "photo.jpg", b"\xff\xd8\xff\xe1Exif\x00\x00")
    truncated = write(tmp_path, "cut.png", png()[:-8])
    assert any("PNG" in problem for problem in images.check_image(jpeg))
    assert any("PNG" in problem for problem in images.check_image(truncated))


def test_check_directory_reports_every_bad_file(tmp_path: Path) -> None:
    write(tmp_path, "ok.png", png())
    write(tmp_path, "bad.png", png((b"tEXt", b"Software\x00x")))
    assert len(images.check_directory(tmp_path)) == 1


def test_the_committed_panel_screenshots_are_clean() -> None:
    assert images.PANEL_IMAGES_DIR.is_dir()
    assert list(images.PANEL_IMAGES_DIR.glob("*.png"))
    assert images.check_directory() == []
