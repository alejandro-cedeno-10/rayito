"""Adaptador de deepagents del puerto `AgentRuntime` (`ai-agent-deepagents`,
ADR-025, design.md §3). Puro: escribe la configuración JSON que lee el
runner (`_runner/deepagents_runner.py`, que la plantilla instala en
`DEEPAGENTS_RUNNER_PATH`), construye el script de una ejecución y traduce
el protocolo JSONL v1 de Rayito a eventos.

A diferencia de OpenCode, el runner ya habla en eventos de Rayito
(`text_delta`, `text`, `reasoning`, `tool_call`, `step_started`,
`step_finished`, `agent_failed`, `done`) más `session`; el adaptador sólo
los valida, recorta y acumula. El script escribe además `rayito.busy` y
`rayito.runtime_missing`, como el de OpenCode.

Los permisos de `AgentSpec` se traducen a los nombres de herramienta de
deepagents (`DEEPAGENTS_TOOL_NAMES`). Sólo `bash` admite patrones (sobre el
comando de `execute`); `mcp`, `raw_config` y `attach=True` no existen en
este runtime y fallan con `InvalidArgumentException` antes de cualquier
RPC.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from rayito._agent._domain import (
    DEFAULT_DENIED_TOOLS,
    AgentPermissions,
    AgentSpec,
    validate_model_id,
)
from rayito._agent._events import (
    AGENT_FAILURE_REASONS,
    AgentEvent,
    AgentFailed,
    Done,
    Reasoning,
    StepFinished,
    StepStarted,
    Text,
    TextDelta,
    TokenUsage,
    ToolCall,
    truncate_tool_output,
)
from rayito._agent._opencode import (
    AGENT_RUN_LOCK_PATH,
    canonical_json,
    files_sha256,
    shell_quote,
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
    AGENT_PROTOCOL_VERSION,
    AGENT_STATE_DIR,
    DEFAULT_AGENT_WORKDIR,
    MODEL_CREDENTIAL_PLACEHOLDER,
)
from rayito.exceptions import InvalidArgumentException

#: Estado de deepagents dentro de `AGENT_STATE_DIR`: configuración y sesiones.
DEEPAGENTS_STATE_DIR: Final = f"{AGENT_STATE_DIR}/deepagents"
#: La configuración estática que lee el runner (su único argumento).
DEEPAGENTS_CONFIG_PATH: Final = f"{DEEPAGENTS_STATE_DIR}/config.json"
#: Historial de cada sesión, `<id>.json` con `messages_to_dict`.
DEEPAGENTS_SESSIONS_DIR: Final = f"{DEEPAGENTS_STATE_DIR}/sessions"
#: El Python del venv de deepagents de la plantilla
#: (`dev/local/agent/Dockerfile`, docs/research/2026-10-agent-spike.md).
DEEPAGENTS_PYTHON: Final = "/opt/agents/deepagents/bin/python"
#: Donde la plantilla instala el runner, de root y 0755 (design.md §3).
DEEPAGENTS_RUNNER_PATH: Final = "/opt/agents/rayito/deepagents_runner.py"
#: Nombre de herramienta de `AgentPermissions` (el vocabulario de OpenCode)
#: -> herramientas de deepagents 0.7 (`create_deep_agent`).
DEEPAGENTS_TOOL_NAMES: Final[Mapping[str, tuple[str, ...]]] = {
    "read": ("read_file",),
    "edit": ("write_file", "edit_file"),
    "list": ("ls",),
    "glob": ("glob",),
    "grep": ("grep",),
    "bash": ("execute",),
    "task": ("task",),
    "todowrite": ("write_todos",),
}
#: La única herramienta cuyos patrones tienen sentido: se comparan con el
#: comando de `execute` (`fnmatch`).
PATTERN_TOOL: Final = "bash"
#: Variables de cada ejecución: sin `.pyc` en el directorio del usuario y
#: con el stdout sin búfer (los eventos llegan en cuanto ocurren).
DEEPAGENTS_FLAG_ENVS: Final[Mapping[str, str]] = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}
#: Tipos del protocolo v1 que el adaptador acepta del runner.
PROTOCOL_EVENT_TYPES: Final[tuple[str, ...]] = (
    "session",
    "text_delta",
    "text",
    "reasoning",
    "tool_call",
    "step_started",
    "step_finished",
    "agent_failed",
    "done",
)
#: Fallos que el runner puede declarar; el resto (`timeout`, `max_steps`…)
#: sólo los decide el SDK.
RUNNER_FAILURE_REASONS: Final[tuple[str, ...]] = (
    "model_error",
    "runtime_error",
    "protocol_error",
)
#: `pkg.mod:build`: un módulo importable desde el directorio de trabajo y
#: una función que recibe un `RunnerContext` y devuelve un grafo compilado.
_ENTRYPOINT_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*")
_SESSION_ID_PATTERN: Final = re.compile(r"rda_[0-9a-f]{32}")
_USAGE_KEYS: Final = ("input", "output", "reasoning", "cache_read", "cache_write")


@dataclass
class DeepAgentsState:
    """Lo que el adaptador acumula de una ejecución."""

    session_id: str | None = None
    steps: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    done: bool = False
    failed: AgentFailed | None = None
    ignored_lines: int = 0


def _tool_rules(permissions: AgentPermissions, label: str) -> dict[str, object]:
    tools: dict[str, object] = {}
    for tool, rule in permissions.effective_tools().items():
        if tool in DEFAULT_DENIED_TOOLS and tool not in permissions.tools:
            continue
        names = DEEPAGENTS_TOOL_NAMES.get(tool)
        if names is None:
            raise InvalidArgumentException(
                f"{label}: deepagents no tiene la herramienta {tool!r}; usa una de "
                f"{sorted(DEEPAGENTS_TOOL_NAMES)}"
            )
        if not isinstance(rule, str) and tool != PATTERN_TOOL:
            raise InvalidArgumentException(
                f"{label}: con deepagents sólo {PATTERN_TOOL!r} admite patrones"
            )
        for name in names:
            tools[name] = rule if isinstance(rule, str) else dict(rule)
    return {"default": permissions.default, "tools": tools}


def build_deepagents_config(
    spec: AgentSpec, *, gateway_urls: Mapping[str, str], workdir: str, entrypoint: str | None
) -> dict[str, object]:
    """La configuración que lee el runner, como diccionario."""
    spec.require_gateways(gateway_urls.keys())
    if spec.mcp:
        raise InvalidArgumentException("deepagents no admite AgentSpec.mcp; usa runtime='opencode'")
    if spec.raw_config:
        raise InvalidArgumentException(
            "AgentSpec.raw_config es configuración de OpenCode; deepagents no la admite"
        )
    model = spec.model
    base_url = gateway_urls[model.gateway].rstrip("/") + model.base_path
    subagents: list[dict[str, object]] = []
    for name, sub in spec.agents.items():
        subagents.append(
            {
                "name": name,
                "description": sub.description,
                "system_prompt": sub.instructions,
                "model": sub.model,
                "permissions": None
                if sub.permissions is None
                else _tool_rules(sub.permissions, f"AgentSpec.agents[{name!r}].permissions"),
            }
        )
    return {
        "v": AGENT_PROTOCOL_VERSION,
        "provider": model.provider,
        "model": model.id,
        "region": model.region,
        "base_url": base_url,
        "credential_placeholder": MODEL_CREDENTIAL_PLACEHOLDER,
        "prompt_caching": model.prompt_caching,
        "instructions": spec.instructions,
        "workdir": workdir,
        "sessions_dir": DEEPAGENTS_SESSIONS_DIR,
        "entrypoint": entrypoint,
        "permissions": _tool_rules(spec.permissions, "AgentSpec.permissions"),
        "subagents": subagents,
    }


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage(raw: object) -> TokenUsage:
    if not isinstance(raw, Mapping):
        return TokenUsage()
    return TokenUsage(**{key: _int(raw.get(key)) for key in _USAGE_KEYS})


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _protocol_line(kind: str) -> str:
    return f'printf \'%s\\n\' \'{{"v":{AGENT_PROTOCOL_VERSION},"type":"rayito.{kind}"}}\''


def _run_script(config_path: str) -> str:
    lines = [
        "set -u",
        f"mkdir -p {shell_quote(DEEPAGENTS_SESSIONS_DIR)}",
        f"exec 9>{shell_quote(AGENT_RUN_LOCK_PATH)}",
        f"if ! flock -n 9; then {_protocol_line('busy')}; exit 0; fi",
        f"if [ ! -x {shell_quote(DEEPAGENTS_PYTHON)} ] || "
        f"[ ! -r {shell_quote(DEEPAGENTS_RUNNER_PATH)} ]; then "
        f"{_protocol_line('runtime_missing')}; exit 0; fi",
        f"exec {shell_quote(DEEPAGENTS_PYTHON)} {shell_quote(DEEPAGENTS_RUNNER_PATH)} "
        f"{shell_quote(config_path)}",
    ]
    return "\n".join(lines) + "\n"


class DeepAgents:
    """`AgentRuntime` de deepagents (`runtime=DeepAgents(...)` o
    `runtime="deepagents"`).

    Sin `entrypoint`, el runner construye `create_deep_agent(model,
    system_prompt=instructions, subagents, backend=LocalShellBackend(
    root_dir=workdir), middleware=ctx.middleware)`. Con `entrypoint=
    "pkg.mod:build"`, importa ese módulo desde el directorio de trabajo y
    llama a `build(ctx)` con un `RunnerContext(model, instructions,
    subagents, backend, middleware, workdir)`; debe devolver un grafo
    compilado. Si no pasa `ctx.middleware` a su grafo, se pierden los
    permisos de `AgentSpec`.

    El modelo siempre llega por la pasarela (`ChatBedrockConverse`,
    `ChatAnthropic` o `ChatOpenAI` con la URL de la pasarela y un marcador
    como clave). deepagents marca puntos de caché de prompts para Claude
    (Bedrock y Anthropic); `AgentModel(prompt_caching=False)` lo apaga."""

    name: str = "deepagents"

    def __init__(self, entrypoint: str | None = None) -> None:
        if entrypoint is not None and (
            not isinstance(entrypoint, str) or not _ENTRYPOINT_PATTERN.fullmatch(entrypoint)
        ):
            raise InvalidArgumentException(
                "DeepAgents.entrypoint debe tener la forma 'paquete.modulo:funcion'"
            )
        self.entrypoint = entrypoint

    def __repr__(self) -> str:
        return f"DeepAgents(entrypoint={self.entrypoint!r})"

    def build_config(
        self, spec: AgentSpec, *, gateway_urls: Mapping[str, str], workdir: str
    ) -> RuntimeFiles:
        config = build_deepagents_config(
            spec, gateway_urls=gateway_urls, workdir=workdir, entrypoint=self.entrypoint
        )
        files = [RuntimeFile(DEEPAGENTS_CONFIG_PATH, canonical_json(config), 0o600)]
        return RuntimeFiles(files=tuple(files), config_sha256=files_sha256(files))

    def command(self, request: RunRequest) -> RunCommand:
        """El script (cerrojo, comprobación del runner, `exec` del Python
        del venv) y la petición JSON por stdin. `attach` no aplica: sólo se
        admite `"auto"` o `False`."""
        if request.attach is True:
            raise InvalidArgumentException("deepagents no tiene servidor residente: attach=True")
        if request.attach not in ("auto", False):
            raise InvalidArgumentException("attach debe ser True, False o 'auto'")
        if request.session_id is not None and not _SESSION_ID_PATTERN.fullmatch(request.session_id):
            raise InvalidArgumentException("session_id de deepagents debe tener la forma rda_<hex>")
        if request.model is not None:
            validate_model_id(request.model, "model")
        payload = {
            "v": AGENT_PROTOCOL_VERSION,
            "prompt": request.prompt,
            "session_id": request.session_id,
            "model": request.model,
            "reasoning": request.reasoning,
        }
        envs = {"HOME": DEFAULT_AGENT_WORKDIR, **DEEPAGENTS_FLAG_ENVS}
        if request.spec.model.provider == "bedrock":
            envs["AWS_BEARER_TOKEN_BEDROCK"] = MODEL_CREDENTIAL_PLACEHOLDER
        if request.spec.model.region is not None:
            envs["AWS_REGION"] = request.spec.model.region
        return RunCommand(
            script=_run_script(DEEPAGENTS_CONFIG_PATH),
            envs=envs,
            stdin=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def new_state(self) -> RuntimeState:
        return DeepAgentsState()

    def parse_line(self, line: bytes, state: RuntimeState) -> Sequence[AgentEvent]:
        """Una línea del protocolo v1 a cero o un evento. Lo que no es JSON,
        otra versión o un tipo desconocido se ignora y se cuenta."""
        assert isinstance(state, DeepAgentsState)
        try:
            payload = json.loads(line)
        except ValueError:
            state.ignored_lines += 1
            return ()
        if not isinstance(payload, Mapping) or payload.get("v") != AGENT_PROTOCOL_VERSION:
            state.ignored_lines += 1
            return ()
        events = self._map(payload, state)
        if events is None:
            state.ignored_lines += 1
            return ()
        return events

    def _map(
        self, payload: Mapping[str, object], state: DeepAgentsState
    ) -> list[AgentEvent] | None:
        kind = payload.get("type")
        if kind in ("rayito.busy", "rayito.runtime_missing"):
            state.failed = AgentFailed(reason=str(kind).removeprefix("rayito."))  # type: ignore[arg-type]
            return [state.failed]
        if kind not in PROTOCOL_EVENT_TYPES:
            return None
        if kind == "session":
            session_id = payload.get("session_id")
            if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.fullmatch(session_id):
                return None
            state.session_id = session_id
            return []
        if kind == "step_started":
            state.steps += 1
            return [StepStarted(index=state.steps)]
        if kind == "step_finished":
            usage = _usage(payload.get("usage"))
            state.usage = state.usage + usage
            return [
                StepFinished(
                    index=state.steps,
                    usage=usage,
                    finish_reason=_optional_str(payload.get("finish_reason")),
                )
            ]
        if kind in ("text_delta", "text", "reasoning"):
            text = payload.get("text")
            if not isinstance(text, str):
                return None
            if kind == "text_delta":
                return [TextDelta(text=text)]
            return [Text(text=text) if kind == "text" else Reasoning(text=text)]
        if kind == "tool_call":
            return self._tool_call(payload)
        if kind == "agent_failed":
            reason = payload.get("reason")
            if reason not in RUNNER_FAILURE_REASONS or reason not in AGENT_FAILURE_REASONS:
                reason = "protocol_error"
            state.failed = AgentFailed(
                reason=reason,  # type: ignore[arg-type]
                detail_code=_optional_str(payload.get("detail_code")),
                session_id=state.session_id,
            )
            return [state.failed]
        state.done = True
        return []

    @staticmethod
    def _tool_call(payload: Mapping[str, object]) -> list[AgentEvent] | None:
        status = payload.get("status")
        if status not in ("completed", "error"):
            return None
        raw_input = payload.get("input")
        raw_output = payload.get("output")
        output, truncated = (None, False)
        if isinstance(raw_output, str):
            output, truncated = truncate_tool_output(raw_output)
        return [
            ToolCall(
                call_id=str(payload.get("call_id", "")),
                name=str(payload.get("name", "")),
                status="completed" if status == "completed" else "error",
                input=dict(raw_input) if isinstance(raw_input, Mapping) else None,
                output=output,
                output_truncated=truncated or payload.get("output_truncated") is True,
            )
        ]

    def finish(self, state: RuntimeState, exit_code: int) -> Done | AgentFailed:
        """`Done` sólo con salida 0, el `done` del runner y una sesión."""
        assert isinstance(state, DeepAgentsState)
        if state.failed is not None:
            return state.failed
        if exit_code != 0:
            return AgentFailed(
                reason="runtime_error", exit_code=exit_code, session_id=state.session_id
            )
        if not state.done or state.session_id is None:
            return AgentFailed(
                reason="protocol_error", exit_code=exit_code, session_id=state.session_id
            )
        return Done(session_id=state.session_id, exit_code=exit_code, usage=state.usage)

    def abort_command(self, state: RuntimeState) -> str | None:
        """Nada: matar el proceso basta (no hay servidor residente)."""
        return None

    def template_steps(self) -> Sequence[TemplateStep]:
        """Vacío: la plantilla de agente (`ai-agent-fast-start`) declara sus
        propios pasos."""
        return ()

    def warmup_steps(self, *, serve: bool) -> Sequence[WarmupStep]:
        """Carga deepagents y langchain en la caché de páginas; `serve` no
        aplica."""
        del serve
        return (
            WarmupStep(
                cmd=f"{shell_quote(DEEPAGENTS_PYTHON)} -c 'import deepagents, langchain_aws' "
                ">/dev/null 2>&1 || true"
            ),
        )


__all__ = [
    "DEEPAGENTS_CONFIG_PATH",
    "DEEPAGENTS_RUNNER_PATH",
    "DEEPAGENTS_SESSIONS_DIR",
    "DEEPAGENTS_TOOL_NAMES",
    "DeepAgents",
    "DeepAgentsState",
    "build_deepagents_config",
]
