"""SEO del sitio: la portada y las páginas clave llevan `description` de 50 a 160
caracteres en español e inglés, y la portada su `seo_title` de hasta 60."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs" / "site" / "docs"
KEY_PAGES = (
    "index",
    "primeros-pasos/index",
    "primeros-pasos/instalacion",
    "primeros-pasos/configurar-aws",
    "quickstart",
    "migrar-desde-e2b/index",
    "cost",
    "pool",
    "guias/agente-en-el-sandbox",
)
MIN_DESCRIPTION = 50
MAX_DESCRIPTION = 160
MAX_SEO_TITLE = 60


def front_matter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    block = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert block, path
    for line in re.findall(r"^description: .*$", block.group(1), re.MULTILINE):
        assert line.startswith('description: "') and line.endswith('"'), (
            f"{path}: entrecomilla la description (lleva dos puntos)"
        )
    pairs = re.findall(r"^(\w+): (.+)$", block.group(1), re.MULTILINE)
    return {key: value.strip().strip('"') for key, value in pairs}


@pytest.mark.parametrize("suffix", ["", ".en"])
@pytest.mark.parametrize("page", KEY_PAGES)
def test_key_pages_have_a_description_of_the_right_length(
    page: str, suffix: str
) -> None:
    description = front_matter(DOCS / f"{page}{suffix}.md").get("description", "")
    assert MIN_DESCRIPTION <= len(description) <= MAX_DESCRIPTION, (
        page,
        suffix,
        len(description),
    )


@pytest.mark.parametrize("suffix", ["", ".en"])
def test_home_has_a_short_seo_title(suffix: str) -> None:
    title = front_matter(DOCS / f"index{suffix}.md").get("seo_title", "")
    assert title and len(title) <= MAX_SEO_TITLE and title != "Rayito - Rayito"
