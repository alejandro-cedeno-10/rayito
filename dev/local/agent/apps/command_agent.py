"""Un agente de ejemplo que habla el protocolo v1 por stdio, para los tests
`local` de `CustomRuntime(command=[...])` (`make local-agent-up`): lee la
petición de una línea de stdin y escribe los eventos de una vuelta. Sólo
biblioteca estándar; no llama a ningún modelo."""

from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = 1


def emit(kind: str, **fields: object) -> None:
    print(json.dumps({"v": PROTOCOL_VERSION, "type": kind, **fields}), flush=True)


def main() -> int:
    request = json.loads(sys.stdin.readline())
    reply = f"eco:{request['prompt']}"
    emit("step_started", index=1)
    for word in reply.split(":"):
        emit("text_delta", text=word)
    emit("text", text=reply)
    emit(
        "step_finished",
        index=1,
        usage={"input": 3, "output": 2, "reasoning": 0, "cache_read": 0, "cache_write": 0},
        finish_reason="stop",
    )
    emit("done", session_id=request.get("session_id"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
