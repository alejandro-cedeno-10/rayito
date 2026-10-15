"""Temas y logo del panel: se aceptan los válidos y se rechazan, sin copiar
el valor al mensaje, las claves desconocidas y cualquier intento de inyectar
CSS, scripts o recursos remotos. El CSS que se sirve sale sólo de valores
validados."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rayito._dashboard._theme import (
    ALL_TOKENS,
    COLOR_TOKENS,
    FONT_TOKENS,
    LENGTH_TOKENS,
    PNG_SIGNATURE,
    Appearance,
    parse_logo,
    parse_theme,
)
from rayito._limits import DASHBOARD_LOGO_MAX_BYTES, DASHBOARD_THEME_MAX_BYTES
from rayito.exceptions import InvalidArgumentException

EXAMPLES = Path(__file__).resolve().parents[4] / "examples" / "dashboard-theme"
INJECTION_VALUES = (
    "red; } body { display: none",
    "url(https://example.com/x.png)",
    "#fff; background: url(x)",
    "expression(alert(1))",
    "var(--bg)",
    "rgb(0, 0, 0) !important",
    "</style><script>alert(1)</script>",
    "javascript:alert(1)",
    "#12",
    "#ggg",
    "10",
    "1e3px",
    "calc(1px + 2px)",
)
FONT_INJECTIONS = (
    "Arial; } body { color: red",
    "url(https://example.com/font.woff2)",
    '"Arial", url(x)',
    "Arial\\",
    "a, b, c, d, e, f, g, h, i",
)


def theme_json(**sections: Any) -> str:
    return json.dumps({"version": 1, **sections})


def test_the_example_themes_are_valid_and_equivalent() -> None:
    from_json = parse_theme((EXAMPLES / "aurora.json").read_text(encoding="utf-8"), fmt="json")
    from_css = parse_theme((EXAMPLES / "aurora.css").read_text(encoding="utf-8"), fmt="css")
    assert from_json.name == "aurora"
    assert (from_json.shared, from_json.dark, from_json.light) == (
        from_css.shared,
        from_css.dark,
        from_css.light,
    )
    assert set(from_json.dark) == set(COLOR_TOKENS) == set(from_json.light)


def test_the_example_logo_is_valid() -> None:
    assert parse_logo((EXAMPLES / "aurora-logo.svg").read_bytes()).content_type == "image/svg+xml"


@pytest.mark.parametrize(
    "color",
    [
        "#fff",
        "#ffff",
        "#110d1c",
        "#110d1c80",
        "rgb(1, 2, 3)",
        "rgba(1 2 3 / 0.5)",
        "hsl(200, 50%, 40%)",
    ],
)
def test_it_accepts_colors_lengths_and_fonts(color: str) -> None:
    theme = parse_theme(
        theme_json(
            tokens={"radius": "0.5rem", "space-2": "8px", "font-mono": '"Fira Code", monospace'},
            dark={"bg": color},
        ),
        fmt="json",
    )
    assert theme.dark["bg"] == color
    assert theme.shared["radius"] == "0.5rem"


def test_it_rejects_unknown_keys_at_every_level() -> None:
    for document in (
        {"version": 1, "extra": 1},
        {"version": 1, "tokens": {"not-a-token": "#fff"}},
        {"version": 1, "dark": {"radius": "1px"}},
        {"version": 1, "light": {"font-sans": "Arial"}},
    ):
        with pytest.raises(InvalidArgumentException):
            parse_theme(json.dumps(document), fmt="json")


@pytest.mark.parametrize("value", INJECTION_VALUES)
def test_it_rejects_injection_in_colors_and_lengths(value: str) -> None:
    for token in ("bg", "radius"):
        with pytest.raises(InvalidArgumentException) as raised:
            parse_theme(theme_json(tokens={token: value}), fmt="json")
        assert value not in str(raised.value)


@pytest.mark.parametrize("value", FONT_INJECTIONS)
def test_it_rejects_injection_in_fonts(value: str) -> None:
    with pytest.raises(InvalidArgumentException):
        parse_theme(theme_json(tokens={"font-sans": value}), fmt="json")


def test_an_unknown_key_with_markup_is_not_echoed() -> None:
    with pytest.raises(InvalidArgumentException) as raised:
        parse_theme(theme_json(tokens={"</style><script>": "#fff"}), fmt="json")
    assert "script" not in str(raised.value)


@pytest.mark.parametrize(
    "text",
    ["", "[]", "not json", '{"version": 2}', '{"name": "x"}', '{"version": 1, "name": "<b>"}'],
)
def test_it_rejects_malformed_documents(text: str) -> None:
    with pytest.raises(InvalidArgumentException):
        parse_theme(text, fmt="json")


def test_it_rejects_a_file_over_the_size_cap_and_unknown_formats() -> None:
    with pytest.raises(InvalidArgumentException):
        parse_theme(" " * (DASHBOARD_THEME_MAX_BYTES + 1), fmt="json")
    with pytest.raises(InvalidArgumentException):
        parse_theme("{}", fmt="yaml")


@pytest.mark.parametrize(
    "css",
    [
        "@import url(https://example.com/x.css);",
        "body { color: red }",
        ":root { --bg: url(x); }",
        ":root { --bg: #fff; } a { x: y }",
        ":root { background: red; }",
        ":root[data-theme='x'] { --bg: #fff; }",
        ":root { --bg: #fff } .x",
        ":root { --unknown: #fff; }",
    ],
)
def test_it_rejects_css_that_is_not_just_token_blocks(css: str) -> None:
    with pytest.raises(InvalidArgumentException):
        parse_theme(css, fmt="css")


def test_the_generated_css_only_contains_validated_declarations() -> None:
    theme = parse_theme(
        theme_json(tokens={"radius": "2px"}, dark={"bg": "#000"}, light={"bg": "#fff"}),
        fmt="json",
    )
    css = theme.css()
    assert ':root, :root[data-theme="dark"]' in css
    assert ':root[data-theme="light"]' in css
    assert "@media (prefers-color-scheme: light)" in css
    assert "--bg: #000;" in css and "--bg: #fff;" in css and "--radius: 2px;" in css
    assert all(char not in css for char in "<>\\") and "url(" not in css and "@import" not in css


def test_an_empty_theme_serves_no_css_and_the_builtin_name() -> None:
    assert Appearance().css == ""
    assert Appearance().branding.name == "rayito" and not Appearance().branding.logo
    assert Appearance(theme=parse_theme(theme_json(), fmt="json")).css.count("{") == 0


def test_every_documented_token_is_covered() -> None:
    assert set(ALL_TOKENS) == {*COLOR_TOKENS, *LENGTH_TOKENS, *FONT_TOKENS}


def svg(body: str = "", root_attrs: str = "") -> bytes:
    return f'<svg xmlns="http://www.w3.org/2000/svg" {root_attrs}>{body}</svg>'.encode()


def test_it_accepts_a_png_by_signature_and_a_plain_svg() -> None:
    assert parse_logo(PNG_SIGNATURE + b"rest").content_type == "image/png"
    assert parse_logo(svg('<circle r="1" fill="#fff"/><use href="#a"/>')).content_type == (
        "image/svg+xml"
    )


@pytest.mark.parametrize(
    "logo",
    [
        svg("<script>alert(1)</script>"),
        svg("<foreignObject><div/></foreignObject>"),
        svg("", 'onload="alert(1)"'),
        svg('<a href="https://example.com"><rect/></a>'),
        svg('<image href="data:image/png;base64,AAAA"/>'),
        svg('<rect fill="url(https://example.com/x)"/>'),
        svg("<style>@import 'x.css';</style>"),
        svg('<animate attributeName="href" to="javascript:alert(1)"/>'),
        svg('<rect style="fill:url(//example.com/x)"/>'),
        b'<!DOCTYPE svg [<!ENTITY a "b">]><svg xmlns="http://www.w3.org/2000/svg"/>',
        b"<html><body/></html>",
        b"GIF89a",
        b"",
    ],
)
def test_it_rejects_unsafe_or_unknown_logos(logo: bytes) -> None:
    with pytest.raises(InvalidArgumentException):
        parse_logo(logo)


def test_it_rejects_a_logo_over_the_size_cap() -> None:
    with pytest.raises(InvalidArgumentException):
        parse_logo(PNG_SIGNATURE + b"0" * DASHBOARD_LOGO_MAX_BYTES)
