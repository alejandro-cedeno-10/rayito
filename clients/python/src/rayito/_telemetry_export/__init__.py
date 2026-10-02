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
    image_memory_mib_from_guest_bytes,
    plan,
)
from ._section import (
    TelemetryExportSection,
    aresolve_bearer_token,
    build_section,
    require_telemetry_support,
    resolve_bearer_token,
)

__all__ = [
    "DEFAULT_INTERVAL_S",
    "MAX_INTERVAL_S",
    "MIN_INTERVAL_S",
    "OtlpAuth",
    "TelemetryExport",
    "TelemetryExportSection",
    "TelemetryHealth",
    "aresolve_bearer_token",
    "build_section",
    "image_memory_mib_from_guest_bytes",
    "plan",
    "require_telemetry_support",
    "resolve_bearer_token",
]
