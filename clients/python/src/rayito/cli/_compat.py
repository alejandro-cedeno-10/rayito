"""Tabla de compatibilidad SDK ↔ `rayd` (`agent_version`).

Una fila por serie `MAJOR.MINOR` del SDK con el `agent_version` mínimo. Regla
del informe M7 §1: SDKs y `rayd` avanzan `MAJOR.MINOR` en lockstep (el patch
puede divergir). La versión de imagen (`imageVersion`) no entra en la tabla:
es el contador de builds de cada imagen en cada cuenta (`rayito-base` 17.0 y
`rayito-base-poly` 3.0 conviven con el mismo `rayd`, y en una cuenta nueva la
primera es 1.0), mientras que `agent_version` sale del binario dentro de la
imagen y ya prueba de qué tag se construyó. `docs/site/docs/limits.md` lleva
la misma tabla y `tests/unit/cli/test_compat.py` impide que diverjan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

SEMVER_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")

CompatibilityStatus = Literal["OK", "WARN", "FAIL"]


@dataclass(frozen=True)
class CompatibilityRow:
    sdk_series: str
    min_agent_version: str
    note: str


COMPATIBILITY: tuple[CompatibilityRow, ...] = (
    CompatibilityRow(
        "0.1",
        "0.1.0",
        "M6: imds_blocked, hook_anomalies y metadata exigen el rayd del tag rayd-v0.1.0",
    ),
    CompatibilityRow(
        "0.2",
        "0.2.0",
        "M7: Checkpoint/Restore (persist=) y language= exigen el rayd del tag rayd-v0.2.0",
    ),
)


@dataclass(frozen=True)
class Assessment:
    status: CompatibilityStatus
    reason: str
    row: CompatibilityRow | None


def parse_semver(text: str) -> tuple[int, int, int]:
    """`MAJOR.MINOR.PATCH` como enteros; un sufijo de pre-release (`-rc1`,
    `+build`) se ignora; cualquier otra forma es `ValueError`."""
    match = SEMVER_PATTERN.match(text.strip())
    if match is None:
        raise ValueError(f"versión no reconocida: {text!r} (se esperaba MAJOR.MINOR.PATCH)")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def series_of(version: tuple[int, int, int]) -> str:
    return f"{version[0]}.{version[1]}"


def row_for(sdk_series: str) -> CompatibilityRow | None:
    for row in COMPATIBILITY:
        if row.sdk_series == sdk_series:
            return row
    return None


def assess(sdk_version: str, agent_version: str) -> Assessment:
    """`FAIL` si el agente está por debajo del mínimo de la fila; `WARN` si el
    agente es más nuevo que el SDK en `MAJOR.MINOR` (funciones que el SDK no
    conoce); `OK` si no."""
    try:
        sdk = parse_semver(sdk_version)
        agent = parse_semver(agent_version)
    except ValueError as exc:
        return Assessment("FAIL", str(exc), None)
    row = row_for(series_of(sdk))
    if row is None:
        return Assessment("FAIL", f"sin fila de compatibilidad para el SDK {series_of(sdk)}", None)
    minimum_agent = parse_semver(row.min_agent_version)
    if agent < minimum_agent:
        return Assessment(
            "FAIL",
            f"agent_version {agent_version} por debajo del mínimo {row.min_agent_version} "
            f"del SDK {sdk_version}: publica una imagen desde el tag rayd-v{row.min_agent_version}",
            row,
        )
    if agent[:2] > sdk[:2]:
        return Assessment(
            "WARN",
            f"agent_version {agent_version} es más nuevo que el SDK {sdk_version} "
            f"({series_of(agent)} > {series_of(sdk)}): actualiza el SDK",
            row,
        )
    return Assessment("OK", f"SDK {sdk_version}, agent_version {agent_version}", row)
