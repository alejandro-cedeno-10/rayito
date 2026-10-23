"""El caso: resumen por categoría del CSV de tickets, comprobado contra el contrato."""

import base64
import json
import sys
from pathlib import Path

from rayito import Sandbox

from _comun import CSV_LOCAL, CSV_SANDBOX, METADATA, TEMPLATE, CeldaFallida, run_cell, verificar

LOAD = f"import pandas as pd\ndf = pd.read_csv('{CSV_SANDBOX}')\nlen(df)"
SUMMARY = "resumen = df.groupby('categoria')['horas'].agg(['count', 'mean'])\nresumen"
SUMMARY_JSON = (
    "import json\n"
    "print(json.dumps({c: {'tickets': int(r['count']), 'media_horas': float(r['mean'])}"
    " for c, r in resumen.iterrows()}))"
)
CHART = """
import matplotlib.pyplot as plt
df.groupby('categoria')['horas'].mean().plot.bar(title='Horas medias por categoría', rot=0)
plt.tight_layout()
plt.show()
"""
PNG_OUT = Path("tickets.png")


def main() -> int:
    with Sandbox.create(TEMPLATE, timeout=600, metadata=METADATA) as sbx:
        try:
            sbx.files.write(CSV_SANDBOX, CSV_LOCAL.read_text(encoding="utf-8"))
            filas = run_cell(sbx, LOAD, "cargar el CSV").text
            print(f"filas cargadas: {filas}")
            print(run_cell(sbx, SUMMARY, "calcular el resumen").text)

            # El resumen vuelve como JSON por stdout y se compara con el contrato.
            stdout = "".join(run_cell(sbx, SUMMARY_JSON, "exportar el resumen").logs.stdout)
            errores = verificar.comparar(json.loads(stdout))
            if errores:
                print("el resumen NO cumple el contrato:", *errores, sep="\n  ", file=sys.stderr)
                return 1
            print("contrato: OK (3 categorías, 7 tickets)")

            chart = run_cell(sbx, CHART, "dibujar el gráfico")
            png = next((r.png for r in chart.results if r.png), None)
            if png is None:
                print("la celda del gráfico no devolvió ningún PNG", file=sys.stderr)
                return 1
            PNG_OUT.write_bytes(base64.b64decode(png))
            print(f"gráfico: {PNG_OUT} ({PNG_OUT.stat().st_size} bytes)")
        except CeldaFallida as exc:
            print(exc, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
