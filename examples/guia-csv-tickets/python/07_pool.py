"""Pool de sandboxes: preparación, toma y primera celda medidas por separado.

En 0.15.0, start() (o `with`) lanza el relleno en segundo plano y vuelve enseguida;
un take() inmediato, sin plaza lista, hace un create() normal (un fallo de
caché, `misses`). Para medir la toma de una plaza preparada hay que esperar a
que stats().ready llegue a 1 *antes* de poner el cronómetro, con un límite."""

import os
import sys
import time

from rayito import PoolConfig, Sandbox, SandboxPool

from _comun import METADATA, TEMPLATE, run_cell

FILL_LIMIT_S = float(os.environ.get("POOL_FILL_LIMIT_S", "120"))
CONFIG = PoolConfig(
    size=1,
    template=TEMPLATE,
    timeout=3600,  # vida máxima de cada plaza, también suspendida
    min_remaining_seconds=600,  # por debajo de esto la plaza se recicla
    metadata=METADATA,
)


def medir_create() -> None:
    t0 = time.perf_counter()
    with Sandbox.create(TEMPLATE, timeout=300, metadata=METADATA) as sbx:
        t1 = time.perf_counter()
        run_cell(sbx, "1 + 1", "primera celda", timeout=30)
        t2 = time.perf_counter()
    print(f"create(): {t1 - t0:.2f} s | primera celda: {t2 - t1:.2f} s")


def medir_pool() -> int:
    t0 = time.perf_counter()
    with SandboxPool(CONFIG) as pool:  # al salir, close(drain=True) termina las plazas
        while pool.stats().ready < 1:  # espera explícita y acotada
            if time.perf_counter() - t0 > FILL_LIMIT_S:
                print(f"el pool no tuvo plaza lista en {FILL_LIMIT_S:.0f} s; no mido la toma", file=sys.stderr)
                return 1
            time.sleep(0.5)
        preparado = time.perf_counter() - t0
        antes = pool.stats()

        t1 = time.perf_counter()
        sbx = pool.take()
        t2 = time.perf_counter()
        try:
            run_cell(sbx, "1 + 1", "primera celda", timeout=30)
            t3 = time.perf_counter()
        finally:
            sbx.kill()

        despues = pool.stats()
        acierto = despues.hits - antes.hits == 1
        print(f"preparación del pool: {preparado:.2f} s (fuera del cronómetro de la toma)")
        print(f"take(): {t2 - t1:.2f} s | primera celda: {t3 - t2:.2f} s")
        print("plaza preparada (hit)" if acierto else "fallback a create() (miss)")
        print(f"hits={despues.hits} misses={despues.misses} launched={despues.launched}")
    return 0


def main() -> int:
    medir_create()
    return medir_pool()


if __name__ == "__main__":
    sys.exit(main())
