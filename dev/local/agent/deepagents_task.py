"""Tarea mínima de deepagents que el spike (`spike.py`) sube al sandbox y
corre con el Python del venv `/opt/agents/deepagents`.

El modelo es Claude en Amazon Bedrock por `langchain-aws`, con el
`endpoint_url` apuntando a la pasarela de secretos de `rayd` (loopback). El
proceso nunca tiene la credencial: `AWS_BEARER_TOKEN_BEDROCK` vale un
marcador para que botocore elija la autenticación bearer, y la pasarela
quita esa cabecera y pone la real. El agente usa `LocalShellBackend`, así
que sus herramientas (`ls`, `write_file`, `execute`...) tocan el sistema de
ficheros del sandbox: el límite es el MicroVM, no el agente.

Imprime una línea JSON con lo que mide el spike (sin contenido del modelo).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Final

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain_aws import ChatBedrockConverse

MAX_OUTPUT_TOKENS: Final = 512
EXPECTED_FILE: Final = "informe.txt"
EXPECTED_TEXT: Final = "hola desde deepagents"
PROMPT: Final = (
    f"Crea el fichero {EXPECTED_FILE} con exactamente este texto: {EXPECTED_TEXT}. "
    "Después lista el directorio con la herramienta ls y responde sólo 'listo'."
)


def main() -> int:
    workdir = Path(sys.argv[1])
    workdir.mkdir(parents=True, exist_ok=True)
    model = ChatBedrockConverse(
        model=os.environ["SPIKE_MODEL"],
        region_name=os.environ["SPIKE_REGION"],
        endpoint_url=os.environ["SPIKE_GATEWAY_URL"],
        max_tokens=MAX_OUTPUT_TOKENS,
    )
    agent = create_deep_agent(
        model=model,
        backend=LocalShellBackend(root_dir=workdir, virtual_mode=True),
    )
    started = time.monotonic()
    result = agent.invoke({"messages": [{"role": "user", "content": PROMPT}]})
    elapsed = time.monotonic() - started
    target = workdir / EXPECTED_FILE
    tool_calls = [
        call["name"]
        for message in result["messages"]
        for call in (getattr(message, "tool_calls", None) or [])
    ]
    print(
        json.dumps(
            {
                "seconds": round(elapsed, 2),
                "file_written": target.is_file(),
                "file_matches": target.is_file()
                and EXPECTED_TEXT in target.read_text(encoding="utf-8"),
                "tool_calls": tool_calls,
                "messages": len(result["messages"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
