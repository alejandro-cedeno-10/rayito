// Un programa escrito para el SDK de E2B 2.x, con sólo el import cambiado.
import { Sandbox } from "rayito/e2b"; // antes: from "@e2b/code-interpreter"

await using sbx = await Sandbox.create({ timeoutMs: 300_000, metadata: { app: "tickets-demo" } });
const execution = await sbx.runCode("x = 40; x + 2");
if (execution.error) throw new Error(`${execution.error.name}: ${execution.error.value}`);
console.log(execution.text);
