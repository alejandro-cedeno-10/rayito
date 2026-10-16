"""Hook de MkDocs: quita del `<head>` los `<link rel="alternate" hreflang>`.

Material trata cada `<link rel="alternate">` como la raíz de un sitio o de una
versión y, al cargar la página, pide `sitemap.xml` junto a cada una para
resolver la misma página al cambiar. `mkdocs-static-i18n` los rellena con la
URL de la página en cada idioma, así que esas peticiones iban a
`/<página>/sitemap.xml` y `/en/<página>/sitemap.xml` y devolvían 404 (el
plugin sólo escribe el `sitemap.xml` raíz). El selector de idioma no los
necesita: sus enlaces salen de `extra.alternate` y ya apuntan a la misma
página. Los `hreflang` para buscadores siguen en `sitemap.xml`, que lista las
dos lenguas de cada página.
"""

from __future__ import annotations

import re
from typing import Any, Final

HREFLANG_ALTERNATE: Final = re.compile(
    r'[ \t]*<link rel="alternate" href="[^"]*" hreflang="[^"]*">\n?'
)


def on_post_page(output: str, **_: Any) -> str:
    return HREFLANG_ALTERNATE.sub("", output)
