"""Catálogo estático de componentes `OptionalStack` (M15 foundations,
ADR-016). Foundations escribe esta tupla una sola vez, en el orden del
§4.5 de la arquitectura de M15; cada feature sustituye sólo el `COMPONENT`
de su propio módulo en `components/`, nunca esta lista. `components()` no
hace ninguna llamada a AWS: es pura metadata.
"""

from __future__ import annotations

from rayito._stacks._model import StackComponent
from rayito._stacks.components import (
    custom_domain,
    efs_volumes,
    events_webhooks,
    metadata_index,
    otlp_export,
    s3_mounts,
    secrets_access,
    sizes_guard,
    templates,
)

COMPONENTS: tuple[StackComponent, ...] = (
    metadata_index.COMPONENT,
    secrets_access.COMPONENT,
    efs_volumes.COMPONENT,
    s3_mounts.COMPONENT,
    sizes_guard.COMPONENT,
    events_webhooks.COMPONENT,
    otlp_export.COMPONENT,
    templates.COMPONENT,
    custom_domain.COMPONENT,
)

_BY_NAME: dict[str, StackComponent] = {component.name: component for component in COMPONENTS}


def component_by_name(name: str) -> StackComponent | None:
    return _BY_NAME.get(name)
