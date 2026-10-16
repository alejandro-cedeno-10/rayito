"""El hook de MkDocs que quita los `<link rel="alternate" hreflang>` del head."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("mkdocs")

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "docs" / "site" / "hooks" / "alternate_links.py"
RSS = '<link rel="alternate" type="application/rss+xml" title="RSS" href="feed.xml">'


def load_hook():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("alternate_links", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hreflang_alternates_are_removed() -> None:
    html = (
        '<head>\n      <link rel="canonical" href="https://x/cost/">\n'
        '      <link rel="alternate" href="./" hreflang="es">\n'
        '      <link rel="alternate" href="../en/cost/" hreflang="en">\n'
        '      <link rel="icon" href="a.svg">\n</head>'
    )
    out = load_hook().on_post_page(html, page=None, config=None)
    assert "alternate" not in out
    assert 'rel="canonical"' in out and 'rel="icon"' in out


def test_other_alternate_links_are_kept() -> None:
    assert load_hook().on_post_page(RSS) == RSS


def test_the_selector_links_are_not_touched() -> None:
    link = '<a href="../en/cost/" hreflang="en" class="md-select__link">'
    assert load_hook().on_post_page(link) == link


def test_static_templates_such_as_404_are_cleaned() -> None:
    html = '<link rel="alternate" href="./" hreflang="es">\n<link rel="icon" href="a.svg">'
    out = load_hook().on_post_template(html, template_name="404.html", config=None)
    assert "alternate" not in out and 'rel="icon"' in out


def test_build_fails_when_an_alternate_survives(tmp_path: Path) -> None:
    (tmp_path / "ok.html").write_text(RSS, encoding="utf-8")
    config = {"site_dir": str(tmp_path)}
    load_hook().on_post_build(config)
    (tmp_path / "bad.html").write_text(
        '<link rel="alternate" hreflang="en" href="x">', encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match=r"bad\.html"):
        load_hook().on_post_build(config)
