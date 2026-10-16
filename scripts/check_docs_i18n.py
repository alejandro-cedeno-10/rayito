"""Comprueba que el sitio de docs es bilingüe: cada página en español tiene su pareja en inglés.

El sitio usa `mkdocs-static-i18n` con el diseño por sufijo: `pagina.md` es el
español (idioma por defecto, URLs sin cambio) y `pagina.en.md` es el inglés.
Reglas, todas sin red y sólo con la biblioteca estándar:

1. Toda página `X.md` de `docs/site/docs` tiene `X.en.md`, salvo las de
   `PENDING_TRANSLATION`: la lista de lo que aún no se tradujo, que sólo
   puede encoger. Una página de esa lista que ya tiene su pareja es un error
   (hay que quitarla de la lista), y una página nueva sin pareja que no está
   en la lista también.
2. No hay `X.en.md` sin su `X.md`: el español manda y el inglés lo sigue.
3. Las dos versiones llevan el mismo número de bloques de código vallados, de
   modo que un ejemplo añadido o quitado en un idioma no se queda sin su
   reflejo en el otro (`check_docs_examples.py` compila los de las dos).
4. Los `{{macros}}` de `limits.json` que cita una versión los cita la otra.
5. Las anclas del español existen también en el inglés: cada encabezado
   inglés lleva el id del español (`## Costs { #costes }`), de modo que un
   enlace `pagina.md#ancla` (también desde las páginas aún sin traducir, que
   en `/en/` se sirven en español) y el selector de idioma funcionan igual en
   los dos idiomas.

    python scripts/check_docs_i18n.py
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
DOCS_DIR: Final = REPO_ROOT / "docs" / "site" / "docs"
ENGLISH_SUFFIX: Final = ".en.md"
SPANISH_SUFFIX: Final = ".md"

PENDING_TRANSLATION: Final[frozenset[str]] = frozenset()
"""Páginas en español (ruta relativa a `docs/site/docs`) sin traducir todavía.

Mientras una página esté aquí, `/en/<ruta>/` sirve el español (el
`fallback_to_default` de `mkdocs-static-i18n`). Se quita de la lista en el
mismo cambio que añade su `.en.md`."""

FENCE: Final = re.compile(r"^\s*(`{3,}|~{3,})", re.MULTILINE)
LIMIT_MACRO: Final = re.compile(r"\{\{([a-z][A-Za-z0-9]*)\}\}")


HEADING: Final = re.compile(r"^(#{1,6})[ \t]+(?P<text>.+?)[ \t]*#*[ \t]*$")
EXPLICIT_ID: Final = re.compile(r"\{[^}]*#(?P<id>[A-Za-z0-9_:.-]+)[^}]*\}\s*$")
ATTRS: Final = re.compile(r"\s*\{[^}]*\}\s*$")
LINK: Final = re.compile(r"\[([^\]]*)\]\([^)]*\)")


MIN_ANCHOR_LEVEL: Final = 2
"""El `# título` de página no se enlaza con ancla: sólo cuentan los `##` y siguientes."""


def slugify(text: str) -> str:
    """El `slugify` por defecto de `markdown.extensions.toc` (el del sitio)."""
    plain = LINK.sub(r"\1", ATTRS.sub("", text)).replace("`", "").replace("*", "")
    ascii_text = unicodedata.normalize("NFKD", plain).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^\w\s-]", "", ascii_text).strip().lower()
    return re.sub(r"[-\s]+", "-", cleaned)


def anchors(text: str) -> list[str]:
    """Los ids de los encabezados `##`+ de una página, en orden, fuera de los bloques de código."""
    found: list[str] = []
    seen: dict[str, int] = {}
    in_fence = False
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        heading = None if in_fence else HEADING.match(line)
        if not heading or len(heading[1]) < MIN_ANCHOR_LEVEL:
            continue
        explicit = EXPLICIT_ID.search(heading["text"])
        base = explicit["id"] if explicit else slugify(heading["text"])
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.append(base if count == 0 else f"{base}_{count}")
    return found


def spanish_pages(docs_dir: Path = DOCS_DIR) -> dict[str, Path]:
    return {
        path.relative_to(docs_dir).as_posix(): path
        for path in sorted(docs_dir.rglob("*.md"))
        if not path.name.endswith(ENGLISH_SUFFIX)
    }


def english_twin(page: str) -> str:
    return page.removesuffix(SPANISH_SUFFIX) + ENGLISH_SUFFIX


def fence_count(text: str) -> int:
    return len(FENCE.findall(text)) // 2


def check(docs_dir: Path = DOCS_DIR, pending: frozenset[str] = PENDING_TRANSLATION) -> list[str]:
    problems: list[str] = []
    spanish = spanish_pages(docs_dir)
    for page in spanish:
        twin = docs_dir / english_twin(page)
        if not twin.is_file() and page not in pending:
            problems.append(f"{page}: falta {english_twin(page)} (o añádela a PENDING_TRANSLATION)")
        if twin.is_file() and page in pending:
            problems.append(
                f"{page}: ya tiene {english_twin(page)}; quítala de PENDING_TRANSLATION"
            )
    for page in sorted(pending - spanish.keys()):
        problems.append(f"{page}: está en PENDING_TRANSLATION pero la página no existe")
    for path in sorted(docs_dir.rglob("*" + ENGLISH_SUFFIX)):
        english = path.relative_to(docs_dir).as_posix()
        page = english.removesuffix(ENGLISH_SUFFIX) + SPANISH_SUFFIX
        if page not in spanish:
            problems.append(f"{english}: no hay {page} (el español manda)")
            continue
        problems.extend(_parity_problems(page, spanish[page], path))
    return problems


def _parity_problems(page: str, spanish: Path, english: Path) -> list[str]:
    es_text = spanish.read_text(encoding="utf-8")
    en_text = english.read_text(encoding="utf-8")
    problems = []
    if fence_count(es_text) != fence_count(en_text):
        problems.append(
            f"{page}: {fence_count(es_text)} bloques de código en español y "
            f"{fence_count(en_text)} en {english.name}"
        )
    es_macros, en_macros = set(LIMIT_MACRO.findall(es_text)), set(LIMIT_MACRO.findall(en_text))
    if es_macros != en_macros:
        problems.append(
            f"{page}: macros de limits.json distintas entre idiomas "
            f"({sorted(es_macros ^ en_macros)})"
        )
    missing = [a for a in anchors(es_text) if a not in set(anchors(en_text))]
    if missing:
        problems.append(
            f"{page}: faltan en {english.name} las anclas del español "
            f"(añade `{{ #ancla }}` al encabezado): {', '.join(missing)}"
        )
    return problems


def main() -> int:
    problems = check()
    for problem in problems:
        print(f"check_docs_i18n: {problem}", file=sys.stderr)
    if not problems:
        pages = len(spanish_pages())
        print(
            f"check_docs_i18n: {pages - len(PENDING_TRANSLATION)}/{pages} páginas con pareja en inglés"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
