/**
 * Un comando, un fichero de ida y vuelta y una celda con estado.
 * `await using` llama a `kill()` al salir del bloque, también si algo lanza.
 */
import { Sandbox } from "rayito";
import { METADATA, runCell, TEMPLATE } from "./common.ts";

const SANDBOX_TIMEOUT_MS = 600_000;
const COMMAND_TIMEOUT_MS = 30_000;
const NOTE_PATH = "/home/user/a.txt";

async function main(): Promise<void> {
  await using sbx = await Sandbox.create({ template: TEMPLATE, timeoutMs: SANDBOX_TIMEOUT_MS, metadata: METADATA });
  process.stdout.write((await sbx.commands.run("echo hola", { timeoutMs: COMMAND_TIMEOUT_MS })).stdout);
  await sbx.files.write(NOTE_PATH, "contenido");
  console.log(await sbx.files.read(NOTE_PATH));
  console.log((await runCell(sbx, "x = 40; x + 2", "celda con estado")).text);
}

await main();
