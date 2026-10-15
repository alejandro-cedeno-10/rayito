"""La SPA empaquetada (`_dashboard/_static/`) más el aspecto, compartidos por
las dos cáscaras: el servidor local la sirve del wheel y la Lambda de la pila
`dashboard` del zip. Sólo se sirve lo que hay bajo `_static/` y el tema
validado, listado al arrancar: ninguna ruta de la petición toca el sistema de
ficheros.
"""

from __future__ import annotations

import importlib.resources
from typing import Any, Final

from rayito._dashboard._theme import Appearance

STATIC_PACKAGE: Final = "rayito._dashboard"
STATIC_DIR: Final = "_static"
INDEX_PATH: Final = "/"
INDEX_FILE: Final = "index.html"
THEME_CSS_PATH: Final = "/theme.css"
LOGO_PATH: Final = "/theme/logo"
CSS_CONTENT_TYPE: Final = "text/css; charset=utf-8"
CONTENT_TYPES: Final[dict[str, str]] = {
    ".html": "text/html; charset=utf-8",
    ".css": CSS_CONTENT_TYPE,
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


def appearance_files(appearance: Appearance | None = None) -> dict[str, tuple[bytes, str]]:
    """Lo que aporta el aspecto: `/theme.css`, que existe siempre (vacío sin
    tema de usuario) para que `index.html` lo enlace sin 404, y el logo si lo
    hay."""
    look = appearance or Appearance()
    files = {THEME_CSS_PATH: (look.css.encode(), CSS_CONTENT_TYPE)}
    if look.logo is not None:
        files[LOGO_PATH] = (look.logo.data, look.logo.content_type)
    return files


def load_static_files(appearance: Appearance | None = None) -> dict[str, tuple[bytes, str]]:
    """La SPA y el aspecto: ruta URL → (bytes, tipo)."""
    root = importlib.resources.files(STATIC_PACKAGE) / STATIC_DIR
    files: dict[str, tuple[bytes, str]] = {}

    def walk(directory: Any, prefix: str) -> None:
        for entry in directory.iterdir():
            path = f"{prefix}{entry.name}"
            if entry.is_dir():
                walk(entry, f"{path}/")
                continue
            suffix = "." + entry.name.rsplit(".", 1)[-1]
            content_type = CONTENT_TYPES.get(suffix)
            if content_type is not None:
                files[f"/{path}"] = (entry.read_bytes(), content_type)

    walk(root, "")
    if f"/{INDEX_FILE}" in files:
        files[INDEX_PATH] = files[f"/{INDEX_FILE}"]
    files.update(appearance_files(appearance))
    return files
