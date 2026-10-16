"""``check_docs_i18n.py``: cada página en español tiene su pareja en inglés, sin huérfanas ni
desfases de ejemplos; y, sobre el sitio real, que el estado actual pasa."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_docs_examples as examples
import check_docs_i18n as checker

CODE = "```python\nx = 1\n```\n"


def write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_spanish_page_without_english_twin_is_reported(tmp_path: Path) -> None:
    write(tmp_path, "a.md", "# A\n")
    problems = checker.check(tmp_path, frozenset())
    assert problems == ["a.md: falta a.en.md (o añádela a PENDING_TRANSLATION)"]


def test_pending_pages_are_tolerated_until_they_gain_a_twin(tmp_path: Path) -> None:
    write(tmp_path, "a.md", "# A\n")
    assert checker.check(tmp_path, frozenset({"a.md"})) == []
    write(tmp_path, "a.en.md", "# A\n")
    problems = checker.check(tmp_path, frozenset({"a.md"}))
    assert problems == ["a.md: ya tiene a.en.md; quítala de PENDING_TRANSLATION"]


def test_stale_pending_entry_is_reported(tmp_path: Path) -> None:
    assert checker.check(tmp_path, frozenset({"gone.md"})) == [
        "gone.md: está en PENDING_TRANSLATION pero la página no existe"
    ]


def test_orphan_english_page_is_reported(tmp_path: Path) -> None:
    write(tmp_path, "sub/b.en.md", "# B\n")
    assert checker.check(tmp_path, frozenset()) == [
        "sub/b.en.md: no hay sub/b.md (el español manda)"
    ]


def test_code_block_count_must_match(tmp_path: Path) -> None:
    write(tmp_path, "a.md", "# A\n" + CODE + CODE)
    write(tmp_path, "a.en.md", "# A\n" + CODE)
    problems = checker.check(tmp_path, frozenset())
    assert len(problems) == 1 and "2 bloques" in problems[0]


def test_limit_macros_must_match(tmp_path: Path) -> None:
    write(tmp_path, "a.md", "Hasta {{maxTimeoutSeconds}} s.\n")
    write(tmp_path, "a.en.md", "Up to 5 s.\n")
    problems = checker.check(tmp_path, frozenset())
    assert len(problems) == 1 and "macros" in problems[0]


def test_spanish_anchors_must_exist_in_the_english_page(tmp_path: Path) -> None:
    write(tmp_path, "a.md", "# A\n\n## Cuánto cuesta (con ejemplos)\n")
    write(tmp_path, "a.en.md", "# A\n\n## How much it costs\n")
    problems = checker.check(tmp_path, frozenset())
    assert len(problems) == 1 and "cuanto-cuesta-con-ejemplos" in problems[0]
    write(tmp_path, "a.en.md", "# A\n\n## How much it costs { #cuanto-cuesta-con-ejemplos }\n")
    assert checker.check(tmp_path, frozenset()) == []


def test_slugify_matches_the_toc_extension() -> None:
    toc = pytest.importorskip("markdown.extensions.toc")
    for text in (
        "Cuánto cuesta (con ejemplos)",
        "4. Publicar la imagen `rayito-base`",
        "Secretos: `Secret`",
    ):
        assert checker.slugify(text) == toc.slugify(text.replace("`", ""), "-")


def test_anchors_ignore_code_fences_and_dedupe(tmp_path: Path) -> None:
    text = "# T\n```bash\n# comentario\n```\n## Uno\n## Uno\n"
    assert checker.anchors(text) == ["uno", "uno_1"]


def test_index_pages_pair_by_suffix(tmp_path: Path) -> None:
    write(tmp_path, "guias/index.md", "# G\n")
    write(tmp_path, "guias/index.en.md", "# G\n")
    assert checker.check(tmp_path, frozenset()) == []


def test_english_pages_are_collected_by_the_examples_checker(tmp_path: Path) -> None:
    write(tmp_path, "a.md", "# A\n" + CODE)
    write(tmp_path, "a.en.md", "# A\n" + CODE)
    pages = {block.page for block in examples.collect(tmp_path).blocks}
    assert pages == {"a.md", "a.en.md"}
    stems = {block.stem for block in examples.collect(tmp_path).blocks}
    assert len(stems) == 2


def test_the_real_site_is_consistent() -> None:
    assert checker.check() == []
