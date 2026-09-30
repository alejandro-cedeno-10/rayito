"""Convención compartida de opt-in para funciones que consumen cuota o dinero
de AWS (ADR-014, `docs/site/docs/optional-features.md`).

Una función opcional se activa con un kwarg nombrado (más, como mucho, un
kwarg con su objeto de configuración); `None` es "apagada". Ninguna variable
de entorno, fichero de configuración ni setter global activa una función de
coste. Un objeto de configuración crea su cliente boto3 de forma perezosa, en
su primer uso, nunca en su constructor.

Este módulo da el único helper de esa convención que necesita Python:
`require_module`, para dependencias opcionales instalables como extra de
`pip` (por ejemplo `opentelemetry`, vía `rayito[otel]`). No importa nada
opcional a nivel de módulo: importar `rayito` nunca importa un paquete que
sólo hace falta cuando el llamante activa la función correspondiente.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from rayito.exceptions import InvalidArgumentException


def require_module(module: str, *, extra: str, feature: str) -> ModuleType:
    """Importa `module` bajo demanda para una función opcional ya activada.

    Se llama sólo desde dentro de la función que el llamante activó con su
    propia opción (nunca a nivel de módulo): así, no tener el extra instalado
    no afecta a nadie que no use esa función. Sólo se traduce a
    `InvalidArgumentException` (con la instrucción de instalación exacta) el
    `ModuleNotFoundError` de `module` mismo o de uno de sus paquetes padre;
    cualquier otro error de la importación (por ejemplo una dependencia
    transitiva rota de un paquete ya instalado, o un fallo interno del
    paquete) se propaga tal cual, porque no es un problema de "falta el
    extra".

    Args:
        module: nombre del módulo a importar (`opentelemetry`, `opentelemetry.trace`).
        extra: nombre del extra de `pyproject.toml` que lo instala (`otel`).
        feature: nombre de la función que lo necesita, para el mensaje de error.

    Returns:
        El módulo importado.

    Raises:
        InvalidArgumentException: si `module` no está instalado.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        if not _is_missing_requested_module(exc, module):
            raise
        raise InvalidArgumentException(
            f"{feature} necesita el paquete opcional '{module}': "
            f"instala pip install rayito[{extra}]"
        ) from exc


def _is_missing_requested_module(exc: ImportError, module: str) -> bool:
    """`True` sólo cuando lo que falta es `module` mismo o uno de sus
    paquetes padre (p. ej. `module="opentelemetry.trace"` y falta
    `opentelemetry`), nunca una dependencia anidada de un paquete ya
    instalado: `ModuleNotFoundError.name` es siempre el nombre exacto del
    módulo que Python no pudo encontrar."""
    if not isinstance(exc, ModuleNotFoundError) or exc.name is None:
        return False
    candidates = {module}
    parts = module.split(".")
    candidates.update(".".join(parts[:i]) for i in range(1, len(parts)))
    return exc.name in candidates
