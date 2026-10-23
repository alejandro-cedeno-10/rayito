"""Forma A: un turno del agente dentro del sandbox, verificado por la traza y el contrato.

La respuesta final no basta como prueba: comprobamos en `tool_calls` que el
agente ejecutó python3 y comparamos los recuentos con el contrato determinista.

La instrucción pide; los permisos deniegan todo salvo bash con `python3 *`. No es
solo lectura: python3 todavía puede escribir ficheros accesibles al proceso. El
sandbox no tiene salida a Internet: sólo la pasarela del modelo. `MODEL_SECRET`
es el nombre del secreto, nunca su valor."""

import os
import re
import sys
from collections.abc import Mapping

from rayito import AgentLimits, AgentModel, AgentPermissions, AgentResult, AgentSpec, Sandbox, bedrock_gateway

from _common import LOCAL_CSV, METADATA, SANDBOX_CSV, verify

AGENT_TEMPLATE = os.environ.get("AGENT_TEMPLATE", "rayito-agent")
MODEL_SECRET = os.environ.get("MODEL_SECRET", "bedrock-key")
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
REGION = "us-east-1"
SANDBOX_TIMEOUT_SECONDS = 900
GATEWAY_RATE_PER_MINUTE = 60
TRACE_PREVIEW_CHARS = 100

SPEC = AgentSpec(
    model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region=REGION),
    instructions="Responde en español, en dos frases como máximo. No modifiques ficheros.",
    permissions=AgentPermissions(default="deny", tools={"bash": {"python3 *": "allow", "*": "deny"}}),
)
LIMITS = AgentLimits(max_steps=10, timeout_seconds=180)
PROMPT = f"Cuenta con python3 los tickets de {SANDBOX_CSV} por categoría y dame el resultado."
PATTERNS = {"acceso": r"acceso", "facturacion": r"factur[aá]ci[oó]n", "rendimiento": r"rendimiento"}


def counts(text: str) -> dict[str, int]:
    """Extrae «categoría: N» o «N de categoría» de la respuesta.

    Es una heurística: si el modelo redacta de otra forma, falla y lo dice; no
    la conviertas en la prueba única."""
    found: dict[str, int] = {}
    for category, pattern in PATTERNS.items():
        before = re.search(
            r"(\d+)\s+(?:tickets?\s+)?(?:de|en)\s+(?:la\s+categor[ií]a\s+)?\W{0,2}" + pattern, text, re.IGNORECASE
        )
        after = re.search(pattern + r"\W{0,6}\s*(\d+)", text, re.IGNORECASE)
        match = before or after
        if match:
            found[category] = int(match.group(1))
    return found


def command_of(call_input: Mapping[str, object] | None) -> str:
    """El comando de una llamada a `bash`, o cadena vacía si no lo tiene."""
    return str((call_input or {}).get("command", ""))


def run_agent() -> AgentResult:
    """Un turno del agente sobre el CSV, en un sandbox sin salida a Internet."""
    gateway = bedrock_gateway(MODEL_SECRET, region=REGION, models=[MODEL_ID], rate_per_minute=GATEWAY_RATE_PER_MINUTE)
    with Sandbox.create(
        AGENT_TEMPLATE,
        timeout=SANDBOX_TIMEOUT_SECONDS,
        metadata=METADATA,
        allow_internet_access=False,
        gateways={"bedrock": gateway},
    ) as sbx:
        sbx.files.write(SANDBOX_CSV, LOCAL_CSV.read_text(encoding="utf-8"))
        return sbx.agent.run(PROMPT, spec=SPEC, limits=LIMITS)


def main() -> int:
    """Ejecuta el agente, imprime la traza y verifica traza y recuentos."""
    result = run_agent()
    print("respuesta:", result.text)
    print(f"pasos={result.steps} tokens={result.usage.total}")
    for call in result.tool_calls:
        print(f"traza: {call.name} [{call.status}] {command_of(call.input)[:TRACE_PREVIEW_CHARS]!r}")

    used_python = any(
        call.name == "bash" and call.status == "completed" and "python3" in command_of(call.input)
        for call in result.tool_calls
    )
    want = {category: summary["tickets"] for category, summary in verify.expected()["categories"].items()}
    got = counts(result.text)
    print("¿usó python3 según la traza?:", "sí" if used_python else "no")
    print("recuentos en la respuesta:", got, "| contrato:", want)
    if not used_python or got != want:
        print("verificación: NO superada", file=sys.stderr)
        return 1
    print("verificación: superada (traza + contrato)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
