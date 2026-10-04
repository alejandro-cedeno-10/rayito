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
from typing import Final

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


#: Dentro de `rayito/`, el espacio de nombres que las plantillas de IAM ya
#: protegen: sólo el publicador (`CallerPolicy`/`ImagePublisherPolicy`)
#: escribe ahí, el execution role tiene un `Deny` explícito y el builder de
#: templates sólo lee. Nunca la raíz del bucket, que cualquier otra política
#: del bucket podría permitir escribir.
STACK_ARTIFACT_PREFIX: Final = "rayito/stacks"


def artifact_key(component: StackComponent, data: bytes) -> str:
    """La clave S3 determinista de un artefacto,
    `rayito/stacks/<componente>/<sha256>.zip`: subir el mismo contenido dos
    veces es un no-op (`StackProvisioner.put_artifact` compara el contenido
    ya subido antes)."""
    return f"{STACK_ARTIFACT_PREFIX}/{component.name}/{hashlib.sha256(data).hexdigest()}.zip"
