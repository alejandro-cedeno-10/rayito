"""Puerto `StackProvisioner` (M15 foundations, ADR-016): lo que
`OptionalStacks` necesita de CloudFormation, sin saber si detrás hay boto3
de verdad o un fake de test."""

from __future__ import annotations

from typing import Literal, Protocol

from rayito._stacks._model import StackComponent, StackStatus

UpdateOutcome = Literal["changed", "no_changes"]
DeployTarget = Literal["deployed", "deleted"]


class StackProvisioner(Protocol):
    def describe(self, stack_name: str) -> StackStatus | None:
        """`None` si la pila no existe."""
        ...

    def create(
        self,
        component: StackComponent,
        *,
        stack_name: str,
        template_body: str,
        parameters: dict[str, str],
        tags: dict[str, str],
    ) -> None: ...

    def update(
        self,
        component: StackComponent,
        *,
        stack_name: str,
        template_body: str,
        parameters: dict[str, str],
        tags: dict[str, str],
        keep_previous: tuple[str, ...] = (),
    ) -> UpdateOutcome:
        """`"no_changes"` cuando CloudFormation responde el `ValidationError`
        "No updates are to be performed" (no es un fallo). `keep_previous`
        son los parámetros que se mandan con `UsePreviousValue=True`
        (`ParameterPlan.keep_previous`)."""
        ...

    def delete(self, stack_name: str) -> None:
        """Idempotente: borrar una pila que no existe no es un error."""
        ...

    def wait(self, stack_name: str, target: DeployTarget, timeout: float) -> None:
        """Bloquea hasta que la pila alcanza un estado terminal del target
        pedido o `timeout` expira (`StackException(code="in_progress")`)."""
        ...

    def put_artifact(self, bucket: str, key: str, data: bytes) -> None:
        """Sube `data` a `bucket/key` salvo que el objeto ya exista con
        exactamente ese contenido (se compara, nunca basta con que la clave
        exista): idempotente, para que desplegar dos veces el mismo
        artefacto no vuelva a subirlo, y sin fiarse de un objeto plantado.
        Sólo sobre un bucket de la cuenta del llamante."""
        ...

    def failure_reason(self, stack_name: str) -> str | None:
        """El `StackStatusReason` más reciente, o `None` si la pila no
        existe o no falló."""
        ...
