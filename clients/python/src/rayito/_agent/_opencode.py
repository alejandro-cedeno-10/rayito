"""Adaptador de OpenCode del puerto `AgentRuntime` (`ai-agent-core`,
ADR-025, design.md §3). Puro: construye el `opencode.json`, el script de
bash de una ejecución y traduce el JSONL de `opencode run --format json`
(v1.18.34, `packages/opencode/src/cli/cmd/run.ts`) a eventos de Rayito.

Lo que el diseño verificó y este módulo respeta:

- La configuración se carga por directorio y de forma perezosa (F1): un
  `opencode serve` arrancado antes de conocer la pasarela sirve igual. Si
  la configuración cambió desde la última ejecución con `--attach`, el
  script llama a `POST /instance/dispose` para que se relea.
- Siempre `--title` (F4): sin él OpenCode pide un título a otro modelo.
- El prompt va por stdin (F8), nunca en argv; la contraseña del servidor
  residente va en `OPENCODE_SERVER_PASSWORD`, leída de su fichero dentro
  del propio script, nunca en argv ni en `envs`.
- Con `--attach` el código de salida no refleja `session.error` (F7): el
  fallo sale de los eventos `error`, y `finish()` sólo da `Done` con
  salida 0 y ningún `error` visto.

El script escribe además tres líneas propias, con `type` prefijado
`rayito.` para no chocar con las de OpenCode: `rayito.busy` (otra
ejecución tiene el cerrojo), `rayito.runtime_missing` (no hay `opencode` o
se pidió `attach=True` sin servidor) y `rayito.attached`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import quote

from rayito._agent._domain import (
    AgentPermissions,
    AgentSpec,
    McpLocal,
    ToolPermission,
)
from rayito._agent._events import (
    AgentEvent,
    AgentFailed,
    Done,
    Reasoning,
    StepFinished,
    StepStarted,
    Text,
    TokenUsage,
    ToolCall,
    truncate_tool_output,
)
from rayito._agent._runtime import (
    RunCommand,
    RunRequest,
    RuntimeFile,
    RuntimeFiles,
    RuntimeState,
    TemplateStep,
    WarmupStep,
)
from rayito._limits import (
    AGENT_STATE_DIR,
    DEFAULT_AGENT_WORKDIR,
    MODEL_CREDENTIAL_PLACEHOLDER,
    OPENCODE_SERVE_PORT,
    OPENCODE_SESSION_TITLE,
)
from rayito.exceptions import InvalidArgumentException

#: Directorio de OpenCode dentro de `AGENT_STATE_DIR`: configuración,
#: instrucciones, secreto del servidor residente y sha aplicado.
OPENCODE_STATE_DIR: Final = f"{AGENT_STATE_DIR}/opencode"
#: El `OPENCODE_CONFIG` de cada ejecución.
OPENCODE_CONFIG_PATH: Final = f"{OPENCODE_STATE_DIR}/opencode.json"
#: Las instrucciones de `AgentSpec.instructions`; nunca el `AGENTS.md` del
#: directorio de trabajo del usuario.
OPENCODE_INSTRUCTIONS_PATH: Final = f"{OPENCODE_STATE_DIR}/AGENTS.md"
#: Contraseña del `opencode serve` residente (F3), 0600, de uid 1000.
OPENCODE_SERVE_SECRET_PATH: Final = f"{OPENCODE_STATE_DIR}/serve.secret"
#: sha256 del `opencode.json` que el servidor residente cargó por última vez.
OPENCODE_APPLIED_SHA_PATH: Final = f"{OPENCODE_STATE_DIR}/applied.sha256"
#: Una ejecución a la vez por sandbox en la fase 1 (`flock -n`).
AGENT_RUN_LOCK_PATH: Final = f"{AGENT_STATE_DIR}/run.lock"
#: El servidor residente sólo escucha en loopback.
OPENCODE_SERVE_URL: Final = f"http://127.0.0.1:{OPENCODE_SERVE_PORT}"
#: Usuario fijo de la autenticación básica de `opencode serve` (F3).
OPENCODE_SERVE_USER: Final = "opencode"
#: `$schema` de la documentación de OpenCode (opencode.ai/docs/config).
OPENCODE_CONFIG_SCHEMA: Final = "https://opencode.ai/config.json"
#: El agente principal de OpenCode; los subagentes van a su lado.
OPENCODE_PRIMARY_AGENT: Final = "build"
#: Proveedor propio para `"openai-compatible"`, con el paquete que OpenCode
#: ya trae empaquetado (F10).
OPENAI_COMPATIBLE_PROVIDER_ID: Final = "rayito-openai"
OPENAI_COMPATIBLE_NPM: Final = "@ai-sdk/openai-compatible"
#: `AgentModel.provider` -> id de proveedor de OpenCode.
OPENCODE_PROVIDER_IDS: Final[Mapping[str, str]] = {
    "bedrock": "amazon-bedrock",
    "anthropic": "anthropic",
    "openai-compatible": OPENAI_COMPATIBLE_PROVIDER_ID,
}
#: Prefijo de ruta de la Messages API que el SDK de Anthropic añade a
#: `baseURL`.
ANTHROPIC_BASE_PATH: Final = "/v1"
#: Las cinco variables de la plantilla (docs/research/2026-10-agent-spike.md)
#: más `OPENCODE_DISABLE_CLAUDE_CODE` (F11): sin descargas, sin
#: autoactualización y sin leer `.claude/`.
OPENCODE_FLAG_ENVS: Final[Mapping[str, str]] = {
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
    "OPENCODE_PURE": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
}
#: Un `sessionID` de OpenCode (`ses_…`): sólo así se mete en un comando.
_SESSION_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
#: Segundos de `curl` al comprobar la salud del servidor residente.
_HEALTH_TIMEOUT_SECONDS: Final = 1
#: Segundos de `curl` para `dispose` y `abort`.
_CONTROL_TIMEOUT_SECONDS: Final = 5
#: Bytes aleatorios de la contraseña del servidor residente.
_SERVE_SECRET_BYTES: Final = 32


@dataclass
class OpenCodeState:
    """Lo que el adaptador acumula de una ejecución: sesión, pasos, tokens,
    si se conectó al servidor residente y el primer fallo visto."""

    session_id: str | None = None
    steps: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    attached: bool = False
    failed: AgentFailed | None = None
    ignored_lines: int = 0


def shell_quote(value: str) -> str:
    """Comillas simples de POSIX, siempre (también para cadenas seguras),
    para que el script sea idéntico byte a byte en Python y TypeScript."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def canonical_json(value: object) -> bytes:
    """JSON con claves ordenadas, sangría de 2 y salto final: la misma
    serialización que `canonicalJson` en TypeScript."""
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def files_sha256(files: Sequence[RuntimeFile]) -> str:
    """sha256 de `ruta NUL contenido NUL` de cada fichero, en orden."""
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.path.encode("utf-8") + b"\0" + item.data + b"\0")
    return digest.hexdigest()


def _join_url(base: str, path: str) -> str:
    return base.rstrip("/") + path


def _model_ref(provider_id: str, model_id: str) -> str:
    return f"{provider_id}/{model_id}"


def _permission(permissions: AgentPermissions) -> dict[str, object]:
    rules: dict[str, object] = {"*": permissions.default}
    for tool, rule in permissions.effective_tools().items():
        rules[tool] = _permission_rule(rule)
    return rules


def _permission_rule(rule: ToolPermission) -> object:
    return rule if isinstance(rule, str) else dict(rule)


def _provider(spec: AgentSpec, gateway_url: str) -> dict[str, object]:
    model = spec.model
    if model.provider == "bedrock":
        return {"options": {"region": model.region, "endpoint": gateway_url}}
    if model.provider == "anthropic":
        return {
            "options": {
                "baseURL": _join_url(gateway_url, ANTHROPIC_BASE_PATH),
                "apiKey": MODEL_CREDENTIAL_PLACEHOLDER,
            }
        }
    model_ids = {model.id, spec.effective_small_model}
    model_ids.update(sub.model for sub in spec.agents.values() if sub.model is not None)
    return {
        "npm": OPENAI_COMPATIBLE_NPM,
        "options": {
            "baseURL": _join_url(gateway_url, model.base_path),
            "apiKey": MODEL_CREDENTIAL_PLACEHOLDER,
        },
        "models": {model_id: {} for model_id in sorted(model_ids)},
    }


def _mcp(spec: AgentSpec, gateway_urls: Mapping[str, str]) -> dict[str, object]:
    servers: dict[str, object] = {}
    for name, server in spec.mcp.items():
        if isinstance(server, McpLocal):
            entry: dict[str, object] = {
                "type": "local",
                "command": list(server.command),
                "enabled": True,
            }
            if server.envs:
                entry["environment"] = dict(server.envs)
            if server.timeout_seconds is not None:
                entry["timeout"] = round(server.timeout_seconds * 1000)
            servers[name] = entry
        else:
            servers[name] = {
                "type": "remote",
                "url": _join_url(gateway_urls[server.gateway], server.path),
                "enabled": True,
            }
    return servers


def _agents(spec: AgentSpec, provider_id: str) -> dict[str, object]:
    agents: dict[str, object] = {
        OPENCODE_PRIMARY_AGENT: {"permission": _permission(spec.permissions)}
    }
    for name, sub in spec.agents.items():
        entry: dict[str, object] = {
            "mode": "subagent",
            "description": sub.description,
            "prompt": sub.instructions,
        }
        if sub.model is not None:
            entry["model"] = _model_ref(provider_id, sub.model)
        if sub.permissions is not None:
            entry["permission"] = _permission(sub.permissions)
        agents[name] = entry
    return agents


def _deep_merge(base: dict[str, object], extra: Mapping[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in extra.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def build_opencode_config(spec: AgentSpec, *, gateway_urls: Mapping[str, str]) -> dict[str, object]:
    """El `opencode.json` de `spec` como diccionario (design.md §3)."""
    spec.require_gateways(gateway_urls.keys())
    provider_id = OPENCODE_PROVIDER_IDS[spec.model.provider]
    config: dict[str, object] = {
        "$schema": OPENCODE_CONFIG_SCHEMA,
        "model": _model_ref(provider_id, spec.model.id),
        "small_model": _model_ref(provider_id, spec.effective_small_model),
        "autoupdate": False,
        "share": "disabled",
        "snapshot": False,
        "enabled_providers": [provider_id],
        "provider": {provider_id: _provider(spec, gateway_urls[spec.model.gateway])},
        "agent": _agents(spec, provider_id),
    }
    if spec.instructions is not None:
        config["instructions"] = [OPENCODE_INSTRUCTIONS_PATH]
    if spec.mcp:
        config["mcp"] = _mcp(spec, gateway_urls)
    if spec.raw_config:
        config = _deep_merge(config, spec.raw_config)
    return config


def _failed(reason: str, state: OpenCodeState, **extra: object) -> AgentFailed:
    return AgentFailed(reason=reason, session_id=state.session_id, **extra)  # type: ignore[arg-type]


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage(tokens: object) -> TokenUsage:
    if not isinstance(tokens, Mapping):
        return TokenUsage()
    cache = tokens.get("cache")
    cache = cache if isinstance(cache, Mapping) else {}
    return TokenUsage(
        input=_int(tokens.get("input")),
        output=_int(tokens.get("output")),
        reasoning=_int(tokens.get("reasoning")),
        cache_read=_int(cache.get("read")),
        cache_write=_int(cache.get("write")),
    )


def _tool_call(part: Mapping[str, object]) -> ToolCall | None:
    tool_state = part.get("state")
    if not isinstance(tool_state, Mapping):
        return None
    status = tool_state.get("status")
    if status not in ("completed", "error"):
        return None
    raw_input = tool_state.get("input")
    raw_output = tool_state.get("output") if status == "completed" else tool_state.get("error")
    output, truncated = (None, False)
    if isinstance(raw_output, str):
        output, truncated = truncate_tool_output(raw_output)
    return ToolCall(
        call_id=str(part.get("callID", "")),
        name=str(part.get("tool", "")),
        status="completed" if status == "completed" else "error",
        input=dict(raw_input) if isinstance(raw_input, Mapping) else None,
        output=output,
        output_truncated=truncated,
    )


class OpenCodeRuntime:
    """`AgentRuntime` de OpenCode. Sin estado propio: lo de cada ejecución
    vive en el `OpenCodeState` que devuelve `new_state()`."""

    name: str = "opencode"

    def build_config(
        self, spec: AgentSpec, *, gateway_urls: Mapping[str, str], workdir: str
    ) -> RuntimeFiles:
        """`opencode.json` y, si hay `instructions`, su `AGENTS.md`.
        `workdir` no entra en la configuración: va en `--dir`."""
        del workdir
        files = [
            RuntimeFile(
                OPENCODE_CONFIG_PATH,
                canonical_json(build_opencode_config(spec, gateway_urls=gateway_urls)),
            )
        ]
        if spec.instructions is not None:
            files.append(RuntimeFile(OPENCODE_INSTRUCTIONS_PATH, spec.instructions.encode()))
        return RuntimeFiles(files=tuple(files), config_sha256=files_sha256(files))

    def command(self, request: RunRequest) -> RunCommand:
        """El script de una ejecución (cerrojo, decisión de `attach`,
        `dispose` si la configuración cambió, `exec opencode run`)."""
        provider_id = OPENCODE_PROVIDER_IDS[request.spec.model.provider]
        args = [
            "opencode",
            "run",
            "--format",
            "json",
            "--auto",
            "--title",
            OPENCODE_SESSION_TITLE,
            "--dir",
            request.workdir,
        ]
        if request.model is not None:
            args += ["-m", _model_ref(provider_id, request.model)]
        if request.session_id is not None:
            args += ["-s", request.session_id]
        if request.reasoning:
            args.append("--thinking")
        return RunCommand(
            script=_run_script(
                _attach_mode(request.attach), " ".join(shell_quote(a) for a in args), request
            ),
            envs=_run_envs(request.spec),
            stdin=request.prompt.encode("utf-8"),
        )

    def new_state(self) -> RuntimeState:
        return OpenCodeState()

    def parse_line(self, line: bytes, state: RuntimeState) -> Sequence[AgentEvent]:
        """Una línea JSONL de OpenCode (o del script) a cero o más eventos.
        Las líneas que no son JSON o tienen un `type` desconocido se ignoran
        y se cuentan en `ignored_lines`."""
        assert isinstance(state, OpenCodeState)
        try:
            payload = json.loads(line)
        except ValueError:
            state.ignored_lines += 1
            return ()
        if not isinstance(payload, Mapping):
            state.ignored_lines += 1
            return ()
        session_id = payload.get("sessionID")
        if isinstance(session_id, str) and session_id:
            state.session_id = session_id
        events = self._map(payload, state)
        if events is None:
            state.ignored_lines += 1
            return ()
        return events

    def _map(self, payload: Mapping[str, object], state: OpenCodeState) -> list[AgentEvent] | None:
        kind = payload.get("type")
        part = payload.get("part")
        part = part if isinstance(part, Mapping) else {}
        if kind == "rayito.attached":
            state.attached = True
            return []
        if kind in ("rayito.busy", "rayito.runtime_missing"):
            state.failed = _failed(str(kind).removeprefix("rayito."), state)
            return [state.failed]
        if kind == "step_start":
            state.steps += 1
            return [StepStarted(index=state.steps)]
        if kind == "step_finish":
            usage = _usage(part.get("tokens"))
            state.usage = state.usage + usage
            reason = part.get("reason")
            return [
                StepFinished(
                    index=state.steps,
                    usage=usage,
                    finish_reason=reason if isinstance(reason, str) else None,
                )
            ]
        if kind in ("text", "reasoning"):
            text = part.get("text")
            if not isinstance(text, str):
                return None
            return [Text(text=text) if kind == "text" else Reasoning(text=text)]
        if kind == "tool_use":
            call = _tool_call(part)
            return None if call is None else [call]
        if kind == "error":
            error = payload.get("error")
            name = error.get("name") if isinstance(error, Mapping) else None
            state.failed = _failed("model_error", state, detail_code=name)
            return [state.failed]
        return None

    def finish(self, state: RuntimeState, exit_code: int) -> Done | AgentFailed:
        """`Done` sólo con salida 0, ningún `error` y una sesión vista (F7)."""
        assert isinstance(state, OpenCodeState)
        if state.failed is not None:
            return state.failed
        if exit_code != 0:
            return _failed("runtime_error", state, exit_code=exit_code)
        if state.session_id is None:
            return _failed("protocol_error", state, exit_code=exit_code)
        return Done(session_id=state.session_id, exit_code=exit_code, usage=state.usage)

    def abort_command(self, state: RuntimeState) -> str | None:
        """Con `--attach`, `POST /session/<id>/abort` al servidor residente
        antes de matar el proceso; sin él, nada (basta con matarlo)."""
        assert isinstance(state, OpenCodeState)
        if not state.attached or state.session_id is None:
            return None
        if not _SESSION_ID_PATTERN.fullmatch(state.session_id):
            return None
        return (
            f"curl -fsS -m {_CONTROL_TIMEOUT_SECONDS} "
            f'-u "{OPENCODE_SERVE_USER}:$(cat {shell_quote(OPENCODE_SERVE_SECRET_PATH)})" '
            f"-X POST {shell_quote(f'{OPENCODE_SERVE_URL}/session/{state.session_id}/abort')}"
            " >/dev/null 2>&1 || true"
        )

    def template_steps(self) -> Sequence[TemplateStep]:
        """Vacío: la plantilla de agente (`ai-agent-fast-start`) declara sus
        propios pasos."""
        return ()

    def warmup_steps(self, *, serve: bool) -> Sequence[WarmupStep]:
        """Sin `serve`, sólo carga el binario en la caché de páginas. Con
        `serve`, además arranca `opencode serve` residente en loopback con
        una contraseña aleatoria que sólo uid 1000 puede leer."""
        steps = [WarmupStep(cmd="opencode --version >/dev/null")]
        if serve:
            steps.append(WarmupStep(cmd=_serve_script(), background=True, tag="rayito-agent-serve"))
        return tuple(steps)


def _attach_mode(attach: object) -> str:
    """`True` -> `always`, `False` -> `never`, `"auto"` -> `auto`."""
    if attach is True:
        return "always"
    if attach is False:
        return "never"
    if attach == "auto":
        return "auto"
    raise InvalidArgumentException("attach debe ser True, False o 'auto'")


def _run_envs(spec: AgentSpec) -> dict[str, str]:
    envs = {"HOME": DEFAULT_AGENT_WORKDIR, "OPENCODE_CONFIG": OPENCODE_CONFIG_PATH}
    envs.update(OPENCODE_FLAG_ENVS)
    if spec.model.provider == "bedrock":
        envs["AWS_BEARER_TOKEN_BEDROCK"] = MODEL_CREDENTIAL_PLACEHOLDER
    if spec.model.region is not None:
        envs["AWS_REGION"] = spec.model.region
    return envs


def _protocol_line(kind: str) -> str:
    return f"printf '%s\\n' '{{\"type\":\"rayito.{kind}\"}}'"


def _run_script(attach: str, command: str, request: RunRequest) -> str:
    auth = f'"{OPENCODE_SERVE_USER}:$OPENCODE_SERVER_PASSWORD"'
    dispose = f"{OPENCODE_SERVE_URL}/instance/dispose?directory={quote(request.workdir, safe='')}"
    lines = [
        "set -u",
        f"mkdir -p {shell_quote(OPENCODE_STATE_DIR)}",
        f"exec 9>{shell_quote(AGENT_RUN_LOCK_PATH)}",
        f"if ! flock -n 9; then {_protocol_line('busy')}; exit 0; fi",
        f"if ! command -v opencode >/dev/null 2>&1; then {_protocol_line('runtime_missing')}; "
        "exit 0; fi",
        f"attach={attach}",
        "attached=0",
        f"secret={shell_quote(OPENCODE_SERVE_SECRET_PATH)}",
        'if [ "$attach" != never ] && [ -r "$secret" ]; then',
        '  OPENCODE_SERVER_PASSWORD="$(cat "$secret")"',
        "  export OPENCODE_SERVER_PASSWORD",
        f"  if curl -fsS -m {_HEALTH_TIMEOUT_SECONDS} -u {auth} "
        f"{shell_quote(OPENCODE_SERVE_URL + '/global/health')} >/dev/null 2>&1; then",
        "    attached=1",
        "  fi",
        "fi",
        'if [ "$attach" = always ] && [ "$attached" = 0 ]; then',
        f"  {_protocol_line('runtime_missing')}",
        "  exit 0",
        "fi",
        f"set -- {command}",
        'if [ "$attached" = 1 ]; then',
        '  sha="$(sha256sum "$OPENCODE_CONFIG" | cut -d " " -f 1)"',
        f'  if [ "$sha" != "$(cat {shell_quote(OPENCODE_APPLIED_SHA_PATH)} 2>/dev/null)" ]; then',
        f"    curl -fsS -m {_CONTROL_TIMEOUT_SECONDS} -u {auth} -X POST "
        f"{shell_quote(dispose)} >/dev/null 2>&1 || true",
        f"    printf '%s\\n' \"$sha\" > {shell_quote(OPENCODE_APPLIED_SHA_PATH)}",
        "  fi",
        f"  {_protocol_line('attached')}",
        f'  set -- "$@" --attach {shell_quote(OPENCODE_SERVE_URL)}',
        "fi",
        'exec "$@"',
    ]
    return "\n".join(lines) + "\n"


def _serve_script() -> str:
    secret = shell_quote(OPENCODE_SERVE_SECRET_PATH)
    envs = " ".join(f"{name}={value}" for name, value in OPENCODE_FLAG_ENVS.items())
    lines = [
        "set -u",
        f"mkdir -p {shell_quote(OPENCODE_STATE_DIR)}",
        f"umask 077 && head -c {_SERVE_SECRET_BYTES} /dev/urandom | base64 > {secret}",
        f'OPENCODE_SERVER_PASSWORD="$(cat {secret})" '
        f"OPENCODE_CONFIG={shell_quote(OPENCODE_CONFIG_PATH)} {envs} "
        f"exec opencode serve --hostname 127.0.0.1 --port {OPENCODE_SERVE_PORT}",
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "OPENCODE_CONFIG_PATH",
    "OPENCODE_INSTRUCTIONS_PATH",
    "OPENCODE_SERVE_SECRET_PATH",
    "OpenCodeRuntime",
    "OpenCodeState",
    "build_opencode_config",
    "canonical_json",
    "files_sha256",
    "shell_quote",
]
