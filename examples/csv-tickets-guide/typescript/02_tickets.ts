/**
 * El caso: resumen por categoría del CSV de tickets, comprobado contra el contrato.
 * El resumen vuelve como JSON por stdout y se compara con `contract/expected.json`.
 * Sale con 1 si una celda falla, si no se cumple el contrato o si no hay PNG.
 */
import { writeFile } from "node:fs/promises";
import { Sandbox } from "rayito";
import {
  CellFailedError,
  compare,
  expectedContract,
  localCsv,
  METADATA,
  runCell,
  SANDBOX_CSV,
  TEMPLATE,
} from "./common.ts";

const SANDBOX_TIMEOUT_MS = 600_000;
const LOAD = `import pandas as pd\ndf = pd.read_csv('${SANDBOX_CSV}')\nlen(df)`;
const SUMMARY = "summary = df.groupby('categoria')['horas'].agg(['count', 'mean'])\nsummary";
const SUMMARY_JSON =
  "import json\n" +
  "print(json.dumps({c: {'tickets': int(r['count']), 'mean_hours': float(r['mean'])} for c, r in summary.iterrows()}))";
const CHART = `
import matplotlib.pyplot as plt
df.groupby('categoria')['horas'].mean().plot.bar(title='Horas medias por categoría', rot=0)
plt.tight_layout()
plt.show()
`;
const PNG_OUT = "tickets-ts.png";

/** Carga el CSV, calcula el resumen, lo compara con el contrato y guarda el gráfico. */
async function summarize(sbx: Sandbox): Promise<number> {
  await sbx.files.write(SANDBOX_CSV, localCsv());
  console.log(`filas cargadas: ${(await runCell(sbx, LOAD, "cargar el CSV")).text}`);
  console.log((await runCell(sbx, SUMMARY, "calcular el resumen")).text);

  const output = (await runCell(sbx, SUMMARY_JSON, "exportar el resumen")).logs.stdout.join("");
  const errors = compare(JSON.parse(output));
  if (errors.length > 0) {
    console.error(`el resumen NO cumple el contrato:\n  ${errors.join("\n  ")}`);
    return 1;
  }
  const contract = expectedContract();
  console.log(`contrato: OK (${Object.keys(contract.categories).length} categorías, ${contract.total} tickets)`);

  const chart = await runCell(sbx, CHART, "dibujar el gráfico");
  const png = chart.results.find((result) => result.png)?.png;
  if (!png) {
    console.error("la celda del gráfico no devolvió ningún PNG");
    return 1;
  }
  const bytes = Buffer.from(png, "base64");
  await writeFile(PNG_OUT, bytes);
  console.log(`gráfico: ${PNG_OUT} (${bytes.length} bytes)`);
  return 0;
}

/** Crea el sandbox y convierte un fallo de celda en código de salida 1. */
async function main(): Promise<number> {
  await using sbx = await Sandbox.create({ template: TEMPLATE, timeoutMs: SANDBOX_TIMEOUT_MS, metadata: METADATA });
  try {
    return await summarize(sbx);
  } catch (err) {
    if (!(err instanceof CellFailedError)) throw err;
    console.error(err.message);
    return 1;
  }
}

process.exitCode = await main();
