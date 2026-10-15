"""La decisión pura: en qué nivel está el uso, si hay que avisar y qué se
dice. Nada de AWS. Un aviso sale cuando el nivel *sube* (de `ok` a
`warning`, de `warning` a `critical`); mientras el uso se mantiene en el mismo
nivel no se repite. El nivel que se recuerda es el más alto alcanzado y sólo
baja al volver a `ok` (`level_to_remember`): un uso que oscila alrededor del
umbral crítico avisa una vez, no cada vez que cruza, y tras volver a `ok` el
siguiente cruce vuelve a avisar."""

from __future__ import annotations

from typing import Final, Literal

from rayito._percent import PERCENT

Level = Literal["ok", "warning", "critical"]

LEVEL_RANK: Final[dict[Level, int]] = {"ok": 0, "warning": 1, "critical": 2}
LEVEL_LABEL: Final[dict[Level, str]] = {
    "ok": "normal",
    "warning": "alto",
    "critical": "crítico",
}
SUBJECT_MAX_CHARS: Final = 100


def classify(fraction: float | None, warning: float, critical: float) -> Level:
    """Sin cuota conocida (`None`) no hay nivel que avisar."""
    if fraction is None:
        return "ok"
    if fraction >= critical:
        return "critical"
    if fraction >= warning:
        return "warning"
    return "ok"


def parse_level(value: object) -> Level:
    """El nivel guardado; cualquier cosa que no sea uno de los tres vale `ok`,
    para que una fila rara nunca silencie un aviso."""
    return value if value in LEVEL_RANK else "ok"  # type: ignore[return-value]


def should_notify(previous: Level, current: Level) -> bool:
    return LEVEL_RANK[current] > LEVEL_RANK[previous]


def level_to_remember(previous: Level, current: Level) -> Level:
    """El nivel que se guarda tras una ejecución: el más alto entre el anterior
    y el actual, salvo que el uso haya vuelto a `ok`, que reinicia el estado."""
    if current == "ok":
        return "ok"
    return max(previous, current, key=LEVEL_RANK.__getitem__)


def subject(level: Level, region: str, fraction: float) -> str:
    text = (
        f"Rayito: memoria de MicroVMs {LEVEL_LABEL[level]} "
        f"({fraction * PERCENT:.0f} % de la cuota, {region})"
    )
    return text[:SUBJECT_MAX_CHARS]


def body(
    level: Level,
    region: str,
    allocated_gb: float,
    quota_gb: float,
    sandboxes: int,
    unresolved: int,
) -> str:
    """El cuerpo del aviso: región, cifras y qué hacer. Nunca ids de sandbox,
    de cuenta ni nombres de imágenes."""
    lines = [
        f"La memoria asignada a MicroVMs en {region} está en nivel {LEVEL_LABEL[level]}.",
        f"Asignada: {allocated_gb:g} GB de {quota_gb:g} GB ({sandboxes} sandboxes, "
        "RUNNING más suspendidos).",
    ]
    if unresolved:
        lines.append(
            f"No se pudo leer la memoria de {unresolved} sandboxes: el uso real puede ser mayor."
        )
    lines.append(
        "Pide un aumento de la cuota en Service Quotas antes de lanzar más sandboxes, "
        "o termina los que ya no uses."
    )
    return "\n".join(lines)
