/**
 * Un error a propósito: el fallo del código es un dato del resultado, no una excepción.
 * Lo que imprime es lo que le devolverías al modelo para que corrija su código.
 */
import { Sandbox } from "rayito";
import { CELL_TIMEOUT_MS, localCsv, METADATA, runCell, SANDBOX_CSV, TEMPLATE } from "./common.ts";

const SANDBOX_TIMEOUT_MS = 300_000;

async function main(): Promise<void> {
  await using sbx = await Sandbox.create({ template: TEMPLATE, timeoutMs: SANDBOX_TIMEOUT_MS, metadata: METADATA });
  await sbx.files.write(SANDBOX_CSV, localCsv());
  await runCell(sbx, `import pandas as pd\ndf = pd.read_csv('${SANDBOX_CSV}')`, "cargar el CSV");

  const failed = await sbx.runCode("df['no_existe']", { timeoutMs: CELL_TIMEOUT_MS });
  if (!failed.error) throw new Error("se esperaba un KeyError y la celda no falló");
  console.log(`error esperado -> ${failed.error.name}: ${failed.error.value}`);
  console.log(`traceback: ${failed.error.traceback.split("\n").length} líneas`);
}

await main();
