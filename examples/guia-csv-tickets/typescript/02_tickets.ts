// El caso: resumen por categoría del CSV de tickets, comprobado contra el contrato.
import { writeFile } from "node:fs/promises";
import { Sandbox } from "rayito";
import { CSV_SANDBOX, CeldaFallida, METADATA, TEMPLATE, comparar, csvLocal, runCell } from "./comun.ts";

const LOAD = `import pandas as pd\ndf = pd.read_csv('${CSV_SANDBOX}')\nlen(df)`;
const SUMMARY = "resumen = df.groupby('categoria')['horas'].agg(['count', 'mean'])\nresumen";
const SUMMARY_JSON =
  "import json\n" +
  "print(json.dumps({c: {'tickets': int(r['count']), 'media_horas': float(r['mean'])} for c, r in resumen.iterrows()}))";
const CHART = `
import matplotlib.pyplot as plt
df.groupby('categoria')['horas'].mean().plot.bar(title='Horas medias por categoría', rot=0)
plt.tight_layout()
plt.show()
`;
const PNG_OUT = "tickets-ts.png";

async function main(): Promise<number> {
  await using sbx = await Sandbox.create({ template: TEMPLATE, timeoutMs: 600_000, metadata: METADATA });
  try {
    await sbx.files.write(CSV_SANDBOX, csvLocal());
    console.log(`filas cargadas: ${(await runCell(sbx, LOAD, "cargar el CSV")).text}`);
    console.log((await runCell(sbx, SUMMARY, "calcular el resumen")).text);

    const stdout = (await runCell(sbx, SUMMARY_JSON, "exportar el resumen")).logs.stdout.join("");
    const errores = comparar(JSON.parse(stdout));
    if (errores.length > 0) {
      console.error("el resumen NO cumple el contrato:\n  " + errores.join("\n  "));
      return 1;
    }
    console.log("contrato: OK (3 categorías, 7 tickets)");

    const chart = await runCell(sbx, CHART, "dibujar el gráfico");
    const png = chart.results.find((r) => r.png)?.png;
    if (!png) {
      console.error("la celda del gráfico no devolvió ningún PNG");
      return 1;
    }
    const bytes = Buffer.from(png, "base64");
    await writeFile(PNG_OUT, bytes);
    console.log(`gráfico: ${PNG_OUT} (${bytes.length} bytes)`);
    return 0;
  } catch (err) {
    if (err instanceof CeldaFallida) {
      console.error(err.message);
      return 1;
    }
    throw err;
  }
}

process.exitCode = await main();
