"""Driver de deepagents que suben al sandbox los tests `local` de agentes
(`clients/python/tests/local/test_local_agents.py` y su espejo
`clients/typescript/tests/local/agents.local.test.ts`) y que corre con el
Python del venv `/opt/agents/deepagents` del guest de
`dev/local/agent/compose.yaml`.

El modelo es Claude en Amazon Bedrock por `langchain-aws`, con el
`endpoint_url` en la pasarela de secretos de `rayd` (loopback): el proceso
nunca tiene la credencial, `AWS_BEARER_TOKEN_BEDROCK` vale un marcador para
que botocore elija la autenticación bearer y la pasarela pone la real.

    python deepagents_check.py session <dir> <palabra>
        Dos turnos sobre el mismo `thread_id` con un checkpointer en memoria:
        el primero escribe un fichero (uso de herramientas) y recibe una
        palabra que no se escribe en ningún sitio, el segundo la pregunta.
        El primero se recorre con `stream(stream_mode="updates")` para
        devolver la forma de los eventos (nombres de nodo, nunca contenido).
    python deepagents_check.py steps <dir> <límite>
        Un turno con `recursion_limit` bajo que pide más pasos de los que
        caben: devuelve si LangGraph lo cortó con `GraphRecursionError`.
    python deepagents_check.py sleep <dir>
        Arranca el agente y pide una orden larga, para abortarla desde fuera.
    python deepagents_check.py telemetry
        Si LangSmith trazaría este proceso (sin llamar al modelo).

Cada modo imprime una sola línea JSON en stdout con datos de forma (nombres
de herramientas y de nodos, booleanos, la palabra que el propio test eligió)
y nunca el texto del modelo entero ni el entorno.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Final

#: Lo justo para las respuestas de una palabra y las llamadas a herramientas.
MAX_OUTPUT_TOKENS: Final = 512
SESSION_FILE: Final = "informe.txt"
SESSION_TEXT: Final = "hola desde deepagents"
THREAD_ID: Final = "rayito-local-agents"
#: Más ficheros de los que un `recursion_limit` de un dígito deja escribir
#: uno a uno.
STEPS_FILES: Final = 8
#: Lo que duerme la orden que el test aborta: mucho más que su espera.
SLEEP_SECONDS: Final = 300


def build_agent(workdir: Path, *, checkpointer: Any = None) -> Any:
    from deepagents import create_deep_agent
    from deepagents.backends import LocalShellBackend
    from langchain_aws import ChatBedrockConverse

    model = ChatBedrockConverse(
        model=os.environ["AGENT_MODEL"],
        region_name=os.environ["AGENT_REGION"],
        endpoint_url=os.environ["AGENT_GATEWAY_URL"],
        max_tokens=MAX_OUTPUT_TOKENS,
    )
    workdir.mkdir(parents=True, exist_ok=True)
    return create_deep_agent(
        model=model,
        backend=LocalShellBackend(root_dir=workdir, virtual_mode=True),
        checkpointer=checkpointer,
    )


def user(text: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}]}


def tool_names(messages: list[Any]) -> list[str]:
    return [
        call["name"]
        for message in messages
        for call in (getattr(message, "tool_calls", None) or [])
    ]


def last_text(messages: list[Any]) -> str:
    content = messages[-1].content if messages else ""
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)


def session(workdir: Path, word: str) -> dict[str, Any]:
    from langgraph.checkpoint.memory import InMemorySaver

    agent = build_agent(workdir, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": THREAD_ID}}
    first = user(
        f"Crea el fichero {SESSION_FILE} con exactamente este texto: {SESSION_TEXT}. "
        f"Además, recuerda la palabra clave {word}, pero no la escribas en ningún "
        "fichero. Responde sólo 'listo'."
    )
    events: list[dict[str, Any]] = []
    for chunk in agent.stream(first, config=config, stream_mode="updates"):
        events.append({"type": type(chunk).__name__, "nodes": sorted(chunk)})
    first_messages = agent.get_state(config).values["messages"]
    second = agent.invoke(
        user("¿Cuál era la palabra clave? Responde sólo con la palabra, sin herramientas."),
        config=config,
    )
    target = workdir / SESSION_FILE
    return {
        "events": events,
        "tool_calls": tool_names(first_messages),
        "file_matches": target.is_file() and SESSION_TEXT in target.read_text(encoding="utf-8"),
        "recalled": word.lower() in last_text(second["messages"]).lower(),
    }


def steps(workdir: Path, limit: int) -> dict[str, Any]:
    from langgraph.errors import GraphRecursionError

    agent = build_agent(workdir)
    prompt = user(
        f"Crea {STEPS_FILES} ficheros, uno por llamada y de uno en uno: "
        + ", ".join(f"f{index}.txt" for index in range(STEPS_FILES))
        + ". Cada uno con su nombre como texto."
    )
    try:
        agent.invoke(prompt, config={"recursion_limit": limit})
    except GraphRecursionError:
        return {"recursion_limit_hit": True}
    return {"recursion_limit_hit": False}


def sleep(workdir: Path) -> dict[str, Any]:
    agent = build_agent(workdir)
    agent.invoke(user(f"Ejecuta la orden `sleep {SLEEP_SECONDS}` y espera a que termine."))
    return {"finished": True}


def telemetry() -> dict[str, Any]:
    from langsmith.utils import tracing_is_enabled

    return {"langsmith_tracing": bool(tracing_is_enabled())}


def main(argv: list[str]) -> int:
    mode = argv[1]
    if mode == "telemetry":
        report = telemetry()
    elif mode == "session":
        report = session(Path(argv[2]), argv[3])
    elif mode == "steps":
        report = steps(Path(argv[2]), int(argv[3]))
    elif mode == "sleep":
        report = sleep(Path(argv[2]))
    else:
        print(f"modo desconocido: {mode}", file=sys.stderr)
        return 2
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
