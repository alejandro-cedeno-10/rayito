"""Pool de sandboxes: preparación, toma y primera celda medidas por separado.

En 0.15.0, `start()` (o `with`) lanza el relleno en segundo plano y vuelve enseguida;
un `take()` inmediato, sin plaza lista, hace un `create()` normal (un fallo de
caché, `misses`). Para medir la toma de una plaza preparada hay que esperar a
que `stats().ready` llegue a 1 *antes* de poner el cronómetro, con un límite.
La plaza tomada es un sandbox normal: se termina aunque falle la celda. Al salir
del `with`, `close(drain=True)` termina las plazas que queden."""

import os
import sys
import time

from rayito import PoolConfig, Sandbox, SandboxPool

from _common import METADATA, TEMPLATE, run_cell

FILL_LIMIT_SECONDS = float(os.environ.get("POOL_FILL_LIMIT_SECONDS", "120"))
POLL_INTERVAL_SECONDS = 0.5
SANDBOX_TIMEOUT_SECONDS = 300
FIRST_CELL_TIMEOUT_SECONDS = 30.0
SLOT_LIFETIME_SECONDS = 3600
"""Vida máxima de cada plaza, también suspendida."""
SLOT_MIN_REMAINING_SECONDS = 600
"""Por debajo de esto la plaza se recicla."""
CONFIG = PoolConfig(
    size=1,
    template=TEMPLATE,
    timeout=SLOT_LIFETIME_SECONDS,
    min_remaining_seconds=SLOT_MIN_REMAINING_SECONDS,
    metadata=METADATA,
)


def measure_create() -> None:
    """Mide `create()` y la primera celda de un sandbox nuevo."""
    t0 = time.perf_counter()
    with Sandbox.create(TEMPLATE, timeout=SANDBOX_TIMEOUT_SECONDS, metadata=METADATA) as sbx:
        t1 = time.perf_counter()
        run_cell(sbx, "1 + 1", "primera celda", timeout_seconds=FIRST_CELL_TIMEOUT_SECONDS)
        t2 = time.perf_counter()
    print(f"create(): {t1 - t0:.2f} s | primera celda: {t2 - t1:.2f} s")


def wait_until_ready(pool: SandboxPool, started: float) -> bool:
    """Espera explícita y acotada a que el pool tenga una plaza lista."""
    while pool.stats().ready < 1:
        if time.perf_counter() - started > FILL_LIMIT_SECONDS:
            return False
        time.sleep(POLL_INTERVAL_SECONDS)
    return True


def measure_pool() -> int:
    """Mide la toma de una plaza preparada y su primera celda."""
    t0 = time.perf_counter()
    with SandboxPool(CONFIG) as pool:
        if not wait_until_ready(pool, t0):
            print(f"el pool no tuvo plaza lista en {FILL_LIMIT_SECONDS:.0f} s; no mido la toma", file=sys.stderr)
            return 1
        prepared = time.perf_counter() - t0
        before = pool.stats()

        t1 = time.perf_counter()
        sbx = pool.take()
        t2 = time.perf_counter()
        try:
            run_cell(sbx, "1 + 1", "primera celda", timeout_seconds=FIRST_CELL_TIMEOUT_SECONDS)
            t3 = time.perf_counter()
        finally:
            sbx.kill()

        after = pool.stats()
        hit = after.hits - before.hits == 1
        print(f"preparación del pool: {prepared:.2f} s (fuera del cronómetro de la toma)")
        print(f"take(): {t2 - t1:.2f} s | primera celda: {t3 - t2:.2f} s")
        print("plaza preparada (hit)" if hit else "fallback a create() (miss)")
        print(f"hits={after.hits} misses={after.misses} launched={after.launched}")
    return 0


def main() -> int:
    """Mide primero `create()` y después el pool."""
    measure_create()
    return measure_pool()


if __name__ == "__main__":
    sys.exit(main())
