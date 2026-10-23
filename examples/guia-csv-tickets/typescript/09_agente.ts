// Forma A: un turno del agente, verificado por la traza y por el contrato determinista.
import { AgentLimits, AgentModel, AgentPermissions, AgentSpec, Sandbox, bedrockGateway } from "rayito";
import { CSV_SANDBOX, METADATA, csvLocal, esperado } from "./comun.ts";

const AGENT_TEMPLATE = process.env.AGENT_TEMPLATE ?? "rayito-agent";
const MODEL_SECRET = process.env.MODEL_SECRET ?? "bedrock-key"; // el nombre, nunca el valor
const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
const REGION = "us-east-1";

const spec = new AgentSpec({
  model: new AgentModel({ provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: REGION }),
  // La instrucción pide; los permisos bloquean la edición y limitan bash a `python3 *`.
  // No es solo lectura: python3 todavía puede escribir ficheros accesibles al proceso.
  instructions: "Responde en español, en dos frases como máximo. No modifiques ficheros.",
  permissions: new AgentPermissions({ default: "deny", tools: { bash: { "python3 *": "allow", "*": "deny" } } }),
});
const limits = new AgentLimits({ maxSteps: 10, timeoutMs: 180_000 });
const PATRONES: Record<string, string> = { acceso: "acceso", facturacion: "factur[aá]ci[oó]n", rendimiento: "rendimiento" };

/** «categoría: N» o «N de categoría». Heurística: si el modelo redacta de otra forma, falla y lo dice. */
function recuentos(texto: string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [cat, patron] of Object.entries(PATRONES)) {
    const antes = new RegExp(`(\\d+)\\s+(?:tickets?\\s+)?(?:de|en)\\s+(?:la\\s+categor[ií]a\\s+)?\\W{0,2}${patron}`, "i").exec(texto);
    const despues = new RegExp(`${patron}\\W{0,6}\\s*(\\d+)`, "i").exec(texto);
    const m = antes ?? despues;
    if (m) out[cat] = Number(m[1]);
  }
  return out;
}

const result = await (async () => {
  await using sbx = await Sandbox.create({
    template: AGENT_TEMPLATE,
    timeoutMs: 900_000,
    metadata: METADATA,
    allowInternetAccess: false, // sin salida a Internet: sólo la pasarela del modelo
    gateways: { bedrock: bedrockGateway(MODEL_SECRET, { region: REGION, models: [MODEL_ID], ratePerMinute: 60 }) },
  });
  await sbx.files.write(CSV_SANDBOX, csvLocal());
  // return await: sin await, `await using` cerraría el sandbox antes de que termine el agente
  return await sbx.agent.run(`Cuenta con python3 los tickets de ${CSV_SANDBOX} por categoría y dame el resultado.`, {
    spec,
    limits,
  });
})();

console.log("respuesta:", result.text);
console.log(`pasos=${result.steps} tokens=${result.usage.total}`);
const comando = (c: { input: Readonly<Record<string, unknown>> | undefined }): string => String(c.input?.command ?? "");
for (const call of result.toolCalls) console.log(`traza: ${call.name} [${call.status}] ${JSON.stringify(comando(call).slice(0, 100))}`);

const usoPython = result.toolCalls.some((c) => c.name === "bash" && c.status === "completed" && comando(c).includes("python3"));
const want = Object.fromEntries(Object.entries(esperado().categorias).map(([k, v]) => [k, v.tickets]));
const got = recuentos(result.text);
console.log("¿usó python3 según la traza?:", usoPython ? "sí" : "no");
console.log("recuentos en la respuesta:", got, "| contrato:", want);
const ok = usoPython && JSON.stringify(got) === JSON.stringify(want);
console.log(ok ? "verificación: superada (traza + contrato)" : "verificación: NO superada");
process.exitCode = ok ? 0 : 1;
