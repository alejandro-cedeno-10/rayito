"""La puerta de documentación de `m9-server-timeout` (design D11, «Docs
gate»): `set_timeout` deja de ser un no-objetivo en `SPEC.md` §4 y en la regla
3 de `openspec/project.md`, ADR-007 queda sustituida por ADR-011 sin perder su
cuerpo, y la fila T2 de `SECURITY.md` nombra la salida por vencimiento del
plazo sin retirar el origen de dentro de la VM. Un revert silencioso de
cualquiera de las tres rompe la puerta."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def flatten(text: str) -> str:
    return " ".join(text.split())


def section(text: str, heading: str) -> str:
    """El cuerpo de una sección Markdown, hasta el siguiente encabezado de su
    mismo nivel o superior."""
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith(heading)]
    assert starts, f"no se encontró el encabezado «{heading}»"
    start = starts[0]
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    for offset, line in enumerate(lines[start + 1 :], start=start + 1):
        if line.startswith("#") and len(line) - len(line.lstrip("#")) <= level:
            return "\n".join(lines[start:offset])
    return "\n".join(lines[start:])


def hard_rule(text: str, number: int) -> str:
    """La regla numerada `number.` de una lista Markdown, con sus líneas de
    continuación, hasta la siguiente regla numerada."""
    lines = text.splitlines()
    starts = [
        index for index, line in enumerate(lines) if line.startswith(f"{number}. ")
    ]
    assert starts, f"no se encontró la regla {number}"
    body = [lines[starts[0]]]
    for line in lines[starts[0] + 1 :]:
        if line[:1].isdigit() or line.startswith("#"):
            break
        body.append(line)
    return flatten("\n".join(body))


def test_set_timeout_is_no_longer_a_non_goal() -> None:
    non_goals = flatten(section(read("SPEC.md"), "## 4."))
    assert "set_timeout" not in non_goals, "SPEC.md §4 sigue listando set_timeout"
    rule = hard_rule(read("openspec/project.md"), 3)
    assert "set_timeout" not in rule, (
        "openspec/project.md regla 3 sigue diciendo «no set_timeout»"
    )


def test_adr_007_is_superseded_by_adr_011() -> None:
    architecture = read("ARCHITECTURE.md")
    adr_007 = section(architecture, "## ADR-007")
    first_lines = [line for line in adr_007.splitlines()[1:] if line.strip()]
    assert first_lines, "ADR-007 quedó vacía"
    assert "Sustituida por ADR-011" in first_lines[0], (
        "la primera línea de ADR-007 no dice «Sustituida por ADR-011»"
    )
    assert "UpdateMicrovm" in adr_007, "ADR-007 perdió su cuerpo"
    adr_011 = flatten(section(architecture, "## ADR-011"))
    for fragment in ("max_lifetime", "maximumDurationInSeconds", "reincarnate()"):
        assert fragment in adr_011, f"ADR-011: falta «{fragment}»"


def test_t2_names_the_timeout_exit() -> None:
    rows = [
        line for line in read("SECURITY.md").splitlines() if line.startswith("| T2 |")
    ]
    assert len(rows) == 1, (
        f"SECURITY.md: T2 debería ser una única fila, hay {len(rows)}"
    )
    row = flatten(rows[0])
    for fragment in ("m9-server-timeout", "SetTimeout", "auto-DoS", "0.0.0.0:9000"):
        assert fragment in row, f"SECURITY.md T2: falta «{fragment}»"
