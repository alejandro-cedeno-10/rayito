"""Qué termina el sweeper de sesión de los e2e de Python (`conftest.py`).

Regla: la sesión sólo termina lo que ella misma creó. El `control_plane` de la
sesión apunta cada id que devuelve `run_microvm`; al acabar, un MicroVM vivo de
la imagen se termina sólo si está en ese registro. Los demás (los que ya
existían al empezar y los que nacieron durante la sesión por otro camino, de
otra persona o de otra sesión) no se tocan y se informan por número, nunca por
id. Lo que nace por caminos que no pasan por el `control_plane` (el corpus E2B
y la CLI en subprocesos) lo acota el `maximumDurationInSeconds=900` de todo
sandbox de test.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass


@dataclass(frozen=True)
class SweepPlan:
    """`terminate`: ids propios aún vivos, en orden estable. `preexisting` y
    `unregistered`: cuántos vivos se dejan, sin sus ids."""

    terminate: tuple[str, ...]
    preexisting: int
    unregistered: int

    def report(self) -> str | None:
        """La nota para el informe de la sesión, o `None` si no se deja nada."""
        if not (self.preexisting or self.unregistered):
            return None
        return (
            f"sweeper: {self.preexisting} MicroVMs que ya existían antes de la sesión y "
            f"{self.unregistered} nacidos durante ella fuera del plano de control de la "
            "sesión: no se tocan"
        )


def plan_session_sweep(
    baseline: Collection[str], registered: Collection[str], remaining: Collection[str]
) -> SweepPlan:
    """`baseline`: vivos al empezar; `registered`: ids que creó el plano de la
    sesión; `remaining`: vivos al acabar."""
    own = set(registered) - set(baseline)
    live = set(remaining)
    preexisting = live & set(baseline)
    return SweepPlan(
        terminate=tuple(sorted(live & own)),
        preexisting=len(preexisting),
        unregistered=len(live - preexisting - own),
    )
