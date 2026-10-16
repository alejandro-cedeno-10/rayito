"""Ilustración de la portada: las capas HTML/CSS existen en las dos lenguas, no queda
vídeo ni GIF en el sitio y el peso de las imágenes cabe en el presupuesto.

`mkdocs --strict` no valida las rutas de HTML en bruto, así que se comprueban aquí.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "site" / "docs"
ART = DOCS / "assets" / "rayito" / "art"
PAGES = {"es": DOCS / "index.md", "en": DOCS / "index.en.md"}
DESKTOP_BUDGET_BYTES = 400 * 1024
MOBILE_BUDGET_BYTES = 260 * 1024
SOCIAL_CARD_MAX_BYTES = 200 * 1024
PLATE_MOBILE = "plate-960.webp"
PLATE_DESKTOP = "plate-1672.webp"

SRC = re.compile(r'\b(?:src|srcset)="([^"]+)"')
ART_PATH = re.compile(r"((?:\.\./)?assets/rayito/art/[\w.-]+)")


def art_files(language: str) -> set[str]:
    refs: set[str] = set()
    for value in SRC.findall(PAGES[language].read_text(encoding="utf-8")):
        refs.update(m.rsplit("/", 1)[1] for m in ART_PATH.findall(value))
    return refs


@pytest.mark.parametrize("language", ["es", "en"])
def test_every_referenced_layer_exists(language: str) -> None:
    text = PAGES[language].read_text(encoding="utf-8")
    prefix = "../" if language == "en" else ""
    paths = ART_PATH.findall(text)
    assert paths, "la portada no referencia ninguna capa"
    for path in paths:
        assert path.startswith(prefix + "assets/") and (
            not prefix or path.startswith("../")
        ), path
        assert (DOCS / path.removeprefix("../")).is_file(), path


def test_both_languages_use_the_same_files() -> None:
    assert art_files("es") == art_files("en")


@pytest.mark.parametrize("language", ["es", "en"])
def test_no_video_or_gif_on_the_site(language: str) -> None:
    text = PAGES[language].read_text(encoding="utf-8")
    assert "<video" not in text
    assert ".mp4" not in text
    assert ".gif" not in text
    stray = [
        p.name for p in (DOCS / "assets").rglob("*") if p.suffix in {".mp4", ".webm"}
    ]
    assert stray == []


def test_desktop_art_fits_the_budget() -> None:
    total = sum(p.stat().st_size for p in ART.glob("*.webp") if p.name != PLATE_MOBILE)
    assert total <= DESKTOP_BUDGET_BYTES, total


def test_mobile_art_fits_the_budget() -> None:
    total = sum(p.stat().st_size for p in ART.glob("*.webp") if p.name != PLATE_DESKTOP)
    assert total <= MOBILE_BUDGET_BYTES, total


def test_social_card_is_a_small_jpeg() -> None:
    card = DOCS / "assets" / "rayito" / "og-card.jpg"
    assert card.read_bytes()[:3] == b"\xff\xd8\xff"
    assert card.stat().st_size <= SOCIAL_CARD_MAX_BYTES


@pytest.mark.parametrize("language", ["es", "en"])
def test_layers_are_lazy_and_sized(language: str) -> None:
    text = PAGES[language].read_text(encoding="utf-8")
    images = re.findall(r"<img [^>]*assets/rayito/art/[^>]*>", text)
    assert len(images) == 17
    for image in images:
        assert 'loading="lazy"' in image and "width=" in image and "height=" in image, (
            image
        )
