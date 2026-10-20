"""Los destinos de `deploy_agent()` y la regla que los separa (ADR-031): las
opciones que sólo existen en AgentCore son un error con el destino MicroVM, antes
de cualquier llamada a AWS. Puro: lo importan la fachada de despliegue y la CLI
sin cargar el resto de `_agentcore`.
"""

from __future__ import annotations

from typing import Final

from rayito.exceptions import AgentCoreException, InvalidArgumentException

TARGET_MICROVM: Final = "microvm"
TARGET_AGENTCORE: Final = "agentcore"
TARGETS: Final = (TARGET_MICROVM, TARGET_AGENTCORE)
#: Cómo se nombra `AgentCoreOptions` en el error de `option_requires_agentcore`.
AGENTCORE_ONLY_OPTION: Final = "agentcore"


def require_target(target: str) -> None:
    """El destino debe ser uno de `TARGETS`."""
    if target not in TARGETS:
        raise InvalidArgumentException(f"target debe ser uno de {list(TARGETS)}")


def check_agentcore_options_target(agentcore: object, target: str) -> None:
    """Las opciones de AgentCore con el destino MicroVM son
    `option_requires_agentcore`."""
    if agentcore is not None and target != TARGET_AGENTCORE:
        raise AgentCoreException("option_requires_agentcore", option=AGENTCORE_ONLY_OPTION)
