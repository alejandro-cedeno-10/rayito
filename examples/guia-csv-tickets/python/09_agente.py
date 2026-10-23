"""Forma A: un turno del agente dentro del sandbox, verificado por la traza y el contrato.

La respuesta final no basta como prueba: comprobamos en `tool_calls` que el
agente ejecutó python3 y comparamos los recuentos con el contrato determinista."""

import os
import re
import sys

from rayito import AgentLimits, AgentModel, AgentPermissions, AgentSpec, Sandbox, bedrock_gateway

from _comun import CSV_LOCAL, CSV_SANDBOX, METADATA, verificar

AGENT_TEMPLATE = os.environ.get("AGENT_TEMPLATE", "rayito-agent")
MODEL_SECRET = os.environ.get("MODEL_SECRET", "bedrock-key")  # el nombre, nunca el valor
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
REGION = "us-east-1"

SPEC = AgentSpec(
    model=AgentModel(provider="bedrock", id=MODEL_ID, gateway="bedrock", region=REGION),
    # La instrucción pide; los permisos deniegan todo salvo bash con `python3 *`.
    # No es solo lectura: python3 todavía puede escribir ficheros accesibles al proceso.
    instructions="Responde en español, en dos frases como máximo. No modifiques ficheros.",
    permissions=AgentPermissions(default="deny", tools={"bash": {"python3 *": "allow", "*": "deny"}}),
)
LIMITS = AgentLimits(max_steps=10, timeout_seconds=180)
PROMPT = f"Cuenta con python3 los tickets de {CSV_SANDBOX} por categoría y dame el resultado."
PATRONES = {"acceso": r"acceso", "facturacion": r"factur[aá]ci[oó]n", "rendimiento": r"rendimiento"}


def recuentos(texto: str) -> dict[str, int]:
    """Extrae «categoría: N» o «N de categoría» de la respuesta. Es una heurística:
    si el modelo redacta de otra forma, falla y lo dice; no la conviertas en la prueba única."""
    encontrados = {}
    for cat, patron in PATRONES.items():
        antes = re.search(
            r"(\d+)\s+(?:tickets?\s+)?(?:de|en)\s+(?:la\s+categor[ií]a\s+)?\W{0,2}" + patron, texto, re.IGNORECASE
        )
        despues = re.search(patron + r"\W{0,6}\s*(\d+)", texto, re.IGNORECASE)
        m = antes or despues
        if m:
            encontrados[cat] = int(m.group(1))
    return encontrados


def main() -> int:
    with Sandbox.create(
        AGENT_TEMPLATE,
        timeout=900,
        metadata=METADATA,
        allow_internet_access=False,  # sin salida a Internet: sólo la pasarela del modelo
        gateways={"bedrock": bedrock_gateway(MODEL_SECRET, region=REGION, models=[MODEL_ID], rate_per_minute=60)},
    ) as sbx:
        sbx.files.write(CSV_SANDBOX, CSV_LOCAL.read_text(encoding="utf-8"))
        result = sbx.agent.run(PROMPT, spec=SPEC, limits=LIMITS)

    print("respuesta:", result.text)
    print(f"pasos={result.steps} tokens={result.usage.total}")
    for call in result.tool_calls:
        comando = str((call.input or {}).get("command", ""))[:100]
        print(f"traza: {call.name} [{call.status}] {comando!r}")

    uso_python = any(
        c.name == "bash" and c.status == "completed" and "python3" in str((c.input or {}).get("command", ""))
        for c in result.tool_calls
    )
    esperado = {cat: v["tickets"] for cat, v in verificar.esperado()["categorias"].items()}
    obtenido = recuentos(result.text)
    print("¿usó python3 según la traza?:", "sí" if uso_python else "no")
    print("recuentos en la respuesta:", obtenido, "| contrato:", esperado)
    if not uso_python or obtenido != esperado:
        print("verificación: NO superada", file=sys.stderr)
        return 1
    print("verificación: superada (traza + contrato)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
