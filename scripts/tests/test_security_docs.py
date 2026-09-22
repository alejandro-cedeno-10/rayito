"""Las frases que la auditoría interna de 2026-09-22 corrigió, fijadas una por
fila del triage (C-01, C-02, C-03, C-04, C-07, C-08, C-09, H-06): cada test
comprueba que el texto corregido sigue ahí **y** que la frase retirada no ha
vuelto, para que un revert silencioso rompa la puerta en vez de pasar
desapercibido. El fallo nombra siempre el fichero y el fragmento."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

RETIRED_SENTENCES = {
    "SECURITY.md": ("el kernel no se reinicia", "la máquina que ejecuta el SDK"),
    "ARCHITECTURE.md": ("cada hook se audita",),
    "infra/README.md": (
        "otra rama, otro environment u otro repositorio no pueden asumirlo",
    ),
    "docs/site/docs/concepts.md": ("(o exporta `RAYITO_ACCESS_TOKEN`)",),
}


def flatten(text: str) -> str:
    """El texto con cada racha de espacio en blanco colapsada, para que las
    aserciones no dependan de dónde parta la línea el ajuste de párrafo."""
    return " ".join(text.split())


def read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def security_md() -> str:
    return read("SECURITY.md")


def architecture_md() -> str:
    return read("ARCHITECTURE.md")


def infra_readme() -> str:
    return read("infra/README.md")


def site_doc(name: str) -> str:
    return read(f"docs/site/docs/{name}.md")


def heading_index(lines: list[str], heading: str) -> int:
    for index, line in enumerate(lines):
        if line.startswith(heading):
            return index
    raise AssertionError(f"no se encontró el encabezado «{heading}»")


def section(text: str, heading: str) -> str:
    """El cuerpo de una sección Markdown, hasta el siguiente encabezado de su
    mismo nivel o superior."""
    lines = text.splitlines()
    start = heading_index(lines, heading)
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    for offset, line in enumerate(lines[start + 1 :], start=start + 1):
        if line.startswith("#") and len(line) - len(line.lstrip("#")) <= level:
            return flatten("\n".join(lines[start:offset]))
    return flatten("\n".join(lines[start:]))


def paragraph(text: str, opening: str) -> str:
    """El párrafo que empieza por `opening`, hasta la primera línea en blanco."""
    start = text.find(opening)
    assert start != -1, f"no se encontró el párrafo que empieza por «{opening}»"
    end = text.find("\n\n", start)
    return flatten(text[start:] if end == -1 else text[start:end])


def threat_row(marker: str) -> str:
    """La única fila de la tabla de amenazas de `SECURITY.md` cuyo primer campo
    es el marcador, para que una aserción sobre T2 no la satisfaga otra prosa."""
    rows = [
        line for line in security_md().splitlines() if line.startswith(f"| {marker} |")
    ]
    assert len(rows) == 1, (
        f"SECURITY.md: {marker} debería ser una única fila, hay {len(rows)}"
    )
    return flatten(rows[0])


def assert_says(where: str, text: str, fragments: Iterable[str]) -> None:
    for fragment in fragments:
        assert fragment in text, f"{where}: falta «{fragment}»"


def assert_silent(where: str, text: str, fragments: Iterable[str]) -> None:
    for fragment in fragments:
        assert fragment not in text, (
            f"{where}: reapareció la frase retirada «{fragment}»"
        )


def test_t2_names_the_in_vm_origin() -> None:
    row = threat_row("T2")
    assert_says(
        "SECURITY.md T2",
        row,
        (
            "desde dentro de la propia VM",
            "`rayd` escucha en `0.0.0.0:9000`",
            "cualquier proceso uid 1000 alcanza las seis rutas por loopback",
            "un `/terminate` falso se lleva la VM",
            "un `/validate` falso reinicia el contexto `default` del kernel",
            "acota sólo el origen externo",
            "queda pendiente",
        ),
    )
    assert_silent("SECURITY.md T2", row, ("el kernel no se reinicia",))

    origin = paragraph(architecture_md(), "**Origen de los hooks")
    assert_says(
        "ARCHITECTURE.md «Origen de los hooks»",
        origin,
        (
            "No acota el de dentro de la VM",
            "`0.0.0.0:9000`",
            "un proceso uid 1000 alcanza los hooks por loopback",
            "queda pendiente",
        ),
    )

    adr = section(architecture_md(), "## ADR-006")
    assert_says(
        "ARCHITECTURE.md ADR-006",
        adr,
        (
            "acota el origen **externo** y sólo ése",
            "cualquier proceso uid 1000 de dentro de la VM",
            "`0.0.0.0:9000`",
            "queda pendiente",
        ),
    )


def test_audit_scope_is_runtime_hooks() -> None:
    row = threat_row("T2")
    assert_says(
        "SECURITY.md T2",
        row,
        (
            "`hook_audit` de cada hook de runtime",
            "`/ready` y `/validate` son hooks de build",
            "no se auditan",
        ),
    )
    assert_silent("SECURITY.md T2", row, ("de cada hook tras", "cada hook se audita"))

    origin = paragraph(architecture_md(), "**Origen de los hooks")
    assert_says(
        "ARCHITECTURE.md «Origen de los hooks»",
        origin,
        (
            "cada hook de runtime se audita",
            "`/ready` y `/validate` son hooks de build",
            "nunca llegan a `audit()`",
        ),
    )
    assert_silent(
        "ARCHITECTURE.md «Origen de los hooks»", origin, ("cada hook se audita",)
    )

    adr = section(architecture_md(), "## ADR-006")
    assert_says("ARCHITECTURE.md ADR-006", adr, ("sólo los hooks de runtime",))


def test_metadata_is_readable_from_inside_the_vm() -> None:
    row = threat_row("T4")
    assert_says(
        "SECURITY.md T4",
        row,
        (
            "sin credencial alguna, a la propia carga de trabajo del sandbox",
            "`0.0.0.0:8080`",
            "`Health` es el único RPC anónimo",
            "un proceso uid 1000 dentro del MicroVM lee `sandbox_id`",
            "Se acepta",
        ),
    )

    page = section(site_doc("security"), "## Qué no poner en `envs` ni en `metadata`")
    assert_says(
        "docs/site/docs/security.md «Qué no poner…»",
        page,
        (
            "el propio código que corre dentro del sandbox también, sin ninguna credencial",
            "`Health` es el único RPC anónimo",
            "`0.0.0.0:8080`",
        ),
    )


def test_prefix_is_not_a_tenant_boundary() -> None:
    row = threat_row("T15")
    assert_says(
        "SECURITY.md T15",
        row,
        (
            "no separa inquilinos",
            "no liga el `S3Location` al sandbox",
            "un execution role y un prefijo por inquilino",
            "queda pendiente",
        ),
    )

    page = section(site_doc("persistence"), "## `S3Prefix`")
    assert_says(
        "docs/site/docs/persistence.md «S3Prefix»",
        page,
        (
            "El prefijo no separa inquilinos",
            "no comprueba que el destino sea el del sandbox que lo pide",
            "un execution role y un prefijo por inquilino",
        ),
    )

    summary = section(site_doc("security"), "## Persistencia en S3 (T15)")
    assert_says(
        "docs/site/docs/security.md «Persistencia en S3 (T15)»",
        summary,
        ("no separa inquilinos", "no liga el destino al sandbox que lo pide"),
    )


def test_persistence_quickstart_stays_out_of_the_artifact_namespace() -> None:
    quickstart = section(site_doc("persistence"), "## Quickstart")
    assert_says(
        "docs/site/docs/persistence.md «Quickstart»",
        quickstart,
        (
            'prefix="rayito-home"',
            'prefix: "rayito-home"',
            "s3://mi-bucket/rayito-home/agente-7/home.tar.gz",
        ),
    )
    assert_silent(
        "docs/site/docs/persistence.md «Quickstart»",
        quickstart,
        ('prefix="rayito"', "s3://mi-bucket/rayito/"),
    )

    page = section(site_doc("persistence"), "## `S3Prefix`")
    assert_says(
        "docs/site/docs/persistence.md «S3Prefix»",
        page,
        (
            "tiene que coincidir con el `PersistencePrefix` del despliegue",
            "por defecto `rayito-home`",
            'El `prefix="rayito"` por defecto del SDK no sirve',
            "`rayito/images/*`",
            "el `*` de una política IAM atraviesa `/`",
            "cuyo primer segmento no sea `rayito`",
        ),
    )


def test_access_token_env_var_is_shared_by_create() -> None:
    tokens = section(site_doc("concepts"), "## Dos tokens")
    assert_says(
        "docs/site/docs/concepts.md «Dos tokens»",
        tokens,
        (
            "`RAYITO_ACCESS_TOKEN` existe, pero la leen las dos llamadas",
            "cada `create()` de ese proceso reutiliza el mismo secreto",
            "32 bytes frescos por sandbox",
            "`access_token=` / `accessToken`",
        ),
    )
    assert_silent(
        "docs/site/docs/concepts.md «Dos tokens»",
        tokens,
        ("(o exporta `RAYITO_ACCESS_TOKEN`)",),
    )

    row = threat_row("T4")
    assert_says(
        "SECURITY.md T4",
        row,
        (
            "`RAYITO_ACCESS_TOKEN` la leen también los `create()` de ambos SDKs",
            "uno de toda la flota del proceso",
            "el servidor MCP crea sus sandboxes sin pasar token",
        ),
    )


def test_caller_policy_is_the_publisher_policy() -> None:
    bullet = paragraph(security_md(), "- `CallerPolicy`")
    assert_says(
        "SECURITY.md `CallerPolicy`",
        bullet,
        (
            "la política del **publicador**",
            "`rayito image publish` / `prune`",
            "**no** la de un servidor de aplicación",
            "`infra/ci-oidc-role.yaml`",
        ),
    )
    assert_silent(
        "SECURITY.md `CallerPolicy`",
        bullet,
        ("la máquina que ejecuta el SDK", "lambda:DeleteMicrovmImage"),
    )


def test_oidc_trust_has_no_branch_component() -> None:
    trust = section(infra_readme(), "## e2e desde GitHub Actions")
    assert_says(
        "infra/README.md «e2e desde GitHub Actions»",
        trust,
        (
            "no lleva componente de rama",
            "en cualquier rama— que declare `environment: e2e`",
            "fijar sus deployment branches a `main`",
            "`GitHubRef`",
        ),
    )
    assert_silent(
        "infra/README.md «e2e desde GitHub Actions»",
        trust,
        ("otra rama, otro environment u otro repositorio no pueden asumirlo",),
    )

    steps = section(infra_readme(), "### Después de desplegar, en GitHub")
    assert_says(
        "infra/README.md «Después de desplegar, en GitHub»",
        steps,
        ("**deployment branches limitadas a `main`**", "no lleva componente de rama"),
    )


def test_retired_sentences_are_gone() -> None:
    for relative, sentences in RETIRED_SENTENCES.items():
        assert_silent(relative, flatten(read(relative)), sentences)
