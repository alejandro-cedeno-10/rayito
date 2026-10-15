"""Ayudantes de DOM de la página del panel: un bloque opcional ausente no se
pinta como el texto «null». Los módulos ES de la página no necesitan DOM para
esta función pura, así que se ejecuta con Node (se salta si no está)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = Path(__file__).resolve().parents[2] / "src" / "rayito"
STATIC_DIR = SRC_DIR / "_dashboard" / "_static" / "js"
NODE = shutil.which("node")
NODE_TIMEOUT_SECONDS = 30
GUIDE_PAGE = REPO_ROOT / "docs" / "site" / "docs" / "funciones-opcionales" / "panel-de-gestion.md"
SITE_URL = "https://alejandro-cedeno-10.github.io/rayito/"

pytestmark = pytest.mark.skipif(NODE is None, reason="node no está instalado")


def renderable(content_literal: str) -> list[object]:
    script = (
        f'import {{ renderableNodes }} from "{(STATIC_DIR / "dom.js").as_uri()}";'
        f"console.log(JSON.stringify(renderableNodes({content_literal})));"
    )
    assert NODE is not None
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=NODE_TIMEOUT_SECONDS,
    )
    parsed: list[object] = json.loads(result.stdout)
    return parsed


def test_renderable_nodes_drops_the_blocks_that_do_not_apply() -> None:
    assert renderable('["a", null, undefined, false, "b"]') == ["a", "b"]


def test_renderable_nodes_flattens_and_accepts_a_single_value() -> None:
    assert renderable('["a", ["b", null], "c"]') == ["a", "b", "c"]
    assert renderable('"solo"') == ["solo"]
    assert renderable("null") == []


def test_every_view_paints_through_the_shared_helper() -> None:
    for source in (STATIC_DIR / "view.js", STATIC_DIR / "views" / "sandboxes.js"):
        text = source.read_text(encoding="utf-8")
        assert "renderableNodes(" in text
        assert "[].concat(" not in text


def test_text_states_in_stat_cards_use_the_title_size() -> None:
    """«Desactivado» y «Bajo demanda» son texto, no cifras: con el tamaño de
    cifra se salían de la tarjeta en pantallas de ~1280 px."""
    overview = (STATIC_DIR / "views" / "overview.js").read_text(encoding="utf-8")
    styles = (STATIC_DIR.parent / "css" / "app.css").read_text(encoding="utf-8")
    assert '"stat-value stat-value-text muted"' in overview
    assert ".stat-value-text {" in styles


def help_view_source() -> str:
    return (STATIC_DIR / "views" / "help.js").read_text(encoding="utf-8")


def test_help_is_a_sidebar_route_with_an_icon() -> None:
    """El manual de ayuda es una entrada de la barra lateral con su icono."""
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    dom = (STATIC_DIR / "dom.js").read_text(encoding="utf-8")
    assert 'import "./views/help.js";' in app
    assert '{ path: "help", label: "Ayuda", tag: "rayito-help", icon: "help" }' in app
    assert "  help: " in dom
    assert 'customElements.define("rayito-help", RayitoHelp);' in help_view_source()


def test_help_makes_no_network_call_and_links_to_the_published_guide() -> None:
    """La ayuda se pinta con `meta`: ni `fetch` ni la API; sólo un enlace a la guía."""
    source = help_view_source()
    for forbidden in ("fetch(", "get(", "XMLHttpRequest", "import { get"):
        assert forbidden not in source
    guide = SITE_URL + "funciones-opcionales/panel-de-gestion/#recorrido"
    assert f'DOCS_GUIDE_URL = "{guide}"' in source
    assert "{ #recorrido }" in GUIDE_PAGE.read_text(encoding="utf-8")


def test_help_lists_every_view_of_the_panel() -> None:
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    source = help_view_source()
    for view in ("overview", "sandboxes", "quotas", "cost", "images", "events"):
        assert f'path: "{view}"' in app
        assert f'route: "{view}"' in source


def run_module(module: str, expression: str) -> object:
    """Evalúa `expression` con las exportaciones de un módulo ES de la página."""
    script = (
        f'import * as m from "{(STATIC_DIR / module).as_uri()}";'
        f"console.log(JSON.stringify({expression}));"
    )
    assert NODE is not None
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=NODE_TIMEOUT_SECONDS,
    )
    return json.loads(result.stdout)


def hints_for(deployment: str, **flags: bool) -> list[dict[str, object]]:
    meta = {
        "deployment": deployment,
        "allowActions": flags.get("actions", False),
        "costExplorer": {"enabled": flags.get("cost", False)},
        "eventsConfigured": flags.get("events", False),
    }
    rows = run_module("help-hints.js", f"m.statusRows({json.dumps(meta)})")
    assert isinstance(rows, list)
    return rows


def test_help_hints_name_the_cli_flags_in_the_local_panel() -> None:
    hints = [row["hint"] for row in hints_for("local")]
    assert hints == ["--allow-actions", "--cost-explorer", "--events-stack <nombre>"]
    assert all(row["hintIsFlag"] for row in hints_for("local"))


def test_help_hints_do_not_mention_cli_flags_in_the_account_panel() -> None:
    rows = hints_for("stack")
    assert not any(row["hintIsFlag"] for row in rows)
    assert "grupo de acciones" in str(rows[0]["hint"])
    assert "EnableCostExplorer" in str(rows[1]["hint"])
    assert "--allow-actions" not in str(rows[0]["hint"])
    assert "--cost-explorer" not in str(rows[1]["hint"])


def test_a_queue_under_one_second_is_not_announced_as_zero_seconds() -> None:
    assert run_module("dom.js", "m.formatEta(0.3)") == "menos de 1 s"
    assert run_module("dom.js", "m.formatEta(0)") == "menos de 1 s"
    assert run_module("dom.js", "m.formatEta(45)") == "unos 45 s"


def test_sandbox_counts_agree_in_number() -> None:
    assert run_module("dom.js", "m.countSandboxes(1)") == "1 sandbox"
    assert run_module("dom.js", "m.countSandboxes(3)") == "3 sandboxes"
    assert run_module("dom.js", 'm.plural(1, "consulta", "consultas")') == "1 consulta"
    assert run_module("dom.js", 'm.plural(2, "consulta", "consultas")') == "2 consultas"
    for view in ("sandboxes.js", "cost.js"):
        assert "(es)" not in (STATIC_DIR / "views" / view).read_text(encoding="utf-8")
        assert "consulta(s)" not in (STATIC_DIR / "views" / view).read_text(encoding="utf-8")
