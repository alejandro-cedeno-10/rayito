"""Lo común a `test_agent_project_*.py`: los casos por runtime, las dos
imágenes desechables y el `AgentProject` de cada caso.

Variables de entorno, además de `RAYITO_E2E=1` y `RAYITO_TEMPLATE`:

- `RAYITO_E2E_BEDROCK_SECRET`: secreto que guarda `Bearer <clave de API de
  Bedrock de corta duración>`; sin él se salta todo.
- `RAYITO_E2E_AGENT_PROJECT_PREFIX` y `RAYITO_E2E_AGENT_PROJECT_BUCKET`: si
  están, la sesión construye las imágenes `<prefijo>-py` y `<prefijo>-cli`
  con `AgentTemplate` y el código de `testdata/agent-project-e2e/`. El
  prefijo lleva el id de la ejecución y el carril (`rayito-v014-<id>-pylane`)
  para que dos ejecuciones nunca compartan imagen.
- `RAYITO_E2E_AGENT_PROJECT_BASE` (opcional): imagen base con capabilities de
  la que parten las dos imágenes; sin ella, la que fija `AgentTemplate`.
- `RAYITO_E2E_AGENT_PROJECT_PY_TEMPLATE` y
  `RAYITO_E2E_AGENT_PROJECT_CLI_TEMPLATE`: imágenes ya construidas, en lugar
  de las dos variables anteriores.
- `RAYITO_E2E_ANTHROPIC_SECRET` (opcional): secreto con la clave de la API de
  Anthropic, para Hermes si su matriz de proveedores no admite Bedrock.
- `RAYITO_E2E_BEDROCK_MODEL` y `RAYITO_E2E_BEDROCK_REGION`.

Las imágenes y los despliegues los borra quien lanza la ejecución, por
inventario antes y después; los tests sólo paran lo que crean.
"""

from __future__ import annotations

import base64
import os
import secrets
import shlex
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Literal

from rayito import (
    AgentLimits,
    AgentModel,
    AgentProject,
    AgentSpec,
    AgentTemplate,
    CustomRuntime,
    DeepAgents,
    HermesRuntime,
    ServeOptions,
    StrandsRuntime,
    bedrock_gateway,
)
from rayito._agent._recipes import AGENT_APP_DIR
from rayito._agent._runtime import AgentRuntime
from rayito.exceptions import InvalidArgumentException

REPO_ROOT_DEPTH: Final = 4
FIXTURES: Final = (
    Path(__file__).resolve().parents[REPO_ROOT_DEPTH] / "testdata" / "agent-project-e2e"
)
PREFIX_VAR: Final = "RAYITO_E2E_AGENT_PROJECT_PREFIX"
BUCKET_VAR: Final = "RAYITO_E2E_AGENT_PROJECT_BUCKET"
BASE_VAR: Final = "RAYITO_E2E_AGENT_PROJECT_BASE"
PY_TEMPLATE_VAR: Final = "RAYITO_E2E_AGENT_PROJECT_PY_TEMPLATE"
CLI_TEMPLATE_VAR: Final = "RAYITO_E2E_AGENT_PROJECT_CLI_TEMPLATE"
BEDROCK_SECRET_VAR: Final = "RAYITO_E2E_BEDROCK_SECRET"
ANTHROPIC_SECRET_VAR: Final = "RAYITO_E2E_ANTHROPIC_SECRET"
DEFAULT_BEDROCK_MODEL: Final = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_BEDROCK_REGION: Final = "us-east-1"
ANTHROPIC_MODEL: Final = "claude-haiku-4-5"
BEDROCK_MODEL: Final = os.environ.get("RAYITO_E2E_BEDROCK_MODEL") or DEFAULT_BEDROCK_MODEL
BEDROCK_REGION: Final = os.environ.get("RAYITO_E2E_BEDROCK_REGION") or DEFAULT_BEDROCK_REGION
GATEWAY_NAME: Final = "model"
#: El comando propio corre con el directorio de trabajo del proyecto, no con el
#: del código horneado en la imagen: el script se nombra con su ruta absoluta.
CUSTOM_SCRIPT: Final = f"{AGENT_APP_DIR}/run.sh"
PROMPT: Final = "Reply with exactly one short sentence."
RUN_TIMEOUT_SECONDS: Final = 300
DEPLOY_TIMEOUT_SECONDS: Final = 1200
MAX_STEPS: Final = 8
PY_RUNTIMES: Final = ("deepagents", "strands", "strands-harness")
CLI_RUNTIMES: Final = ("opencode", "hermes")

ImageKind = Literal["py", "cli"]


@dataclass(frozen=True)
class RuntimeCase:
    """Un runtime a probar: en qué imagen vive y cómo se construye su objeto."""

    id: str
    image: ImageKind
    runtime: Callable[[], AgentRuntime | str]
    calls_the_model: bool = True
    runtime_toml: str = ""


CASES: Final = (
    RuntimeCase("deepagents", "py", lambda: "deepagents", runtime_toml='kind = "deepagents"'),
    RuntimeCase(
        "langgraph",
        "py",
        lambda: DeepAgents(entrypoint="graph:build"),
        runtime_toml='kind = "langgraph"\nentrypoint = "graph:build"',
    ),
    RuntimeCase(
        "strands",
        "py",
        lambda: StrandsRuntime(entrypoint="agent:agent"),
        runtime_toml='kind = "strands"\nentrypoint = "agent:agent"',
    ),
    RuntimeCase(
        "strands-harness",
        "py",
        lambda: StrandsRuntime(harness=True),
        runtime_toml='kind = "strands-harness"',
    ),
    RuntimeCase(
        "custom",
        "py",
        lambda: CustomRuntime(command=["bash", CUSTOM_SCRIPT], protocol="text"),
        False,
        runtime_toml=f'kind = "custom"\ncommand = ["bash", "{CUSTOM_SCRIPT}"]\nprotocol = "text"',
    ),
    RuntimeCase("opencode", "cli", lambda: "opencode", runtime_toml='kind = "opencode"'),
    RuntimeCase("hermes", "cli", lambda: HermesRuntime(), runtime_toml='kind = "hermes"'),
    RuntimeCase(
        "strands-node",
        "cli",
        lambda: StrandsRuntime(entrypoint="agent.ts", language="typescript"),
        runtime_toml='kind = "strands"\nlanguage = "typescript"\nentrypoint = "agent.ts"',
    ),
)


def image_runtimes(kind: ImageKind) -> tuple[AgentRuntime | str, ...]:
    if kind == "py":
        return (*PY_RUNTIMES,)
    return (*CLI_RUNTIMES, StrandsRuntime(entrypoint="agent.ts", language="typescript"))


def app_fixture(kind: ImageKind) -> Path:
    """El código de prueba del tipo de imagen: `py` o, para `cli`, `ts`."""
    return FIXTURES / ("py" if kind == "py" else "ts")


def image_name(kind: ImageKind) -> str | None:
    """El nombre de la imagen del caso: el de la variable, o `<prefijo>-<tipo>`."""
    explicit = os.environ.get(PY_TEMPLATE_VAR if kind == "py" else CLI_TEMPLATE_VAR)
    if explicit:
        return explicit
    prefix = os.environ.get(PREFIX_VAR)
    return f"{prefix}-{kind}" if prefix else None


def build_image(kind: ImageKind, *, region: str) -> str:
    """Construye la imagen del tipo con `AgentTemplate` y el código de
    `testdata/agent-project-e2e/<tipo>`; devuelve su nombre. Sólo si el
    entorno trae el prefijo y el bucket."""
    name = image_name(kind)
    bucket = os.environ.get(BUCKET_VAR)
    if name is None or bucket is None:
        raise RuntimeError(f"faltan {PREFIX_VAR} y {BUCKET_VAR}")
    app = app_fixture(kind)
    base = os.environ.get(BASE_VAR)
    template = AgentTemplate(name=name, runtimes=image_runtimes(kind), app=str(app))
    if base:
        template = replace(template, base=base)
    template.build(bucket=bucket, region=region)
    return name


def provider_for(case: RuntimeCase) -> tuple[str, str, str]:
    """`(proveedor, modelo, nombre del secreto)` del caso. Hermes usa
    Anthropic si su matriz no admite Bedrock y hay clave de Anthropic."""
    bedrock = os.environ[BEDROCK_SECRET_VAR]
    if case.id != "hermes":
        return "bedrock", BEDROCK_MODEL, bedrock
    anthropic = os.environ.get(ANTHROPIC_SECRET_VAR)
    if anthropic is None:
        return "bedrock", BEDROCK_MODEL, bedrock
    return "anthropic", ANTHROPIC_MODEL, anthropic


def project_for(case: RuntimeCase, template: str) -> AgentProject:
    """El `AgentProject` del caso sobre una imagen ya construida: Bedrock (o
    Anthropic, para Hermes) sólo por la pasarela, egress cerrado."""
    provider, model, secret = provider_for(case)
    gateway: Any
    if provider == "bedrock":
        gateway = bedrock_gateway(secret, region=BEDROCK_REGION, models=[model])
        region: str | None = BEDROCK_REGION
    else:
        from rayito import anthropic_gateway

        gateway = anthropic_gateway(secret)
        region = None
    return AgentProject(
        name=f"e2e-{case.id}",
        runtime=case.runtime(),
        spec=AgentSpec(
            model=AgentModel(provider=provider, id=model, gateway=GATEWAY_NAME, region=region),  # type: ignore[arg-type]
            instructions="Answer with one short sentence.",
        ),
        gateways={GATEWAY_NAME: gateway},
        template=template,
        limits=AgentLimits(max_steps=MAX_STEPS, timeout_seconds=RUN_TIMEOUT_SECONDS),
        serve=ServeOptions(timeout_seconds=DEPLOY_TIMEOUT_SECONDS, idle=None),
    )


def unsupported(case: RuntimeCase, template: str) -> str | None:
    """El motivo si el proyecto del caso no se puede construir con este
    entorno (por ejemplo, Hermes sin Bedrock y sin clave de Anthropic)."""
    try:
        project_for(case, template)
    except InvalidArgumentException as exc:
        return str(exc)
    return None


ACCESS_TOKEN_VAR: Final = "RAYITO_ACCESS_TOKEN"
ACCESS_TOKEN_BYTES: Final = 32
TOOL_CASES: Final = ("deepagents", "langgraph", "strands-harness", "opencode", "hermes")
SANDBOX_WORKDIR: Final = "/home/user"
CREDENTIAL_PROBE_TIMEOUT_SECONDS: Final = 600
#: Donde un runtime escribiría una configuración o un registro; el código
#: instalado (paquetes de Python y de Node) no se recorre: son cientos de miles
#: de ficheros que el agente no genera.
CREDENTIAL_SEARCH_ROOTS: Final = ("/home", "/root", "/tmp", "/var", "/etc", "/run", "/srv", "/opt")
CREDENTIAL_SEARCH_SKIPPED: Final = ("site-packages", "node_modules")
TELEMETRY_OFF_SWITCHES: Final = {
    "OTEL_SDK_DISABLED": "true",
    "OTEL_TRACES_EXPORTER": "none",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_LOGS_EXPORTER": "none",
}
TELEMETRY_ENV_PREFIXES: Final = ("OTEL_", "LANGSMITH_", "LANGCHAIN_TRACING", "LANGCHAIN_API_KEY")


def env_dump_prompt(path: str) -> str:
    """El prompt que pide al agente volcar su entorno con su herramienta de shell."""
    return f"Run exactly `env > {path}` with your shell tool, then reply with the single word done."


def model_credential(region: str) -> str:
    """La clave de Bedrock del secreto del e2e, sin el prefijo `Bearer `. Sólo
    para comprobar que el agente no puede leerla: nunca se imprime."""
    from rayito import SecretCache, SecretStore

    cache = SecretCache(store=SecretStore(region=region))
    return cache.get(os.environ[BEDROCK_SECRET_VAR]).removeprefix("Bearer ")


def sandbox_exposes_key(sandbox: Any, key: str) -> list[str]:
    """Los sitios del sandbox donde se lee `key`: su entorno, el de `rayd`
    (`/proc/1/environ`, `/proc/1/mem`) y cualquier fichero legible. Vacío
    si no se lee en ninguno. Devuelve nombres de sitio, nunca la clave."""
    from rayito.exceptions import CommandExitException

    def sh(cmd: str) -> tuple[int, str]:
        try:
            result = sandbox.commands.run(cmd, timeout=CREDENTIAL_PROBE_TIMEOUT_SECONDS)
        except CommandExitException as exc:
            return exc.exit_code, exc.stdout
        return result.exit_code, result.stdout

    found: list[str] = []
    if key in sh("env")[1]:
        found.append("env")
    code, out = sh("cat /proc/1/environ 2>&1")
    if code == 0 or key in out:
        found.append("proc-1-environ")
    if sh("head -c 16 /proc/1/mem")[0] == 0:
        found.append("proc-1-mem")
    needle = shlex.quote(key[: len(key) // 2])
    roots = " ".join(CREDENTIAL_SEARCH_ROOTS)
    skipped = " ".join(f"--exclude-dir={name}" for name in CREDENTIAL_SEARCH_SKIPPED)
    _, files = sh(f"grep -rIlsF {needle} {roots} {skipped} 2>/dev/null | head -5")
    if files.split():
        found.append("readable-file")
    return found


def telemetry_env_names(env_dump: str) -> list[str]:
    """Los nombres de variable del volcado que encenderían trazas hacia fuera.
    Los interruptores que la apagan (`OTEL_SDK_DISABLED=true`, los
    `OTEL_*_EXPORTER=none` que pone Strands) no cuentan."""
    pairs = [line.split("=", 1) for line in env_dump.splitlines() if "=" in line]
    return [
        name
        for name, value in pairs
        if name.startswith(TELEMETRY_ENV_PREFIXES) and TELEMETRY_OFF_SWITCHES.get(name) != value
    ]


def project_file_text(case: RuntimeCase, template: str) -> str:
    """El `rayito-agent.toml` del caso: el mismo proyecto que `project_for`,
    sobre la imagen ya construida, para las pruebas de la CLI. Sólo Bedrock
    (Hermes sin Bedrock se salta antes, con `unsupported`). `[source]` apunta
    al propio directorio, que lleva el código del caso (`app_fixture`):
    `deploy` reconstruye la imagen desde ahí y, sin código, el punto de
    entrada no existiría."""
    _, model, secret = provider_for(case)
    return (
        'schema = "rayito.agent-project/1"\n'
        f'name = "e2e-{case.id}"\n\n'
        f"[runtime]\n{case.runtime_toml}\n\n"
        '[source]\npath = "."\n\n'
        f'[model]\nprovider = "bedrock"\nid = "{model}"\nregion = "{BEDROCK_REGION}"\n'
        f'gateway = "{GATEWAY_NAME}"\n\n'
        f'[gateways.{GATEWAY_NAME}]\npreset = "bedrock"\nsecret = "{secret}"\n'
        f'region = "{BEDROCK_REGION}"\nmodels = ["{model}"]\n\n'
        '[agent]\ninstructions = "Answer with one short sentence."\n\n'
        f"[limits]\nmax_steps = {MAX_STEPS}\ntimeout_seconds = {RUN_TIMEOUT_SECONDS}\n\n"
        f"[serve]\ntimeout_seconds = {DEPLOY_TIMEOUT_SECONDS}\nidle_seconds = 0\n\n"
        f'[image]\ntemplate = "{template}"\n'
    )


def new_access_token() -> str:
    """Un access token de sandbox nuevo (base64url de 32 bytes). Con él en
    `RAYITO_ACCESS_TOKEN`, el sandbox de un `deploy()` lo usa y otro proceso
    (`AgentDeployment.connect`, `Sandbox.connect`, la CLI) puede reconectarse."""
    raw = secrets.token_bytes(ACCESS_TOKEN_BYTES)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
