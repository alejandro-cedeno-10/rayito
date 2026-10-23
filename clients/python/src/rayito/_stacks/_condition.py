"""Cómo se clasifica una pila de CloudFormation que Rayito observa: lo que
comparten `rayito setup` (ADR-030) y `rayito agent deploy --target agentcore`
(ADR-031). Puro: sólo la biblioteca estándar.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from rayito._stacks._model import StackStatus

StackCondition = Literal["missing", "usable", "in_progress", "blocked"]

#: Estados de una pila con los que sirve tal cual; `UPDATE_ROLLBACK_COMPLETE`
#: volvió a la última plantilla que funcionaba.
USABLE_STACK_STATES: Final = frozenset(
    {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}
)
IN_PROGRESS_STACK_SUFFIX: Final = "_IN_PROGRESS"
#: Una pila que se está borrando nunca llega a "deployed": esperarla no terminaría.
DELETING_STACK_STATE: Final = "DELETE_IN_PROGRESS"


@dataclass(frozen=True)
class StackObservation:
    """Lo que `DescribeStacks` dice de una pila de Rayito; `status` es `None`
    si la pila no existe."""

    name: str
    status: str | None
    outputs: Mapping[str, str]
    parameters: Mapping[str, str]
    #: Las etiquetas de la pila (`StackStatus.tags`); vacío si no existe.
    tags: Mapping[str, str] = field(default_factory=dict)


def classify_stack(stack: StackObservation) -> StackCondition:
    if stack.status is None:
        return "missing"
    if stack.status in USABLE_STACK_STATES:
        return "usable"
    if stack.status == DELETING_STACK_STATE:
        return "blocked"
    if stack.status.endswith(IN_PROGRESS_STACK_SUFFIX):
        return "in_progress"
    return "blocked"


def stack_observation(status: StackStatus | None, stack_name: str) -> StackObservation:
    """La `StackObservation` de lo que `OptionalStacks.status` devolvió; una
    pila que no existe es `status=None`."""
    if status is None:
        return StackObservation(stack_name, None, {}, {})
    return StackObservation(
        name=stack_name,
        status=status.state,
        outputs=dict(status.outputs),
        parameters=dict(status.parameters),
        tags=dict(status.tags),
    )
