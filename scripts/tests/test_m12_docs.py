"""La documentación de M12 (`m12-sizes-proxy`) frente al código que entrega:
`limits.md` tiene la sección de tamaño y cita su fuente, las filas 82 y 110
de `e2b-parity.md` quedan al día con el proxy local y la elección de tamaño
por imagen, y `SECURITY.md` documenta el alcance del JWE del proxy. Cada
test nombra el fichero y el fragmento que falta."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def flatten(text: str) -> str:
    """El texto con cada racha de espacio en blanco colapsada, para que las
    aserciones no dependan de dónde parta la línea el ajuste de párrafo."""
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
            return flatten("\n".join(lines[start:offset]))
    return flatten("\n".join(lines[start:]))


def parity_row(number: int) -> str:
    """La única fila de `e2b-parity.md` cuyo número de ledger es `number`."""
    rows = [
        line
        for line in read("docs/site/docs/e2b-parity.md").splitlines()
        if line.startswith(f"| {number} |")
    ]
    assert len(rows) == 1, f"e2b-parity.md: fila {number} debería ser única, hay {len(rows)}"
    return flatten(rows[0])


def test_limits_md_has_the_size_section() -> None:
    limits = read("docs/site/docs/limits.md")
    assert "## Tamaño (CPU/RAM)" in limits, "limits.md: falta '## Tamaño (CPU/RAM)'"
    size_section = section(limits, "## Tamaño (CPU/RAM)")
    assert "AWS_API_NOTES.md" in size_section, "limits.md: la sección no cita AWS_API_NOTES.md"
    assert "Q68" in size_section, "limits.md: la sección no cita Q68"
    assert "--memory-mib" in size_section
    assert "resources[0].minimumMemoryInMiB" in size_section or "minimumMemoryInMiB" in (
        size_section
    )


def test_limits_md_size_section_comes_before_compat_table() -> None:
    limits = read("docs/site/docs/limits.md")
    size_index = limits.index("## Tamaño (CPU/RAM)")
    compat_index = limits.index("## Compatibilidad SDK ↔ rayd ↔ imagen")
    assert size_index < compat_index


def test_parity_row_82_documents_the_per_image_sizes() -> None:
    row = parity_row(82)
    assert " | divergente" in row, "e2b-parity.md: la fila 82 debería ser 'divergente'"
    assert "memory-mib" in row
    assert "Q68" in row


def test_parity_row_110_points_to_the_local_proxy() -> None:
    row = parity_row(110)
    assert "sandbox proxy" in row


def test_security_md_documents_the_proxy_jwe_scope() -> None:
    security = read("SECURITY.md")
    assert "sandbox proxy" in security, "SECURITY.md: no menciona 'sandbox proxy'"
    assert "m12-sizes-proxy" in security


def test_site_security_md_documents_the_proxy() -> None:
    page = read("docs/site/docs/security.md")
    assert "sandbox proxy" in page
