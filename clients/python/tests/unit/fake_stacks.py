"""`StackProvisioner` falso en memoria (M15 foundations): una pila por
nombre, con el estado que el test programe. Compartido por foundations y
por cada feature que necesite probar su propia fachada
(`LifecycleEvents.deploy()`, ...) sin tocar AWS; nunca se edita salvo para
el `StackProvisioner` en sí (`_port.py`)."""

from __future__ import annotations

from dataclasses import dataclass, field

from rayito._stacks._model import StackComponent, StackStatus
from rayito._stacks._port import DeployTarget, UpdateOutcome
from rayito.exceptions import StackException


@dataclass
class FakeStackProvisioner:
    #: `stack_name -> StackStatus`; ausente = la pila no existe.
    stacks: dict[str, StackStatus] = field(default_factory=dict)
    #: Objetos subidos por `put_artifact`, por `(bucket, key)`.
    artifacts: dict[tuple[str, str], bytes] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    #: Los `parameters`/`keep_previous` de la última `create`/`update`.
    sent_parameters: dict[str, str] = field(default_factory=dict)
    sent_keep_previous: tuple[str, ...] = ()
    next_update_outcome: UpdateOutcome = "changed"
    fail_wait: bool = False
    #: Cada `timeout` que `wait()` recibió, en orden; permite a un test de
    #: una función concreta comprobar que le llegó el `wait_timeout` que
    #: esperaba (p. ej. `CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS`) sin tener que
    #: inspeccionar `calls`, cuyas tuplas no lo llevan.
    wait_timeouts: list[float] = field(default_factory=list)

    def describe(self, stack_name: str) -> StackStatus | None:
        self.calls.append(("describe", stack_name))
        return self.stacks.get(stack_name)

    def create(
        self,
        component: StackComponent,
        *,
        stack_name: str,
        template_body: str,
        parameters: dict[str, str],
        tags: dict[str, str],
    ) -> None:
        self.calls.append(("create", stack_name))
        self.sent_parameters = dict(parameters)
        self.sent_keep_previous = ()
        self.stacks[stack_name] = StackStatus(
            name=stack_name,
            state="CREATE_COMPLETE",
            outputs={"StackName": stack_name},
            parameters=dict(parameters),
        )

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
        self.calls.append(("update", stack_name))
        self.sent_parameters = dict(parameters)
        self.sent_keep_previous = keep_previous
        if self.next_update_outcome == "changed":
            existing = self.stacks.get(stack_name)
            outputs = existing.outputs if existing else {}
            previous = existing.parameters if existing else {}
            merged = {key: previous[key] for key in keep_previous if key in previous}
            merged.update(parameters)
            self.stacks[stack_name] = StackStatus(
                name=stack_name, state="UPDATE_COMPLETE", outputs=outputs, parameters=merged
            )
        return self.next_update_outcome

    def delete(self, stack_name: str) -> None:
        self.calls.append(("delete", stack_name))
        self.stacks.pop(stack_name, None)

    def wait(self, stack_name: str, target: DeployTarget, timeout: float) -> None:
        self.calls.append(("wait", stack_name, target))
        self.wait_timeouts.append(timeout)
        if self.fail_wait:
            raise StackException(f"tiempo agotado esperando {stack_name!r}", code="in_progress")

    def put_artifact(self, bucket: str, key: str, data: bytes) -> None:
        self.calls.append(("put_artifact", bucket, key))
        self.artifacts[(bucket, key)] = data

    def failure_reason(self, stack_name: str) -> str | None:
        self.calls.append(("failure_reason", stack_name))
        status = self.stacks.get(stack_name)
        return None if status is None else status.reason_code
