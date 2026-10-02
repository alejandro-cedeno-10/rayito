"""Límite de concurrencia de builds en proceso (investigación §3.4/TPL-1,
Q83: la cuenta acepta 10 builds concurrentes, `ServiceQuotaExceededException`
en el undécimo). `BuildSlots` es un guarda local, no un contador
distribuido: varios procesos pueden seguir topando con la cuota real de
AWS, que `_build.py` traduce aparte a la misma `BuildException`; esto sólo
evita que un único proceso (por ejemplo, una matriz de CI en el mismo
runner) dispare más de `MAX_CONCURRENT_BUILDS` peticiones a la vez.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from rayito.exceptions import BuildException

#: Cuota medida de builds concurrentes por cuenta (TPL-1/Q83); el undécimo
#: intento simultáneo da `ServiceQuotaExceededException` en AWS, así que el
#: SDK se queda uno por debajo y rechaza en el acto en vez de esperar.
MAX_CONCURRENT_BUILDS: Final = 10

_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_BUILDS)


@contextmanager
def build_slot() -> Iterator[None]:
    """Reserva un hueco de build para este proceso; si ya hay
    `MAX_CONCURRENT_BUILDS` builds en vuelo, lanza `BuildException` al
    instante (sin esperar y sin llamar a AWS)."""
    if not _SLOTS.acquire(blocking=False):
        raise BuildException(
            f"ya hay {MAX_CONCURRENT_BUILDS} builds de templates en marcha en este proceso "
            "(límite local, Q83); espera a que termine alguno",
            reason="build_quota",
        )
    try:
        yield
    finally:
        _SLOTS.release()
