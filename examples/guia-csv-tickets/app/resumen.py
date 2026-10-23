import sys

import pandas as pd
from tabulate import tabulate

df = pd.read_csv(sys.argv[1])
summary = df.groupby("categoria")["horas"].agg(["count", "mean"])
print(tabulate(summary, headers="keys", floatfmt=".1f"))
