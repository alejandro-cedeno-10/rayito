"""`OptionalStacks` síncrono (M15 foundations, ADR-016): `deploy`/`status`/
`destroy`/`components` sobre el catálogo de `_registry.py`, a través del
puerto `StackProvisioner`. Nunca se invoca implícitamente: ningún
`create()`, listado o getter del SDK lo llama; sólo una llamada explícita
de este servicio (o `rayito stack`) despliega, cambia o borra algo.

Cada feature expone además una fachada fina sobre esto propio
(`LifecycleEvents.deploy()`, `CustomDomain.deploy()`, ...) en su propio
cambio; este módulo es lo que esas fachadas envuelven.
"""

from __future__ import annotations

from typing import Final, NamedTuple

import boto3

from rayito._stacks._cloudformation import CloudFormationProvisioner
from rayito._stacks._model import (
    DeployPlan,
    ParameterChange,
    ParameterPlan,
    StackComponent,
    StackStatus,
    plan_deploy,
    plan_parameters,
    reject_unknown_parameters,
    stack_tags,
)
from rayito._stacks._packaging import artifact_key, load_artifact, load_template
from rayito._stacks._port import StackProvisioner
from rayito._stacks._registry import COMPONENTS, component_by_name
from rayito.exceptions import InvalidArgumentException, StackException, UnimplementedError

#: Una pila sin recursos anidados (todas las de este catálogo) termina en
#: segundos a minutos; diez minutos deja margen generoso sin bloquear para
#: siempre una pila realmente atascada.
DEFAULT_WAIT_TIMEOUT_SECONDS: Final = 600.0


def _resolve_component(component: str | StackComponent) -> StackComponent:
    if isinstance(component, StackComponent):
        return component
    resolved = component_by_name(component)
    if resolved is None:
        names = ", ".join(sorted(c.name for c in COMPONENTS))
        raise InvalidArgumentException(
            f"componente desconocido: {component!r} (disponibles: {names})"
        )
    return resolved


def _require_supported(component: StackComponent) -> None:
    if not component.supported:
        raise UnimplementedError(
            f"rayito stack deploy {component.name}",
            f"{component.name} todavía no tiene plantilla: {component.description}",
        )


def _with_artifact_bucket(
    component: StackComponent, given: dict[str, str], artifact_bucket: str | None
) -> dict[str, str]:
    """`given` más, para cada artefacto con `bucket_parameter_key`, ese
    parámetro con `artifact_bucket` (el bucket al que `deploy()` sube el
    código). Uno ya pasado con otro valor es `InvalidArgumentException`: la
    plantilla apuntaría a un bucket donde el código no está."""
    resolved = dict(given)
    if not artifact_bucket:
        return resolved
    for artifact in component.artifacts:
        key = artifact.bucket_parameter_key
        if key is None:
            continue
        if resolved.get(key, artifact_bucket) != artifact_bucket:
            raise InvalidArgumentException(
                f"{component.name}: el parámetro {key} debe ser el mismo bucket que "
                "artifact_bucket= (es donde se sube el código); omítelo"
            )
        resolved[key] = artifact_bucket
    return resolved


def _require_artifact_bucket(component: StackComponent, artifact_bucket: str | None) -> None:
    if component.artifacts and not artifact_bucket:
        raise InvalidArgumentException(
            f"{component.name} necesita artifact_bucket=: sube el código Lambda del componente"
        )


def _require_deployable(plan: DeployPlan, stack_name: str) -> None:
    if plan.action == "blocked":
        raise StackException(plan.reason or f"deploy de {stack_name!r} bloqueado", code="blocked")


class _Plan(NamedTuple):
    component: StackComponent
    stack_name: str
    current: StackStatus | None
    parameters: ParameterPlan


class OptionalStacks:
    """Construirlo no hace ninguna llamada a AWS."""

    def __init__(
        self,
        *,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        provisioner: StackProvisioner | None = None,
    ) -> None:
        self._provisioner: StackProvisioner = provisioner or CloudFormationProvisioner(
            region=region, session=session
        )

    def components(self) -> tuple[StackComponent, ...]:
        """Metadata pura del catálogo; nunca llama a AWS."""
        return COMPONENTS

    def status(
        self, component: str | StackComponent, *, stack_name: str | None = None
    ) -> StackStatus | None:
        resolved = _resolve_component(component)
        name = stack_name or resolved.default_stack_name
        return self._provisioner.describe(name)

    def parameter_changes(
        self,
        component: str | StackComponent,
        *,
        stack_name: str | None = None,
        parameters: dict[str, str] | None = None,
        artifact_bucket: str | None = None,
    ) -> tuple[ParameterChange, ...]:
        """Qué parámetros cambiaría `deploy()` con estos mismos argumentos,
        sin desplegar nada (sólo un `DescribeStacks`): lo que `rayito stack
        deploy` imprime antes de pedir confirmación. Los que no se pasan y la
        pila ya tiene no aparecen: `deploy()` los conserva."""
        plan = self._plan(component, stack_name, parameters, artifact_bucket)
        return plan.parameters.changes(plan.current)

    def deploy(
        self,
        component: str | StackComponent,
        *,
        stack_name: str | None = None,
        parameters: dict[str, str] | None = None,
        artifact_bucket: str | None = None,
        tags: dict[str, str] | None = None,
        wait: bool = True,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    ) -> StackStatus:
        """Crea la pila o, si ya existe, la actualiza. Al crear, los
        parámetros no pasados toman su valor por defecto del catálogo; al
        actualizar, los no pasados conservan el valor con el que la pila está
        desplegada (`UsePreviousValue`), así redesplegar sin repetir cada
        parámetro no deshace la configuración anterior."""
        plan = self._plan(component, stack_name, parameters, artifact_bucket)
        resolved, name = plan.component, plan.stack_name
        values = dict(plan.parameters.values)
        if resolved.artifacts and artifact_bucket:
            data = load_artifact(resolved)
            key = artifact_key(resolved, data)
            self._provisioner.put_artifact(artifact_bucket, key, data)
            for artifact in resolved.artifacts:
                values[artifact.parameter_key] = key
        template_body = load_template(resolved)
        full_tags = stack_tags(resolved, dict(tags or {}))
        if plan_deploy(plan.current).action == "create":
            self._provisioner.create(
                resolved,
                stack_name=name,
                template_body=template_body,
                parameters=values,
                tags=full_tags,
            )
        else:
            self._provisioner.update(
                resolved,
                stack_name=name,
                template_body=template_body,
                parameters=values,
                tags=full_tags,
                keep_previous=plan.parameters.keep_previous,
            )
        if wait:
            self._provisioner.wait(name, "deployed", wait_timeout)
        status = self._provisioner.describe(name)
        if status is None:
            raise StackException(
                f"la pila {name!r} desapareció justo tras desplegarla", code="failed"
            )
        return status

    def _plan(
        self,
        component: str | StackComponent,
        stack_name: str | None,
        parameters: dict[str, str] | None,
        artifact_bucket: str | None,
    ) -> _Plan:
        """Lo común a `deploy()` y `parameter_changes()`: valida todo lo que
        no necesita AWS antes de la única llamada (`DescribeStacks`), y
        decide los parámetros según exista o no la pila."""
        resolved = _resolve_component(component)
        _require_supported(resolved)
        name = stack_name or resolved.default_stack_name
        _require_artifact_bucket(resolved, artifact_bucket)
        given = _with_artifact_bucket(resolved, dict(parameters or {}), artifact_bucket)
        reject_unknown_parameters(resolved, given)
        current = self._provisioner.describe(name)
        _require_deployable(plan_deploy(current), name)
        return _Plan(resolved, name, current, plan_parameters(resolved, given, current))

    def destroy(
        self,
        component: str | StackComponent,
        *,
        stack_name: str | None = None,
        wait: bool = True,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        resolved = _resolve_component(component)
        _require_supported(resolved)
        name = stack_name or resolved.default_stack_name
        self._provisioner.delete(name)
        if wait:
            self._provisioner.wait(name, "deleted", wait_timeout)
