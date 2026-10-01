"""m15-rayd-otlp (ADR-021): exportador OTLP/HTTP de `rayd` hacia CloudWatch.

API pública: `TelemetryExport`, `OtlpAuth`, `TelemetryHealth` (re-exportados
también desde `rayito`). El resto de este paquete (`_section`,
`_propagation`) son detalles de implementación de `sandbox_{sync,async}`.
"""

from __future__ import annotations

from ._domain import (
    DEFAULT_INTERVAL_S,
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    OtlpAuth,
    TelemetryExport,
    TelemetryHealth,
    plan,
)
from ._section import TelemetryExportSection, build_section, resolve_bearer_token

__all__ = [
    "DEFAULT_INTERVAL_S",
    "MAX_INTERVAL_S",
    "MIN_INTERVAL_S",
    "OtlpAuth",
    "TelemetryExport",
    "TelemetryExportSection",
    "TelemetryHealth",
    "build_section",
    "plan",
    "resolve_bearer_token",
]
