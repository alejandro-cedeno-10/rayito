/**
 * Volver a un sandbox con su id y su access token.
 * El token es un secreto: nunca lo imprimas. Desde otro proceso se hace igual.
 */
import { Sandbox } from "rayito";
import { METADATA, TEMPLATE } from "./common.ts";

const SANDBOX_TIMEOUT_MS = 600_000;
const STATE_PATH = "/home/user/estado.txt";

async function main(): Promise<void> {
  const sbx = await Sandbox.create({ template: TEMPLATE, timeoutMs: SANDBOX_TIMEOUT_MS, metadata: METADATA });
  try {
    await sbx.files.write(STATE_PATH, "paso 1 hecho");
    const { sandboxId, accessToken } = sbx;
    const again = await Sandbox.connect(sandboxId, { accessToken });
    console.log(await again.files.read(STATE_PATH));
  } finally {
    await sbx.kill();
  }
}

await main();
