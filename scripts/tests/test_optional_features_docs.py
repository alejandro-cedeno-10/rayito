"""ADR-014 y la convención de opt-in (M11a): `ARCHITECTURE.md` documenta el
ADR, `SPEC.md` §4 refleja la enmienda, `openspec/project.md` no la contradice,
`docs/site/docs/optional-features.md` existe, está en la navegación y trae una
fila por función con su ancla estable, y cualquier fila que se marque
"disponible" cita el símbolo de su opción junto al marcador de docstring
"Coste y activación" en el fichero SDK que nombra su columna "Dónde".
`MILESTONES.md` trae los encabezados de M11 a M14."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

OPTIONAL_FEATURES_PAGE = "docs/site/docs/optional-features.md"

EXPECTED_TABLE_HEADER = (
    "| Función | Estado | Opción Python | Opción TypeScript | Por defecto | "
    "Qué activa | Recursos / llamadas AWS | Coste aproximado | IAM necesario | "
    "Cómo apagarla | Dónde |"
)

FUNCTION_ANCHORS = (
    "secrets-injection",
    "secrets-crud",
    "metadata-index",
    "otel-sdk",
)

DOCSTRING_MARKER = "Coste y activación"

REQUIRED_SUBHEADINGS = (
    "Activa",
    "Recursos y llamadas AWS",
    "Coste aproximado",
    "IAM",
    "Cómo apagarla",
    "Ejemplo",
)


def read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def flatten(text: str) -> str:
    return " ".join(text.split())


def parse_markdown_table(text: str, header: str) -> list[dict[str, str]]:
    """Filas de datos de la tabla cuya cabecera es exactamente `header`."""
    lines = text.splitlines()
    assert header in lines, f"no se encontró la cabecera de tabla «{header}»"
    start = lines.index(header)
    columns = [cell.strip() for cell in header.strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) == len(columns), (
            f"fila de tabla con {len(cells)} columnas, se esperaban {len(columns)}: {line}"
        )
        rows.append(dict(zip(columns, cells, strict=True)))
    return rows


def code_spans(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def option_symbol(span: str) -> str:
    """El identificador de la opción, sin argumentos ni paréntesis: `secrets=`
    se queda igual, `SecretStore(...)` y `new SecretStore({...})` se quedan
    en `SecretStore`."""
    stripped = span.removeprefix("new ").strip()
    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*(=)?", stripped)
    assert match, f"no se pudo extraer un símbolo de opción de «{span}»"
    return match.group(0)


def test_architecture_documents_adr_014() -> None:
    architecture = read("ARCHITECTURE.md")
    assert "## ADR-014" in architecture, (
        "ARCHITECTURE.md: falta el encabezado de ADR-014"
    )
    flat = flatten(architecture)
    assert "apagad" in flat, (
        "ARCHITECTURE.md: ADR-014 debe decir que está apagado por defecto"
    )
    assert "opción explícita" in flat, (
        "ARCHITECTURE.md: ADR-014 debe exigir una opción explícita"
    )
    assert "sin servidor" in flat, (
        "ARCHITECTURE.md: ADR-014 debe decir que Rayito no hospeda ningún servidor"
    )


def test_spec_section_4_reflects_the_adr_014_amendment() -> None:
    spec = read("SPEC.md")
    assert "## 4. No-objetivos" in spec
    match = re.search(r"## 4\. No-objetivos.*?(?=\n## 5\.)", spec, re.DOTALL)
    assert match, "SPEC.md: no se pudo aislar la sección §4"
    section_4 = match.group(0)
    assert "ADR-014" in section_4, "SPEC.md §4 debe citar ADR-014"
    assert (
        "Servicio de plano de control: en M1–M5 el SDK llama a AWS directamente"
        not in section_4
    ), "SPEC.md §4: la frase retirada sobre el plano de control sigue presente"


def test_openspec_project_hard_rule_3_allows_adr_014_components() -> None:
    project = read("openspec/project.md")
    match = re.search(
        r"3\.\s+\*\*One milestone at a time\.\*\*.*?(?=\n4\.)", project, re.DOTALL
    )
    assert match, "openspec/project.md: no se encontró la regla dura 3"
    assert "ADR-014" in match.group(0), (
        "openspec/project.md: la regla dura 3 debe citar ADR-014 para los "
        "componentes opcionales en la cuenta del cliente"
    )


def test_optional_features_page_is_in_the_nav() -> None:
    assert (REPO_ROOT / OPTIONAL_FEATURES_PAGE).exists(), (
        f"falta la página {OPTIONAL_FEATURES_PAGE}"
    )
    nav = read("docs/site/mkdocs.yml")
    assert "optional-features.md" in nav, (
        "docs/site/mkdocs.yml: falta optional-features.md en nav"
    )


def test_optional_features_table_header_matches_the_contract() -> None:
    page = read(OPTIONAL_FEATURES_PAGE)
    assert EXPECTED_TABLE_HEADER in page, (
        f"{OPTIONAL_FEATURES_PAGE}: la cabecera de la tabla no coincide con el contrato:\n"
        f"{EXPECTED_TABLE_HEADER}"
    )


def test_optional_features_table_has_one_row_per_function() -> None:
    page = read(OPTIONAL_FEATURES_PAGE)
    rows = parse_markdown_table(page, EXPECTED_TABLE_HEADER)
    assert len(rows) == 4, (
        f"se esperaban 4 filas de funciones con coste, hay {len(rows)}"
    )
    for anchor in FUNCTION_ANCHORS:
        assert f'id="{anchor}"' in page, (
            f"{OPTIONAL_FEATURES_PAGE}: falta el ancla #{anchor}"
        )
        assert f"#{anchor}" in page, (
            f"{OPTIONAL_FEATURES_PAGE}: ninguna fila enlaza a #{anchor}"
        )


def test_no_cost_section_lists_the_local_proxy() -> None:
    page = read(OPTIONAL_FEATURES_PAGE)
    assert "## Sin coste AWS" in page
    assert "rayito sandbox proxy" in page
    assert 'id="local-proxy"' in page


def cost_and_activation_blocks(content: str, window_lines: int = 40) -> list[str]:
    """Un bloque por cada aparición del marcador «Coste y activación»: desde
    esa línea hasta `window_lines` líneas después (basta para cubrir sus seis
    apartados y el ejemplo) o hasta el siguiente marcador, lo que llegue
    antes. Así un apartado de otro bloque cercano nunca cuenta para este."""
    lines = content.splitlines()
    starts = [i for i, line in enumerate(lines) if DOCSTRING_MARKER in line]
    blocks: list[str] = []
    for position, start in enumerate(starts):
        end = start + window_lines
        if position + 1 < len(starts):
            end = min(end, starts[position + 1])
        blocks.append("\n".join(lines[start:end]))
    return blocks


def cost_block_problems(content: str, symbols: list[str], label: str) -> list[str]:
    """Problemas del fichero SDK `content` frente al contrato: cada símbolo de
    opción debe aparecer en **un mismo** bloque «Coste y activación» junto con
    los seis apartados obligatorios. Lista vacía = cumple."""
    blocks = cost_and_activation_blocks(content)
    if not blocks:
        return [f"{label}: no lleva el bloque «{DOCSTRING_MARKER}»"]
    problems: list[str] = []
    for symbol in symbols:
        with_symbol = [block for block in blocks if symbol in block]
        if not with_symbol:
            problems.append(
                f"{label}: no menciona el símbolo de opción «{symbol}» dentro de un "
                f"bloque «{DOCSTRING_MARKER}»"
            )
            continue
        if not any(
            all(h in block for h in REQUIRED_SUBHEADINGS) for block in with_symbol
        ):
            missing = sorted(
                {
                    h
                    for block in with_symbol
                    for h in REQUIRED_SUBHEADINGS
                    if h not in block
                }
            )
            problems.append(
                f"{label}: el bloque «{DOCSTRING_MARKER}» de «{symbol}» no lleva "
                f"todos los apartados (faltan: {', '.join(missing)})"
            )
    return problems


def test_available_rows_cite_their_docstring_marker_in_the_named_sdk_file() -> None:
    page = read(OPTIONAL_FEATURES_PAGE)
    rows = parse_markdown_table(page, EXPECTED_TABLE_HEADER)
    available = [row for row in rows if "disponible" in row["Estado"]]
    problems: list[str] = []
    for row in available:
        function_name = row["Función"]
        symbols = [option_symbol(span) for span in code_spans(row["Opción Python"])]
        symbols += [
            option_symbol(span) for span in code_spans(row["Opción TypeScript"])
        ]
        assert symbols, (
            f"{function_name}: fila 'disponible' sin ningún símbolo de opción citado"
        )
        paths = re.findall(r"`([^`]+\.(?:py|ts))`", row["Dónde"])
        assert paths, (
            f"{function_name}: la columna 'Dónde' no nombra ningún fichero SDK"
        )
        for path in paths:
            problems += cost_block_problems(
                read(path), symbols, f"{function_name}: {path}"
            )
    assert problems == []


_FULL_BLOCK = """Coste y activación
-------------------
Activa: algo con `secrets=`
Recursos y llamadas AWS: secretsmanager:GetSecretValue
Coste aproximado: $0,40
IAM: secretsmanager:GetSecretValue
Cómo apagarla: secrets=None
Ejemplo:
    Sandbox.create(secrets={"K": "n"})
"""


def test_cost_block_checker_accepts_a_complete_block(tmp_path: Path) -> None:
    fixture = tmp_path / "ok.py"
    fixture.write_text(f'"""{_FULL_BLOCK}"""\n', encoding="utf-8")
    assert (
        cost_block_problems(fixture.read_text(encoding="utf-8"), ["secrets="], "ok")
        == []
    )


def test_cost_block_checker_flags_a_file_without_the_marker(tmp_path: Path) -> None:
    fixture = tmp_path / "no_marker.py"
    fixture.write_text(
        '"""Activa: secrets= IAM Coste aproximado"""\n', encoding="utf-8"
    )
    problems = cost_block_problems(
        fixture.read_text(encoding="utf-8"), ["secrets="], "x"
    )
    assert len(problems) == 1
    assert DOCSTRING_MARKER in problems[0]


def test_cost_block_checker_flags_a_symbol_outside_the_block(tmp_path: Path) -> None:
    fixture = tmp_path / "symbol_outside.py"
    block = _FULL_BLOCK.replace("`secrets=`", "algo").replace("secrets=None", "None")
    block = block.replace('secrets={"K": "n"}', "")
    filler = "\n" * 60
    fixture.write_text(f'"""{block}"""{filler}secrets= = None\n', encoding="utf-8")
    problems = cost_block_problems(
        fixture.read_text(encoding="utf-8"), ["secrets="], "x"
    )
    assert len(problems) == 1
    assert "símbolo de opción" in problems[0]


def test_cost_block_checker_flags_a_missing_subheading(tmp_path: Path) -> None:
    fixture = tmp_path / "missing_iam.py"
    block = _FULL_BLOCK.replace("IAM: secretsmanager:GetSecretValue\n", "")
    fixture.write_text(f'"""{block}"""\n', encoding="utf-8")
    problems = cost_block_problems(
        fixture.read_text(encoding="utf-8"), ["secrets="], "x"
    )
    assert len(problems) == 1
    assert "IAM" in problems[0]


def test_cost_block_checker_does_not_borrow_subheadings_from_another_block(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "split.py"
    partial = "Coste y activación\nActiva: `secrets=`\nCoste aproximado: $0\n"
    fixture.write_text(
        f'"""{partial}"""\n"""{_FULL_BLOCK.replace("secrets", "otra")}"""\n',
        encoding="utf-8",
    )
    problems = cost_block_problems(
        fixture.read_text(encoding="utf-8"), ["secrets="], "x"
    )
    assert len(problems) == 1
    assert "apartados" in problems[0]


def test_milestones_has_m11_through_m14_headers() -> None:
    milestones = read("MILESTONES.md")
    for milestone in ("M11", "M12", "M13", "M14"):
        assert f"## {milestone}" in milestones, (
            f"MILESTONES.md: falta el encabezado ## {milestone}"
        )
