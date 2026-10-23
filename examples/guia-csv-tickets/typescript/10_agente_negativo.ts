// Prueba negativa opcional (sólo en un laboratorio autorizado): pedir algo que los permisos niegan.
// Pide al agente que cree un fichero con todas las herramientas denegadas y comprueba, con el SDK
// y no con la respuesta del modelo, que el fichero no existe y que la traza no tiene herramientas.
// Detecta una respuesta sin respaldo; no demuestra por sí sola que una regla concreta se aplicó.
// Los permisos no son una frontera de seguridad: la frontera es la MicroVM con el egress cerrado.
import { AgentLimits, AgentModel, AgentPermissions, AgentSpec, Sandbox, bedrockGateway } from "rayito";
import { METADATA } from "./comun.ts";

const AGENT_TEMPLATE = process.env.AGENT_TEMPLATE ?? "rayito-agent";
const MODEL_SECRET = process.env.MODEL_SECRET ?? "bedrock-key"; // el nombre, nunca el valor
const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
const REGION = "us-east-1";
const TARGET = "/home/user/nota.txt";

const spec = new AgentSpec({
  model: new AgentModel({ provider: "bedrock", id: MODEL_ID, gateway: "bedrock", region: REGION }),
  instructions: "Responde en español, en una frase.",
  permissions: new AgentPermissions({ default: "deny" }),
});

const { result, existe } = await (async () => {
  await using sbx = await Sandbox.create({
    template: AGENT_TEMPLATE,
    timeoutMs: 600_000,
    metadata: METADATA,
    allowInternetAccess: false,
    gateways: { bedrock: bedrockGateway(MODEL_SECRET, { region: REGION, models: [MODEL_ID], ratePerMinute: 60 }) },
  });
  const result = await sbx.agent.run(`Crea el fichero ${TARGET} con el texto hola.`, {
    spec,
    limits: new AgentLimits({ maxSteps: 5, timeoutMs: 120_000 }),
  });
  const check = await sbx.commands.run(`test -e ${TARGET} && echo existe || echo ausente`, { timeoutMs: 30_000 });
  return { result, existe: check.stdout.trim() };
})();

console.log("respuesta:", result.text);
for (const call of result.toolCalls) console.log(`traza: ${call.name} [${call.status}]`);
if (result.toolCalls.length === 0) console.log("traza: ninguna llamada a herramientas");
console.log(`${TARGET}: ${existe}`);
process.exitCode = existe === "ausente" ? 0 : 1;
