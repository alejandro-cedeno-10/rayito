"""Hook de MkDocs: quita del `<head>` los `<link rel="alternate" hreflang>`.

Material trata cada `<link rel="alternate">` como la raíz de un sitio o de una
versión y, al cargar la página, pide `sitemap.xml` junto a cada una para
resolver la misma página al cambiar. `mkdocs-static-i18n` los rellena con la
URL de la página en cada idioma, así que esas peticiones iban a
`/<página>/sitemap.xml` y `/en/<página>/sitemap.xml` y devolvían 404 (el
plugin sólo escribe el `sitemap.xml` raíz). El selector de idioma no los
necesita: sus enlaces salen de `extra.alternate` y ya apuntan a la misma
página. Los `hreflang` para buscadores siguen en `sitemap.xml`, que lista las
dos lenguas de cada página, que pasa a ser la única fuente de `hreflang`.

`404.html` es una plantilla estática y no pasa por `on_post_page`, así que
también se limpia en `on_post_template`. `on_post_build` falla la
construcción si algún HTML conserva un alternate con `hreflang` (por ejemplo,
si una versión de Material cambia el orden de los atributos y la expresión
deja de coincidir).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

HREFLANG_ALTERNATE: Final = re.compile(
    r'[ \t]*<link rel="alternate" href="[^"]*" hreflang="[^"]*">\n?'
)
REMAINING_HREFLANG_ALTERNATE: Final = re.compile(
    r'<link[^>]*rel="alternate"[^>]*hreflang=|<link[^>]*hreflang=[^>]*rel="alternate"'
)


def on_post_page(output: str, **_: Any) -> str:
    return HREFLANG_ALTERNATE.sub("", output)


def on_post_template(output_content: str, **_: Any) -> str:
    return HREFLANG_ALTERNATE.sub("", output_content)


def on_post_build(config: Any, **_: Any) -> None:
    leftovers = [
        str(path.relative_to(config["site_dir"]))
        for path in Path(config["site_dir"]).rglob("*.html")
        if REMAINING_HREFLANG_ALTERNATE.search(path.read_text(encoding="utf-8"))
    ]
    if leftovers:
        raise RuntimeError(
            'Quedan <link rel="alternate" hreflang> en el HTML construido '
            f"(Material cambió el formato?): {sorted(leftovers)[:5]}"
        )
