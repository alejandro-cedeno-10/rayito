/**
 * Un programa escrito para el SDK de E2B 2.x, con sólo el import cambiado.
 * Antes: `import { Sandbox } from "@e2b/code-interpreter"`.
 */
import { Sandbox } from "rayito/e2b";
import { METADATA } from "./common.ts";

const SANDBOX_TIMEOUT_MS = 300_000;

async function main(): Promise<void> {
  await using sbx = await Sandbox.create({ timeoutMs: SANDBOX_TIMEOUT_MS, metadata: METADATA });
  const execution = await sbx.runCode("x = 40; x + 2");
  if (execution.error) throw new Error(`${execution.error.name}: ${execution.error.value}`);
  console.log(execution.text);
}

await main();
