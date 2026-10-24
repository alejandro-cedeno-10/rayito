"""El caso: resumen por categoría del CSV de tickets, comprobado contra el contrato.

El resumen vuelve como JSON por stdout y se compara con `contract/expected.json`.
Sale con 1 si una celda falla, si no se cumple el contrato o si no hay PNG."""

import base64
import json
import sys
from pathlib import Path

from rayito import Sandbox

from _common import LOCAL_CSV, METADATA, SANDBOX_CSV, TEMPLATE, CellFailedException, run_cell, verify

SANDBOX_TIMEOUT_SECONDS = 600
LOAD = f"import pandas as pd\ndf = pd.read_csv('{SANDBOX_CSV}')\nlen(df)"
SUMMARY = "summary = df.groupby('categoria')['horas'].agg(['count', 'mean'])\nsummary"
SUMMARY_JSON = (
    "import json\n"
    "print(json.dumps({c: {'tickets': int(r['count']), 'mean_hours': float(r['mean'])}"
    " for c, r in summary.iterrows()}))"
)
CHART = """
import matplotlib.pyplot as plt
df.groupby('categoria')['horas'].mean().plot.bar(title='Horas medias por categoría', rot=0)
plt.tight_layout()
plt.show()
"""
PNG_OUT = Path("tickets.png")


def summarize(sbx: Sandbox) -> int:
    """Carga el CSV, calcula el resumen, lo compara con el contrato y guarda el gráfico."""
    sbx.files.write(SANDBOX_CSV, LOCAL_CSV.read_text(encoding="utf-8"))
    rows = run_cell(sbx, LOAD, "cargar el CSV").text
    print(f"filas cargadas: {rows}")
    print(run_cell(sbx, SUMMARY, "calcular el resumen").text)

    output = "".join(run_cell(sbx, SUMMARY_JSON, "exportar el resumen").logs.stdout)
    errors = verify.compare(json.loads(output))
    if errors:
        print("el resumen NO cumple el contrato:", *errors, sep="\n  ", file=sys.stderr)
        return 1
    contract = verify.expected()
    print(f"contrato: OK ({len(contract['categories'])} categorías, {contract['total']} tickets)")

    chart = run_cell(sbx, CHART, "dibujar el gráfico")
    png = next((result.png for result in chart.results if result.png), None)
    if png is None:
        print("la celda del gráfico no devolvió ningún PNG", file=sys.stderr)
        return 1
    PNG_OUT.write_bytes(base64.b64decode(png))
    print(f"gráfico: {PNG_OUT} ({PNG_OUT.stat().st_size} bytes)")
    return 0


def main() -> int:
    """Crea el sandbox y convierte un fallo de celda en código de salida 1."""
    with Sandbox.create(TEMPLATE, timeout=SANDBOX_TIMEOUT_SECONDS, metadata=METADATA) as sbx:
        try:
            return summarize(sbx)
        except CellFailedException as exc:
            print(exc, file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
