"""Un sandbox de Rayito como herramienta de LangChain, en cincuenta líneas.

Instalación: `pip install rayito langchain` (y el proveedor de tu modelo).
Entorno: `RAYITO_TEMPLATE` (nombre o ARN de la imagen), `AWS_REGION` y
`AWS_PROFILE` o las credenciales de boto3. El sandbox se crea en la primera
llamada, vive como mucho 900 s y se destruye al salir del proceso.
"""

from __future__ import annotations

import atexit
import json

from langchain.tools import tool
from rayito import Sandbox

SANDBOX_TIMEOUT_SECONDS = 900
_sandbox: Sandbox | None = None


def sandbox() -> Sandbox:
    global _sandbox
    if _sandbox is None:
        _sandbox = Sandbox.create(timeout=SANDBOX_TIMEOUT_SECONDS)
        atexit.register(_sandbox.kill)
    return _sandbox


@tool
def run_python(code: str) -> str:
    """Ejecuta código Python en un sandbox aislado con estado entre llamadas
    (Linux, internet, matplotlib y pandas instalados). Devuelve un JSON con
    `text` (valor de la última expresión), `stdout`, `stderr` y `error`
    (nombre y valor si la celda lanzó una excepción)."""
    execution = sandbox().run_code(code)
    error = execution.error
    summary = {
        "text": execution.text,
        "stdout": "".join(execution.logs.stdout),
        "stderr": "".join(execution.logs.stderr),
        "error": None if error is None else {"name": error.name, "value": error.value},
    }
    return json.dumps(summary, ensure_ascii=False)


if __name__ == "__main__":
    from langchain.agents import create_agent

    agent = create_agent("anthropic:claude-opus-5", tools=[run_python])
    print(agent.invoke({"messages": [("user", "Calcula 2**100 en Python.")]}))
