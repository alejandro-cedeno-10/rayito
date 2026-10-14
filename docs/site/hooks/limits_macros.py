"""Hook de MkDocs: sustituye `{{nombre}}` por el valor de `limits.json`.

Las páginas citan los límites por el nombre de `limits.json` (la fuente única,
de la que se generan `_limits.py` y `limits.ts`) en lugar de repetir el número.
Un nombre que no existe rompe el build (`strict`): un error tipográfico no
llega al sitio como un `{{nombre}}` literal.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

from mkdocs.exceptions import PluginError

LIMITS_PATH: Final = Path(__file__).resolve().parents[3] / "limits.json"
MACRO: Final = re.compile(r"\{\{([a-z][A-Za-z0-9]*)\}\}")
LIMITS: Final[dict[str, Any]] = json.loads(LIMITS_PATH.read_text(encoding="utf-8"))


def _value(match: re.Match[str]) -> str:
    name = match.group(1)
    if name not in LIMITS:
        raise PluginError(f"limits.json no tiene «{name}» (macro {{{{{name}}}}})")
    return str(LIMITS[name])


def on_page_markdown(markdown: str, **_: Any) -> str:
    return MACRO.sub(_value, markdown)
