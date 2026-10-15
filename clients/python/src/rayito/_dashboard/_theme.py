"""Temas del panel: la paleta, los radios, las fuentes y los espacios salen de
variables CSS (tokens de diseño), y un tema de usuario es un fichero pequeño
con los valores de esos tokens.

Un tema se valida antes de servirse y no puede inyectar CSS ni scripts:

- Las claves son una lista cerrada (`COLOR_TOKENS`, `LENGTH_TOKENS` y
  `FONT_TOKENS`); una clave desconocida se rechaza.
- Cada valor debe cumplir la gramática de su clase: color (`#rgb`,
  `#rrggbb`, `#rrggbbaa`, `rgb()`/`hsl()` numéricos), longitud (número con
  `px`, `rem` o `em`) o lista de familias tipográficas (nombres sueltos o
  entre comillas, sin `url()` ni `@font-face`: no hay fuentes remotas).
- El CSS lo genera este módulo desde los valores ya validados: el texto del
  fichero nunca se copia tal cual.

Dominio puro, sin ficheros ni sockets: la cáscara local lee los ficheros y
la de la fase 2 recibirá el mismo texto por un parámetro de la pila o de SSM,
y las dos llaman a `parse_theme` y `parse_logo`.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from rayito._limits import DASHBOARD_LOGO_MAX_BYTES, DASHBOARD_THEME_MAX_BYTES
from rayito.exceptions import InvalidArgumentException

THEME_FORMAT_VERSION: Final = 1
BUILTIN_THEME_NAME: Final = "rayito"
CUSTOM_THEME_NAME: Final = "custom"
THEME_NAME_MAX_CHARS: Final = 40
SHOWN_KEY_MAX_CHARS: Final = 40

#: Los colores que cambian entre el esquema claro y el oscuro.
COLOR_TOKENS: Final = (
    "bg",
    "surface",
    "surface-raised",
    "border",
    "border-strong",
    "fg",
    "fg-muted",
    "primary",
    "on-primary",
    "accent",
    "accent-strong",
    "focus",
    "ok",
    "warning",
    "critical",
    "info",
    "chart-1",
    "chart-2",
    "chart-3",
)
#: Radios y espacios: iguales en los dos esquemas.
LENGTH_TOKENS: Final = (
    "radius",
    "radius-lg",
    "space-1",
    "space-2",
    "space-3",
    "space-4",
    "space-6",
    "sidebar-width",
)
FONT_TOKENS: Final = ("font-sans", "font-mono")
ALL_TOKENS: Final = (*COLOR_TOKENS, *LENGTH_TOKENS, *FONT_TOKENS)

KIND_COLOR: Final = "color"
KIND_LENGTH: Final = "length"
KIND_FONT: Final = "font"
TOKEN_KINDS: Final[dict[str, str]] = {
    **dict.fromkeys(COLOR_TOKENS, KIND_COLOR),
    **dict.fromkeys(LENGTH_TOKENS, KIND_LENGTH),
    **dict.fromkeys(FONT_TOKENS, KIND_FONT),
}
SCHEMES: Final = ("dark", "light")
SECTION_SHARED: Final = "tokens"
JSON_KEYS: Final = frozenset({"version", "name", SECTION_SHARED, *SCHEMES})

_NUMBER = r"\d{1,3}(?:\.\d{1,3})?"
_CHANNEL_SEPARATOR = r"(?:\s*,\s*|\s+)"
_VALUE_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    KIND_COLOR: re.compile(
        r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})"
        rf"|(?:rgb|hsl)a?\(\s*{_NUMBER}(?:deg|%)?{_CHANNEL_SEPARATOR}{_NUMBER}%?"
        rf"{_CHANNEL_SEPARATOR}{_NUMBER}%?(?:\s*[,/]\s*(?:{_NUMBER}%?|0?\.\d{{1,3}}))?\s*\)"
    ),
    KIND_LENGTH: re.compile(rf"0|{_NUMBER}(?:px|rem|em)"),
    KIND_FONT: re.compile(
        r"(?:\"[A-Za-z0-9 _-]{1,40}\"|[A-Za-z0-9_-]{1,40})"
        r"(?:\s*,\s*(?:\"[A-Za-z0-9 _-]{1,40}\"|[A-Za-z0-9_-]{1,40})){0,7}"
    ),
}
_NAME_PATTERN: Final = re.compile(rf"[A-Za-z0-9][A-Za-z0-9 _.-]{{0,{THEME_NAME_MAX_CHARS - 1}}}")
_KEY_PATTERN: Final = re.compile(rf"[A-Za-z0-9_-]{{1,{SHOWN_KEY_MAX_CHARS}}}")

_CSS_COMMENT: Final = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_BLOCK: Final = re.compile(r"([^{}]+)\{([^{}]*)\}")
_CSS_DECLARATION: Final = re.compile(r"--([A-Za-z0-9-]+)\s*:\s*([^;{}]+)")
_CSS_SECTIONS: Final[dict[str, str]] = {
    ':root[data-theme="dark"]': "dark",
    ':root[data-theme="light"]': "light",
    ":root": SECTION_SHARED,
}

PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
CONTENT_TYPE_PNG: Final = "image/png"
CONTENT_TYPE_SVG: Final = "image/svg+xml"
#: Elementos de un SVG que ejecutan código, incrustan otro documento o animan
#: atributos (un `<animate>` puede poner un `javascript:` en un `href`).
SVG_FORBIDDEN_TAGS: Final = frozenset(
    {
        "script",
        "foreignobject",
        "iframe",
        "object",
        "embed",
        "audio",
        "video",
        "animate",
        "animatetransform",
        "animatemotion",
        "set",
    }
)
SVG_ROOT_TAG: Final = "svg"
_SVG_DOCTYPE: Final = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_SVG_EXTERNAL_URL: Final = re.compile(r"url\(\s*['\"]?\s*(?!#)", re.IGNORECASE)
_SVG_IMPORT: Final = re.compile(r"@import", re.IGNORECASE)
_SVG_SCRIPT_URL: Final = re.compile(r"(?:java|vb)script\s*:|data\s*:", re.IGNORECASE)


def _shown(key: object) -> str:
    """La clave para el mensaje de error, sólo si es inocua."""
    if isinstance(key, str) and _KEY_PATTERN.fullmatch(key):
        return key
    return "(clave no válida)"


def _validated(section: str, values: object, allowed: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise InvalidArgumentException(f"tema: «{section}» debe ser un objeto de clave y valor")
    checked: dict[str, str] = {}
    for key, value in values.items():
        if key not in allowed:
            raise InvalidArgumentException(f"tema: clave desconocida en «{section}»: {_shown(key)}")
        pattern = _VALUE_PATTERNS[TOKEN_KINDS[key]]
        if not isinstance(value, str) or not pattern.fullmatch(value.strip()):
            raise InvalidArgumentException(
                f"tema: valor no válido para {key} (se espera {TOKEN_KINDS[key]})"
            )
        checked[key] = value.strip()
    return checked


def _declarations(values: Mapping[str, str], allowed: tuple[str, ...]) -> list[str]:
    return [f"  --{key}: {values[key]};" for key in allowed if key in values]


@dataclass(frozen=True)
class Branding:
    """Lo que la SPA sabe del tema por `/api/v1/meta`: el nombre y si hay logo."""

    name: str = BUILTIN_THEME_NAME
    logo: bool = False


@dataclass(frozen=True)
class Theme:
    """Los valores de tokens ya validados. `shared` (radios, espacios,
    fuentes y los colores de los dos esquemas) y un conjunto de colores por
    esquema, que gana sobre `shared`."""

    name: str = CUSTOM_THEME_NAME
    shared: Mapping[str, str] = field(default_factory=dict)
    dark: Mapping[str, str] = field(default_factory=dict)
    light: Mapping[str, str] = field(default_factory=dict)

    def css(self) -> str:
        """La hoja de estilos que se sirve en `/theme.css`: los mismos
        selectores que los temas de fábrica (`tokens.css`), para ganar por
        orden y no por especificidad. El esquema oscuro es el de base, el
        claro entra con `data-theme="light"` o, en automático, con el sistema."""
        blocks: list[str] = []
        for scheme, selector, colors in (
            ("dark", ':root, :root[data-theme="dark"]', self.dark),
            ("light", ':root[data-theme="light"]', self.light),
        ):
            body = _declarations({**self.shared, **colors}, ALL_TOKENS)
            if body:
                blocks.append(f"/* {scheme} */\n{selector} {{\n" + "\n".join(body) + "\n}")
                if scheme == "light":
                    blocks.append(
                        "@media (prefers-color-scheme: light) {\n"
                        '  :root:not([data-theme="dark"]) {\n'
                        + "\n".join(f"  {line}" for line in body)
                        + "\n  }\n}"
                    )
        return f"/* Tema «{self.name}» (generado) */\n" + "\n".join(blocks) + "\n"


def parse_theme(text: str, *, fmt: str) -> Theme:
    """Valida el texto de un tema (`fmt` es `json` o `css`) y lo devuelve.
    Lanza `InvalidArgumentException` con un mensaje que nombra la clave o la
    sección, nunca el valor."""
    if len(text.encode()) > DASHBOARD_THEME_MAX_BYTES:
        raise InvalidArgumentException(f"tema: pesa más de {DASHBOARD_THEME_MAX_BYTES} bytes")
    if fmt == "json":
        document = _load_json(text)
    elif fmt == "css":
        document = _load_css(text)
    else:
        raise InvalidArgumentException("tema: el formato debe ser .json o .css")
    return _theme_of(document)


def _load_json(text: str) -> Mapping[str, object]:
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        raise InvalidArgumentException("tema: el JSON no es válido") from None
    if not isinstance(document, dict):
        raise InvalidArgumentException("tema: la raíz debe ser un objeto")
    return document


def _load_css(text: str) -> Mapping[str, object]:
    """Un CSS de tema es sólo bloques `:root`, `:root[data-theme="dark"]` y
    `:root[data-theme="light"]` con declaraciones `--token: valor;`. Cualquier
    otra cosa (reglas `@`, selectores, `url()`) se rechaza."""
    stripped = _CSS_COMMENT.sub("", text)
    document: dict[str, object] = {"version": THEME_FORMAT_VERSION}
    consumed = 0
    for block in _CSS_BLOCK.finditer(stripped):
        if stripped[consumed : block.start()].strip():
            raise InvalidArgumentException("tema: el CSS sólo admite bloques :root")
        consumed = block.end()
        section = _CSS_SECTIONS.get(" ".join(block.group(1).split()))
        if section is None:
            raise InvalidArgumentException("tema: selector CSS no admitido")
        document[section] = _css_values(block.group(2))
    if stripped[consumed:].strip():
        raise InvalidArgumentException("tema: el CSS sólo admite bloques :root")
    return document


def _css_values(body: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for declaration in body.split(";"):
        if not declaration.strip():
            continue
        found = _CSS_DECLARATION.fullmatch(declaration.strip())
        if found is None:
            raise InvalidArgumentException("tema: declaración CSS no admitida")
        values[found.group(1)] = found.group(2).strip()
    return values


def _theme_of(document: Mapping[str, object]) -> Theme:
    for key in document:
        if key not in JSON_KEYS:
            raise InvalidArgumentException(f"tema: clave desconocida: {_shown(key)}")
    if document.get("version") != THEME_FORMAT_VERSION:
        raise InvalidArgumentException(f"tema: «version» debe ser {THEME_FORMAT_VERSION}")
    name = document.get("name", CUSTOM_THEME_NAME)
    if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
        raise InvalidArgumentException(
            f"tema: «name» admite letras, números, espacio, punto, guion y guion bajo "
            f"(hasta {THEME_NAME_MAX_CHARS})"
        )
    return Theme(
        name=name,
        shared=_validated(SECTION_SHARED, document.get(SECTION_SHARED, {}), ALL_TOKENS),
        dark=_validated("dark", document.get("dark", {}), COLOR_TOKENS),
        light=_validated("light", document.get("light", {}), COLOR_TOKENS),
    )


@dataclass(frozen=True)
class Logo:
    """Un logo ya validado: PNG o SVG sin código ni referencias externas."""

    content_type: str
    data: bytes


def parse_logo(data: bytes) -> Logo:
    """Valida el logo por su contenido (no por la extensión): un PNG por su
    firma, un SVG por su estructura. Lanza `InvalidArgumentException`."""
    if len(data) > DASHBOARD_LOGO_MAX_BYTES:
        raise InvalidArgumentException(f"logo: pesa más de {DASHBOARD_LOGO_MAX_BYTES} bytes")
    if data.startswith(PNG_SIGNATURE):
        return Logo(CONTENT_TYPE_PNG, data)
    _check_svg(data)
    return Logo(CONTENT_TYPE_SVG, data)


def _check_svg(data: bytes) -> None:
    """Rechaza (no sanea) lo que un SVG podría hacer fuera de dibujar: DTD y
    entidades, scripts, `foreignObject`, animaciones, manejadores `on*`,
    `javascript:`/`data:` y cualquier `href` o `url()` que no sea un
    fragmento local `#id`. Se sirve además con una CSP que no ejecuta nada."""
    if _SVG_DOCTYPE.search(data):
        raise InvalidArgumentException("logo: el SVG no puede llevar DOCTYPE ni entidades")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise InvalidArgumentException("logo: no es un PNG ni un SVG válido") from None
    if _local_name(root.tag) != SVG_ROOT_TAG:
        raise InvalidArgumentException("logo: no es un PNG ni un SVG válido")
    for element in root.iter():
        _check_svg_element(element)


def _local_name(name: str) -> str:
    return name.rpartition("}")[2].lower()


def _check_svg_element(element: ET.Element) -> None:
    tag = _local_name(element.tag)
    if tag in SVG_FORBIDDEN_TAGS:
        raise InvalidArgumentException(f"logo: el SVG no admite el elemento {tag}")
    if tag == "style" and _unsafe_style(element.text or ""):
        raise InvalidArgumentException("logo: el SVG no admite estilos con recursos externos")
    for name, value in element.attrib.items():
        attribute = _local_name(name)
        if attribute.startswith("on"):
            raise InvalidArgumentException("logo: el SVG no admite manejadores de eventos")
        if attribute == "href" and not value.startswith("#"):
            raise InvalidArgumentException("logo: el SVG no admite referencias externas")
        if _SVG_SCRIPT_URL.search(value) or _unsafe_style(value):
            raise InvalidArgumentException("logo: el SVG no admite referencias externas")


def _unsafe_style(text: str) -> bool:
    return bool(_SVG_EXTERNAL_URL.search(text) or _SVG_IMPORT.search(text))


@dataclass(frozen=True)
class Appearance:
    """El aspecto que sirve una cáscara: el tema de usuario y el logo, ambos
    opcionales. Sin ellos, valen los temas de fábrica."""

    theme: Theme | None = None
    logo: Logo | None = None

    @property
    def branding(self) -> Branding:
        return Branding(
            name=self.theme.name if self.theme else BUILTIN_THEME_NAME,
            logo=self.logo is not None,
        )

    @property
    def css(self) -> str:
        return self.theme.css() if self.theme else ""
