"""Lectura de las plantillas y artefactos empaquetados con el SDK (M15
foundations, ADR-016). `scripts/gen_stack_assets.py` las renderiza de forma
determinista desde `infra/<component>.yaml` (y, si hay código Lambda, desde
`infra/lambdas/<component>/`) en tiempo de desarrollo/CI; en tiempo de
ejecución el SDK sólo las lee de su propio paquete, nunca las genera ni
necesita el repositorio presente.
"""

from __future__ import annotations

import hashlib
from importlib import resources

from rayito._stacks._model import StackComponent

#: Ancla en el paquete `rayito._stacks` (tiene `__init__.py`); `_templates`
#: y `_artifacts` son subdirectorios de datos sin `__init__.py` propio,
#: alcanzables con `joinpath` igual que cualquier otro recurso empaquetado.
STACKS_PACKAGE = "rayito._stacks"


def load_template(component: StackComponent) -> str:
    """El YAML de `infra/<component>.yaml`, empaquetado en el SDK."""
    return (
        resources.files(STACKS_PACKAGE)
        .joinpath("_templates", f"{component.name}.yaml")
        .read_text(encoding="utf-8")
    )


def load_artifact(component: StackComponent) -> bytes:
    """El zip determinista del código Lambda del componente; sólo se llama
    para un componente con `artifacts` no vacío."""
    return (
        resources.files(STACKS_PACKAGE).joinpath("_artifacts", f"{component.name}.zip").read_bytes()
    )


def artifact_key(data: bytes) -> str:
    """La clave S3 determinista de un artefacto (su sha256 en hex): subir
    el mismo contenido dos veces es un no-op (`StackProvisioner.put_artifact`
    hace `HeadObject` antes)."""
    return hashlib.sha256(data).hexdigest()
