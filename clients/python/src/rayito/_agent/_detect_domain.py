"""`DetectOverrides`: lo que el llamante fija a mano frente a la detección de
`AgentProject.detect()` (ADR-029, D1). Es un valor puro; el orden de
precedencia es fichero de proyecto > argumento (`--framework`, `--language`,
`--entry`, `--protocol`) > detección > valor por defecto con nombre.
"""

from __future__ import annotations

from dataclasses import dataclass

from rayito.exceptions import InvalidArgumentException


@dataclass(frozen=True)
class DetectOverrides:
    """Lo que se fija sin esperar a la detección: `framework`, `language`,
    `entry` (el objeto o la fábrica del agente, `modulo:atributo`) y
    `protocol`. `None` deja que decida la detección."""

    framework: str | None = None
    language: str | None = None
    entry: str | None = None
    protocol: str | None = None

    def __post_init__(self) -> None:
        for label in ("framework", "language", "entry", "protocol"):
            value = getattr(self, label)
            if value is not None and (not isinstance(value, str) or not value):
                raise InvalidArgumentException(
                    f"DetectOverrides.{label} debe ser una cadena no vacía o None"
                )
