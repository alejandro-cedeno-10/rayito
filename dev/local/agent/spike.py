"""Driver del spike de agentes en el entorno local
(docs/research/2026-10-agent-spike.md). Corre dentro del contenedor
`runner` con el guest de `dev/local/agent/compose.yaml`:

    python3 mint_bedrock_key.py | docker compose -f dev/local/compose.yaml \\
      -f dev/local/agent/compose.yaml exec -T runner bash -c \\
      'cd clients/python && uv run --no-sync python /src/dev/local/agent/spike.py'

Lee por stdin una clave de API de Bedrock de corta duración (nunca por
argumento ni variable de entorno, y nunca la imprime), la guarda en el
Secrets Manager emulado por Floci y crea un sandbox con:

- la pasarela de secretos hacia `bedrock-runtime` (la clave entra como
  cabecera `authorization` sólo en `rayd`);
- `allow_internet_access=False`: deny-all de egress para uid 1000-65535,
  aplicado de verdad porque el guest del spike tiene CAP_NET_ADMIN.

Dentro del sandbox comprueba que OpenCode y deepagents trabajan a través de
la pasarela, que el código del sandbox no ve la clave y que no sale a
Internet por su cuenta. Imprime un JSON con resultados y tiempos; la clave
sólo se usa para buscarla (sí/no) en lo que el sandbox devuelve.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Final

import boto3
from rayito import Sandbox, SecretCache, SecretGateway, SecretStore
from rayito._aws import LambdaMicrovmsControlPlane
from rayito.exceptions import CommandExitException, SandboxNotFoundException

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from tests.local.conftest import (
    DEFAULT_ARTIFACT_BUCKET,
    seed_local_image,
)
from tests.local.guest import (
    LOCAL_GUEST_VAR,
    GuestAddress,
    LocalGuestControlPlane,
)

REGION: Final = os.environ.get("AWS_REGION", "us-east-1")
#: Perfil de inferencia de sistema de Claude Haiku 4.5 (el más barato de los
#: Claude activos en la cuenta de pruebas; `bedrock list-inference-profiles`,
#: 2026-10-05).
BEDROCK_MODEL: Final = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
BEDROCK_UPSTREAM: Final = f"https://bedrock-runtime.{REGION}.amazonaws.com"
#: Converse, ConverseStream, InvokeModel e InvokeModelWithResponseStream
#: cuelgan todas de `/model/{modelId}/...`.
BEDROCK_ALLOW: Final = [("POST", "/model/*")]
GATEWAY_NAME: Final = "bedrock"
SECRET_NAME: Final = "spike-bedrock-key"
#: Lo que el proceso del sandbox pone como token: botocore y el SDK de AI de
#: OpenCode necesitan *algún* valor para elegir la autenticación bearer; la
#: pasarela quita su cabecera `authorization` y pone la real.
PLACEHOLDER_TOKEN: Final = "placeholder-not-a-secret"
SANDBOX_TIMEOUT_SECONDS: Final = 1800
AGENT_TIMEOUT_SECONDS: Final = 300
EGRESS_PROBE_TIMEOUT_SECONDS: Final = 5
TTFT_SAMPLES: Final = 5
OPENCODE_RUNS: Final = 3
HOME: Final = "/home/user"
WORKDIR: Final = f"{HOME}/work"
TOOLS_DIR: Final = f"{HOME}/.spike"
DEEPAGENTS_PYTHON: Final = "/opt/agents/deepagents/bin/python"
OPENCODE_ENV: Final = {
    "HOME": HOME,
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
    "OPENCODE_PURE": "1",
    "OPENCODE_CONFIG": f"{WORKDIR}/opencode.json",
    "AWS_BEARER_TOKEN_BEDROCK": PLACEHOLDER_TOKEN,
    "AWS_REGION": REGION,
}
OPENCODE_PROMPT: Final = (
    "Lista los ficheros de este directorio con tu herramienta de listar, "
    "después crea el fichero saludo.txt con el texto 'hola desde opencode' "
    "y responde sólo 'listo'."
)
OPENCODE_TEXT_MARKER: Final = '"type":"text"'
HERE: Final = Path(__file__).resolve().parent
IN_SANDBOX_TOOLS: Final = ("measure.py", "ttft.py", "deepagents_task.py")
EGRESS_TARGETS: Final = ("https://example.com", BEDROCK_UPSTREAM)
#: Una IP pública literal (el resolvedor de Cloudflare) para probar la ruta
#: directa sin DNS ni el proxy local que `rayd` anuncia en `HTTPS_PROXY`.
DIRECT_IP_TARGET: Final = "https://1.1.1.1"
PROXY_VARS: Final = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")


def opencode_config(gateway_url: str) -> str:
    """`opencode.json`: el proveedor `amazon-bedrock` con `endpoint` en la
    pasarela (OpenCode lo pasa como `baseURL` al SDK de AI)."""
    return json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "model": f"amazon-bedrock/{BEDROCK_MODEL}",
            "autoupdate": False,
            "share": "disabled",
            "provider": {
                "amazon-bedrock": {
                    "options": {"region": REGION, "endpoint": gateway_url}
                }
            },
        },
        indent=1,
    )


def run(sbx: Sandbox, cmd: str, **kwargs: Any) -> tuple[int, str, str]:
    """Una orden como `user`; devuelve (exit, stdout, stderr) sin lanzar."""
    kwargs.setdefault("timeout", AGENT_TIMEOUT_SECONDS)
    try:
        result = sbx.commands.run(cmd, **kwargs)
    except CommandExitException as exc:
        return exc.exit_code, exc.stdout, exc.stderr
    return result.exit_code, result.stdout, result.stderr


def last_json(text: str) -> Any:
    lines = [line for line in text.splitlines() if line.startswith("{")]
    return json.loads(lines[-1]) if lines else None


def upload_tools(sbx: Sandbox) -> None:
    for name in IN_SANDBOX_TOOLS:
        sbx.files.write(
            f"{TOOLS_DIR}/{name}", (HERE / name).read_text(encoding="utf-8")
        )


def check_opencode(sbx: Sandbox, gateway_url: str) -> dict[str, Any]:
    sbx.files.write(f"{WORKDIR}/opencode.json", opencode_config(gateway_url))
    sbx.files.write(f"{WORKDIR}/LEEME.md", "Directorio de trabajo del spike.\n")
    code, out, _ = run(sbx, "opencode --version", envs=OPENCODE_ENV)
    report: dict[str, Any] = {"version_exit": code, "version": out.strip()}
    runs = []
    for attempt in range(OPENCODE_RUNS):
        out_file = f"{TOOLS_DIR}/opencode-{attempt}.jsonl"
        cmd = (
            f"python3 {TOOLS_DIR}/measure.py {out_file} {shlex.quote(OPENCODE_TEXT_MARKER)} -- "
            f"opencode run --format json --auto {shlex.quote(OPENCODE_PROMPT)}"
        )
        run(sbx, f"rm -f {WORKDIR}/saludo.txt")
        _, out, err = run(sbx, cmd, envs=OPENCODE_ENV, cwd=WORKDIR)
        measured = last_json(out) or {"error_tail": err[-400:]}
        events = sbx.files.read(out_file)
        measured["tool_events"] = sorted(
            {
                event["part"]["tool"]
                for event in map(json.loads, filter(None, events.splitlines()))
                if event.get("type") == "tool_use"
            }
        )
        measured["file_written"] = sbx.files.exists(f"{WORKDIR}/saludo.txt")
        runs.append(measured)
    report["runs"] = runs
    return report


def check_deepagents(sbx: Sandbox, gateway_url: str) -> dict[str, Any]:
    envs = {
        "HOME": HOME,
        "AWS_BEARER_TOKEN_BEDROCK": PLACEHOLDER_TOKEN,
        "SPIKE_MODEL": BEDROCK_MODEL,
        "SPIKE_REGION": REGION,
        "SPIKE_GATEWAY_URL": gateway_url,
    }
    out_file = f"{TOOLS_DIR}/deepagents.out"
    cmd = (
        f"python3 {TOOLS_DIR}/measure.py {out_file} '\"seconds\"' -- "
        f"{DEEPAGENTS_PYTHON} {TOOLS_DIR}/deepagents_task.py {WORKDIR}/deepagents"
    )
    _, out, err = run(sbx, cmd, envs=envs)
    measured = last_json(out) or {"error_tail": err[-400:]}
    measured["agent"] = last_json(sbx.files.read(out_file))
    return measured


def check_ttft(sbx: Sandbox, gateway_url: str) -> dict[str, Any]:
    envs = {
        "HOME": HOME,
        "AWS_BEARER_TOKEN_BEDROCK": PLACEHOLDER_TOKEN,
        "SPIKE_MODEL": BEDROCK_MODEL,
        "SPIKE_REGION": REGION,
        "SPIKE_GATEWAY_URL": gateway_url,
    }
    _, out, err = run(
        sbx, f"{DEEPAGENTS_PYTHON} {TOOLS_DIR}/ttft.py {TTFT_SAMPLES}", envs=envs
    )
    return last_json(out) or {"error_tail": err[-400:]}


def check_isolation(sbx: Sandbox, secret_value: str) -> dict[str, Any]:
    """Lo que el código del sandbox puede ver: su entorno, el de `rayd`
    (PID 1), los ficheros legibles y la red directa. Sólo se devuelve si la
    clave aparece o no, nunca lo leído."""
    seen: dict[str, Any] = {}
    _, env_out, _ = run(sbx, "env")
    seen["key_in_own_env"] = secret_value in env_out
    code, out, err = run(sbx, "cat /proc/1/environ")
    seen["rayd_environ_readable"] = code == 0
    seen["key_in_rayd_environ"] = secret_value in out + err
    code, _, _ = run(sbx, "head -c 16 /proc/1/mem")
    seen["rayd_mem_readable"] = code == 0
    code, out, _ = run(sbx, "ls /proc/1/fd")
    seen["rayd_fds_listable"] = code == 0
    needle = secret_value[: len(secret_value) // 2]
    code, out, _ = run(
        sbx,
        f"grep -rlsF {shlex.quote(needle)} / --exclude-dir=proc --exclude-dir=sys "
        "--exclude-dir=dev 2>/dev/null | head -5",
        timeout=AGENT_TIMEOUT_SECONDS,
    )
    seen["files_with_key"] = len([line for line in out.splitlines() if line.strip()])
    egress: dict[str, int] = {}
    for target in EGRESS_TARGETS:
        code, _, _ = run(
            sbx,
            f"curl -sS -o /dev/null -m {EGRESS_PROBE_TIMEOUT_SECONDS} {target}",
        )
        egress[target] = code
    seen["direct_egress_curl_exit"] = egress
    code, _, _ = run(
        sbx,
        f"curl -sS -o /dev/null -m {EGRESS_PROBE_TIMEOUT_SECONDS} --noproxy '*' {DIRECT_IP_TARGET}",
    )
    seen["direct_ip_no_proxy_curl_exit"] = code
    seen["proxy_vars_present"] = sorted(
        name for name in PROXY_VARS if f"\n{name}=" in f"\n{env_out}"
    )
    code, _, _ = run(sbx, "getent hosts example.com")
    seen["dns_resolves"] = code == 0
    return seen


def main() -> int:
    secret_value = sys.stdin.read().strip()
    if not secret_value:
        print("falta la clave de Bedrock por stdin", file=sys.stderr)
        return 2
    address = GuestAddress.parse(os.environ[LOCAL_GUEST_VAR])
    session = boto3.session.Session(region_name=REGION)
    s3 = session.client("s3")
    with contextlib.suppress(s3.exceptions.BucketAlreadyOwnedByYou):
        s3.create_bucket(Bucket=DEFAULT_ARTIFACT_BUCKET)
    template_arn = seed_local_image(session, DEFAULT_ARTIFACT_BUCKET)
    plane = LocalGuestControlPlane(
        LambdaMicrovmsControlPlane.from_session(session, region=REGION), address
    )
    store = SecretStore(region=REGION)
    if store.exists(SECRET_NAME):
        store.update(SECRET_NAME, f"Bearer {secret_value}")
    else:
        store.create(SECRET_NAME, f"Bearer {secret_value}")
    report: dict[str, Any] = {}
    sbx = Sandbox.create(
        template_arn,
        control_plane=plane,
        transport=address.transport(),
        timeout=SANDBOX_TIMEOUT_SECONDS,
        allow_internet_access=False,
        gateways={
            GATEWAY_NAME: SecretGateway(
                upstream=BEDROCK_UPSTREAM,
                headers={"authorization": SECRET_NAME},
                allow=BEDROCK_ALLOW,
            )
        },
        secret_cache=SecretCache(),
    )
    try:
        report["egress_enforcement"] = str(sbx.get_health().egress_enforcement)
        gateway_url = sbx.gateways[GATEWAY_NAME].url
        upload_tools(sbx)
        report["isolation"] = check_isolation(sbx, secret_value)
        report["ttft"] = check_ttft(sbx, gateway_url)
        report["opencode"] = check_opencode(sbx, gateway_url)
        report["deepagents"] = check_deepagents(sbx, gateway_url)
        report["isolation_after"] = check_isolation(sbx, secret_value)
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            sbx.kill()
        store.destroy(SECRET_NAME)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
