"""La app que el template copia a /srv/app: resumen por categoría de un CSV de tickets."""

import sys

import pandas as pd
from tabulate import tabulate


def main() -> None:
    """Imprime recuento y media de horas por categoría del CSV que recibe como argumento."""
    frame = pd.read_csv(sys.argv[1])
    summary = frame.groupby("categoria")["horas"].agg(["count", "mean"])
    print(tabulate(summary, headers="keys", floatfmt=".1f"))


if __name__ == "__main__":
    main()
