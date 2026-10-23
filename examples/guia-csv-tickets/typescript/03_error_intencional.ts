// Un error a propósito: el fallo del código es un dato del resultado, no una excepción.
import { Sandbox } from "rayito";
import { CSV_SANDBOX, METADATA, TEMPLATE, csvLocal, runCell } from "./comun.ts";

await using sbx = await Sandbox.create({ template: TEMPLATE, timeoutMs: 300_000, metadata: METADATA });
await sbx.files.write(CSV_SANDBOX, csvLocal());
await runCell(sbx, `import pandas as pd\ndf = pd.read_csv('${CSV_SANDBOX}')`, "cargar el CSV");

const failed = await sbx.runCode("df['no_existe']", { timeoutMs: 60_000 });
if (!failed.error) throw new Error("se esperaba un KeyError y la celda no falló");
// Esto es lo que le devolverías al modelo para que corrija su código.
console.log(`error esperado -> ${failed.error.name}: ${failed.error.value}`);
