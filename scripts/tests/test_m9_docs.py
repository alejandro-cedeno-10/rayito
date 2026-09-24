"""La documentación de M9 frente al código que ya se entrega: cada decisión de
arquitectura queda escrita (ADR-010 transferencias, ADR-012 egress, ADR-013
kernels Deno), el modelo de amenazas gana T16 y T17 y los apéndices de T8,
T10 y T12, el sitio tiene las páginas nuevas en su navegación, y ninguna página
pública vuelve a decir que el shim lanza `UnimplementedError` para algo que ya
se mapea (`set_timeout`, `upload_url`, `allow_internet_access=False`,
`next_token`, historial de métricas) ni que `javascript` es un nombre
reservado. Cada test nombra el fichero y el fragmento que falta o que
reapareció."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def flatten(text: str) -> str:
    return " ".join(text.split())


def section(text: str, heading: str) -> str:
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith(heading)]
    assert starts, f"no se encontró el encabezado «{heading}»"
    start = starts[0]
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    for offset, line in enumerate(lines[start + 1 :], start=start + 1):
        if line.startswith("#") and len(line) - len(line.lstrip("#")) <= level:
            return flatten("\n".join(lines[start:offset]))
    return flatten("\n".join(lines[start:]))


def threat_row(marker: str) -> str:
    rows = [
        line
        for line in read("SECURITY.md").splitlines()
        if line.startswith(f"| {marker} |")
    ]
    assert len(rows) == 1, (
        f"SECURITY.md: {marker} debería ser una única fila, hay {len(rows)}"
    )
    return flatten(rows[0])


def first_cells(text: str, heading: str) -> list[str]:
    """La primera columna de cada fila de datos de la tabla de una sección."""
    lines = text.splitlines()
    start = lines.index(heading)
    cells: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        if line.startswith("|") and not line.startswith("|---"):
            cells.append(line.split(" | ")[0].removeprefix("| ").strip())
    return cells[1:]


def assert_says(where: str, text: str, fragments: Iterable[str]) -> None:
    for fragment in fragments:
        assert fragment in text, f"{where}: falta «{fragment}»"


def assert_silent(where: str, text: str, fragments: Iterable[str]) -> None:
    for fragment in fragments:
        assert fragment not in text, f"{where}: reapareció «{fragment}»"


def test_the_m9_adrs_are_written() -> None:
    architecture = read("ARCHITECTURE.md")
    assert_says(
        "ARCHITECTURE.md ADR-010",
        section(architecture, "## ADR-010"),
        ("credenciales del llamante", "SignedHttp", "T16"),
    )
    assert_says(
        "ARCHITECTURE.md ADR-012",
        section(architecture, "## ADR-012"),
        ("rayito-base-caps", "CAP_NET_ADMIN", "T17", "UnimplementedError"),
    )
    assert_says(
        "ARCHITECTURE.md ADR-013",
        section(architecture, "## ADR-013"),
        ("127.0.0.1", "HMAC", "iopub", "ipc", "KernelLanguage.transport"),
    )
    services = section(architecture, "### Servicios")
    assert_says(
        "ARCHITECTURE.md «Servicios»",
        services,
        ("LifecycleService", "NetworkService", "MetricsHistory", "StartImport"),
    )


def test_the_threat_model_names_the_m9_surfaces() -> None:
    assert_says(
        "SECURITY.md T16",
        threat_row("T16"),
        ("SSRF", "X-Amz-Signature", "169.254.169.254", "max_bytes"),
    )
    assert_says(
        "SECURITY.md T17",
        threat_row("T17"),
        ("SSRF", "RAYITO_ALLOW_ROOT", "falla cerrado", "runHookPayload", "C-01"),
    )
    assert_says(
        "SECURITY.md T8", threat_row("T8"), ("npm:", "rayito-base-caps", "ADR-012")
    )
    assert_says("SECURITY.md T10", threat_row("T10"), ("Deno", "sha256sum -c"))
    assert_says("SECURITY.md T12", threat_row("T12"), ("ADR-013", "127.0.0.1", "iopub"))
    assert_says("SECURITY.md T4", threat_row("T4"), ("cpu_count", "memory_total_bytes"))
    assert_says(
        "SECURITY.md T9",
        threat_row("T9"),
        ("dangerously_authenticate", "transfer_id"),
    )
    site = read("docs/site/docs/security.md")
    assert_says("docs/site/docs/security.md", site, ("T16", "T17"))


def test_the_site_navigates_to_the_new_pages() -> None:
    nav = read("docs/site/mkdocs.yml")
    for page in ("files.md", "network.md", "git.md"):
        assert page in nav, f"docs/site/mkdocs.yml: falta {page} en la navegación"
        assert (REPO_ROOT / "docs/site/docs" / page).is_file(), (
            f"falta docs/site/docs/{page}"
        )
    persistence = nav.index("persistence.md")
    assert persistence < nav.index("files.md"), "files.md debe ir tras persistence.md"
    assert persistence < nav.index("network.md"), (
        "network.md debe ir tras persistence.md"
    )


def test_e2b_compat_no_longer_lists_mapped_features_as_unimplemented() -> None:
    page = read("docs/site/docs/e2b-compat.md")
    features = " ".join(first_cells(page, "## Lanza `UnimplementedError`"))
    assert_silent(
        "e2b-compat.md «Lanza UnimplementedError» (columna de features)",
        features,
        (
            "set_timeout",
            "next_token",
            "get_metrics(start=",
            "Sandbox.get_metrics(id)",
            "auto_pause",
        ),
    )
    for cell in first_cells(page, "## Lanza `UnimplementedError`"):
        if "upload_url" in cell or "download_url" in cell:
            assert "sin bucket" in cell, (
                f"e2b-compat.md: «{cell}» sigue sin nombrar el caso sin bucket"
            )
        if "allow_internet_access=False" in cell:
            assert "fuera de `rayito-base-caps`" in cell, (
                f"e2b-compat.md: «{cell}» sigue sin nombrar el caso fuera de caps"
            )
    assert_silent(
        "e2b-compat.md",
        flatten(page),
        (
            "`next_token` siempre `None`",
            "Rayito **no puede**",
            "javascript` es UNIMPLEMENTED en toda imagen",
        ),
    )
    mapped = section(page, "## Se mapea, con una nota")
    assert_says(
        "e2b-compat.md «Se mapea, con una nota»",
        mapped,
        (
            "set_timeout",
            "upload_url",
            "allow_internet_access=False",
            "next_token",
            "get_metrics(start, end)",
            "typescript",
        ),
    )


def test_javascript_is_no_longer_called_reserved() -> None:
    for relative in (
        "SPEC.md",
        "README.md",
        "docs/site/docs/kernels.md",
        "proto/rayito/v1/code.proto",
    ):
        text = flatten(read(relative))
        assert_silent(relative, text, ("reservado", "no incluido"))
    kernels = read("docs/site/docs/kernels.md")
    assert_says(
        "docs/site/docs/kernels.md",
        flatten(kernels),
        (
            "JavaScript y TypeScript con Deno",
            "Agentes anteriores a M9",
            "ADR-013",
            "R y Java",
        ),
    )


def test_readmes_show_a_working_set_timeout() -> None:
    for relative in ("README.md", "clients/python/README.md"):
        text = read(relative)
        assert_silent(relative, text, ("UnimplementedError: no existe UpdateMicrovm",))
        assert "set_timeout(" in text, (
            f"{relative}: el snippet del shim perdió set_timeout"
        )
    typescript = read("clients/typescript/README.md")
    assert_says(
        "clients/typescript/README.md",
        typescript,
        ("setTimeout", "onTimeout", "rayito/e2b"),
    )


def test_release_0_3_0_changelogs_name_the_m9_surfaces() -> None:
    python = section(read("clients/python/CHANGELOG.md"), "## [0.3.0]")
    assert_says(
        "clients/python/CHANGELOG.md [0.3.0]",
        python,
        (
            "max_lifetime",
            "on_timeout",
            "set_timeout",
            "get_metrics_history",
            "paginate",
            "update_network",
            "typescript",
        ),
    )
    assert_silent(
        "clients/python/CHANGELOG.md [0.3.0]",
        python,
        ("`rayito.e2b.Sandbox.set_timeout` sigue siendo `UnimplementedError`",),
    )
    typescript = section(read("clients/typescript/CHANGELOG.md"), "## [0.3.0]")
    assert_says(
        "clients/typescript/CHANGELOG.md [0.3.0]",
        typescript,
        (
            "setTimeout",
            "maxLifetimeMs",
            "onTimeout",
            "getMetricsHistory",
            "paginate",
            "updateNetwork",
        ),
    )
    rayd = section(read("crates/rayd/CHANGELOG.md"), "## [0.3.0]")
    assert_says(
        "crates/rayd/CHANGELOG.md [0.3.0]",
        rayd,
        ("LifecycleService", "MetricsHistory", "NetworkService", "StartImport"),
    )


def test_platform_egress_facts_are_recorded() -> None:
    notes = read("AWS_API_NOTES.md")
    run_microvm = section(notes, "## 2. `run-microvm`")
    assert_says(
        "AWS_API_NOTES.md §2",
        run_microvm,
        (
            "HTTP_INGRESS",
            "NO_EGRESS",
            "ValidationException",
            "`egressNetworkConnectors: []`",
            "Q60",
        ),
    )
    questions = notes.splitlines()
    assert any(line.startswith("| 66 |") and "QE1" in line for line in questions), (
        "§16: falta la fila 66 (QE1)"
    )
    assert any(line.startswith("| 67 |") and "QE2" in line for line in questions), (
        "§16: falta la fila 67 (QE2)"
    )


def test_operator_guidance_for_transfers_exists() -> None:
    infra = section(read("infra/README.md"), "## Transferencias de ficheros")
    assert_says(
        "infra/README.md «Transferencias de ficheros»",
        infra,
        (
            "TransferBucket",
            "AbortIncompleteMultipartUpload",
            "s3:signatureversion",
            "aws:SecureTransport",
            "amzn-s3-demo-bucket",
            "kms:GenerateDataKey",
        ),
    )


def test_e2b_compat_quotes_every_unimplemented_reason_verbatim() -> None:
    """Cada motivo de la tabla D14 aparece literal en «Lanza
    UnimplementedError» (con `<` y `>` como entidades HTML, que se ven igual)."""
    import html

    from rayito.e2b._unimplemented import UNIMPLEMENTED_REASONS

    compat = read("docs/site/docs/e2b-compat.md")
    page = html.unescape(section(compat, "## Lanza `UnimplementedError`"))
    for feature, reason in UNIMPLEMENTED_REASONS.items():
        assert " ".join(reason.split()) in page, (
            f"e2b-compat.md: falta el motivo literal de «{feature}»"
        )


def test_the_parity_page_has_every_ledger_row() -> None:
    nav = read("docs/site/mkdocs.yml")
    for page in ("e2b-parity.md", "lifecycle.md", "observability.md", "images.md"):
        assert page in nav, f"docs/site/mkdocs.yml: falta {page} en la navegación"
    rows = [
        line
        for line in read("docs/site/docs/e2b-parity.md").splitlines()
        if line.startswith("| ") and line.split(" | ")[0][2:].isdigit()
    ]
    assert [int(row.split(" | ")[0][2:]) for row in rows] == list(range(1, 114))
    statuses = (
        "implementado",
        "divergente",
        "fuera por SPEC",
        "imposible en la plataforma",
    )
    for row in rows:
        status = row.split(" | ")[2]
        assert status.startswith(statuses), (
            f"e2b-parity.md: estado desconocido «{status}»"
        )
