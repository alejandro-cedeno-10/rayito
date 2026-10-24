/**
 * Forma A: un turno del agente, verificado por la traza y por el contrato determinista.
 *
 * La instrucción pide; los permisos deniegan todo salvo bash con `python3 *`. No es solo lectura:
 * python3 todavía puede escribir ficheros accesibles al proceso. El sandbox no tiene salida a
 * Internet: sólo la pasarela del modelo. `MODEL_SECRET` es el nombre del secreto, nunca su valor.
 * `runAgent` devuelve con `return await`: sin `await`, `await using` cerraría el sandbox antes de
 * que termine el agente.
 */
import {
  AgentLimits,
  AgentModel,
  AgentPermissions,
  type AgentResult,
  AgentSpec,
  bedrockGateway,
  Sandbox,
} from "rayito";
import { expectedContract, localCsv, METADATA, SANDBOX_CSV } from "./common.ts";

const AGENT_TEMPLATE = process.env.AGENT_TEMPLATE ?? "rayito-agent";
const MODEL_SECRET = process.env.MODEL_SECRET ?? "bedrock-key";
const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
const REGION = "us-east-1";
const SANDBOX_TIMEOUT_MS = 900_000;
const GATEWAY_RATE_PER_MINUTE = 60;
const TRACE_PREVIEW_CHARS = 100;
const PROMPT = `Cuenta con python3 los tickets de ${SANDBOX_CSV} por categoría y dame el resultado.`;
const PATTERNS: Record<string, string> = {
  acceso: "acceso",
  facturacion: "factur[aá]ci[oó]n",
  rendimiento: "rendimiento",
};

const spec = new AgentSpec({
  model: new AgentModel({ provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: REGION }),
  instructions: "Responde en español, en dos frases como máximo. No modifiques ficheros.",
  permissions: new AgentPermissions({ default: "deny", tools: { bash: { "python3 *": "allow", "*": "deny" } } }),
});
const AGENT_MAX_STEPS = 10;
const AGENT_TIMEOUT_MS = 180_000;
const limits = new AgentLimits({ maxSteps: AGENT_MAX_STEPS, timeoutMs: AGENT_TIMEOUT_MS });

/** «categoría: N» o «N de categoría». Heurística: si el modelo redacta de otra forma, falla y lo dice. */
function counts(text: string): Record<string, number> {
  const found: Record<string, number> = {};
  for (const [category, pattern] of Object.entries(PATTERNS)) {
    const before = new RegExp(
      `(\\d+)\\s+(?:tickets?\\s+)?(?:de|en)\\s+(?:la\\s+categor[ií]a\\s+)?\\W{0,2}${pattern}`,
      "i",
    ).exec(text);
    const after = new RegExp(`${pattern}\\W{0,6}\\s*(\\d+)`, "i").exec(text);
    const match = before ?? after;
    if (match?.[1]) found[category] = Number(match[1]);
  }
  return found;
}

/** El comando de una llamada a `bash`, o cadena vacía si no lo tiene. */
const commandOf = (call: { input: Readonly<Record<string, unknown>> | undefined }): string =>
  String(call.input?.command ?? "");

/** Un turno del agente sobre el CSV, en un sandbox sin salida a Internet. */
async function runAgent(): Promise<AgentResult> {
  await using sbx = await Sandbox.create({
    template: AGENT_TEMPLATE,
    timeoutMs: SANDBOX_TIMEOUT_MS,
    metadata: METADATA,
    allowInternetAccess: false,
    gateways: {
      bedrock: bedrockGateway(MODEL_SECRET, {
        region: REGION,
        models: [MODEL_ID],
        ratePerMinute: GATEWAY_RATE_PER_MINUTE,
      }),
    },
  });
  await sbx.files.write(SANDBOX_CSV, localCsv());
  return await sbx.agent.run(PROMPT, { spec, limits });
}

/** Ejecuta el agente, imprime la traza y verifica traza y recuentos. */
async function main(): Promise<number> {
  const result = await runAgent();
  console.log("respuesta:", result.text);
  console.log(`pasos=${result.steps} tokens=${result.usage.total}`);
  for (const call of result.toolCalls) {
    console.log(
      `traza: ${call.name} [${call.status}] ${JSON.stringify(commandOf(call).slice(0, TRACE_PREVIEW_CHARS))}`,
    );
  }
  const usedPython = result.toolCalls.some(
    (call) => call.name === "bash" && call.status === "completed" && commandOf(call).includes("python3"),
  );
  const want = Object.fromEntries(
    Object.entries(expectedContract().categories).map(([category, summary]) => [category, summary.tickets]),
  );
  const got = counts(result.text);
  console.log("¿usó python3 según la traza?:", usedPython ? "sí" : "no");
  console.log("recuentos en la respuesta:", got, "| contrato:", want);
  const sameKeys = Object.keys(got).length === Object.keys(want).length;
  const ok = usedPython && sameKeys && Object.entries(want).every(([category, n]) => got[category] === n);
  console.log(ok ? "verificación: superada (traza + contrato)" : "verificación: NO superada");
  return ok ? 0 : 1;
}

process.exitCode = await main();
