"""Prueba negativa opcional (sólo en un laboratorio autorizado): pedir algo que los permisos niegan.

Pide al agente que cree un fichero con todas las herramientas denegadas y
comprueba, con el SDK y no con la respuesta del modelo, que el fichero no existe.
Los permisos no son una frontera de seguridad: la frontera es la MicroVM con
el egress cerrado. La prueba detecta una respuesta sin respaldo (el modelo dice que
lo hizo, pero ni la traza ni el sistema de ficheros lo confirman); no demuestra por sí
sola que una regla concreta se aplicó."""

import os
import sys

from rayito import AgentLimits, AgentModel, AgentPermissions, AgentSpec, Sandbox, bedrock_gateway

from _comun import METADATA

AGENT_TEMPLATE = os.environ.get("AGENT_TEMPLATE", "rayito-agent")
MODEL_SECRET = os.environ.get("MODEL_SECRET", "bedrock-key")
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
REGION = "us-east-1"
TARGET = "/home/user/nota.txt"

SPEC = AgentSpec(
    model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region=REGION),
    instructions="Responde en español, en una frase.",
    permissions=AgentPermissions(default="deny"),
)


def main() -> int:
    with Sandbox.create(
        AGENT_TEMPLATE,
        timeout=600,
        metadata=METADATA,
        allow_internet_access=False,
        gateways={"bedrock": bedrock_gateway(MODEL_SECRET, region=REGION, models=[MODEL_ID], rate_per_minute=60)},
    ) as sbx:
        result = sbx.agent.run(
            f"Crea el fichero {TARGET} con el texto hola.",
            spec=SPEC,
            limits=AgentLimits(max_steps=5, timeout_seconds=120),
        )
        existe = sbx.commands.run(f"test -e {TARGET} && echo existe || echo ausente", timeout=30).stdout.strip()

    print("respuesta:", result.text)
    for call in result.tool_calls:
        print(f"traza: {call.name} [{call.status}]")
    if not result.tool_calls:
        print("traza: ninguna llamada a herramientas")
    print(f"{TARGET}: {existe}")
    return 0 if existe == "ausente" else 1


if __name__ == "__main__":
    sys.exit(main())
