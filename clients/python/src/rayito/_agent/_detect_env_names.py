"""Las variables de entorno que un proyecto declara en su `.env.example` (o
`.env.sample`, `.env.template`), para avisar de las que no reciben valor al
desplegar (`--check-env`, ADR-033 D4). Reutiliza el recorrido de la
detección (`ProjectIndex`, `envExampleFiles`) y sólo lee nombres: nunca
interpreta un valor, ni siquiera de ejemplo. `DetectionReport` no cambia.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any, Final

from rayito._agent._detect_scan import ProjectIndex, ProjectTree, WarningSink
from rayito._agent._detection_rules import DETECTION_RULES
from rayito._agent._secret_env_domain import dotenv_names

ENV_MISSING_WARNING: Final = "env_missing"


def required_env_names(
    tree: ProjectTree, rules: Mapping[str, Any] = DETECTION_RULES
) -> tuple[str, ...]:
    """Los nombres de los ficheros de ejemplo de la raíz, en el orden de
    `envExampleFiles` y de aparición, sin repetir."""
    index = ProjectIndex(tree, rules, WarningSink(rules))
    names: list[str] = []
    for file in rules["envExampleFiles"]:
        text = index.read(file) if index.has_root_file(file) else None
        if text is not None:
            names.extend(dotenv_names(text))
    return tuple(dict.fromkeys(names))


def missing_env_names(
    required: Sequence[str], covered: Collection[str], open_prefixes: Sequence[str] = ()
) -> tuple[str, ...]:
    """Los nombres de `required` que nada cubre: ni `covered` ni un prefijo de
    `open_prefixes` (un `secret_env_from` sin `keys`, cuyas variables sólo se
    conocen al leer el secreto; el prefijo vacío lo cubre todo)."""
    return tuple(
        name
        for name in required
        if name not in covered and not any(name.startswith(prefix) for prefix in open_prefixes)
    )


def env_missing_warnings(
    names: Sequence[str], rules: Mapping[str, Any] = DETECTION_RULES
) -> tuple[str, ...]:
    """El aviso `env_missing` de cada nombre, con el texto de las reglas."""
    sink = WarningSink(rules)
    for name in names:
        sink.add(ENV_MISSING_WARNING, name=name)
    return tuple(warning.detail for warning in sink.items)
