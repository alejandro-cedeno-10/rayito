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

from typing import Final

import boto3

from rayito._stacks._cloudformation import CloudFormationProvisioner
from rayito._stacks._model import StackComponent, StackStatus, plan_deploy, stack_tags
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


def _resolved_parameters(component: StackComponent, given: dict[str, str]) -> dict[str, str]:
    known = {parameter.name for parameter in component.parameters}
    unknown = sorted(set(given) - known)
    if unknown:
        raise InvalidArgumentException(f"parámetros desconocidos para {component.name}: {unknown}")
    resolved = {
        parameter.name: parameter.default
        for parameter in component.parameters
        if parameter.default is not None
    }
    resolved.update(given)
    missing = sorted(
        parameter.name
        for parameter in component.parameters
        if parameter.required and parameter.name not in resolved
    )
    if missing:
        raise InvalidArgumentException(
            f"faltan parámetros obligatorios para {component.name}: {missing}"
        )
    return resolved


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
        resolved = _resolve_component(component)
        _require_supported(resolved)
        name = stack_name or resolved.default_stack_name
        resolved_parameters = _resolved_parameters(resolved, dict(parameters or {}))
        if resolved.artifacts:
            if not artifact_bucket:
                raise InvalidArgumentException(
                    f"{resolved.name} necesita artifact_bucket=: sube el código Lambda del "
                    "componente"
                )
            data = load_artifact(resolved)
            key = artifact_key(data)
            self._provisioner.put_artifact(artifact_bucket, key, data)
            for artifact in resolved.artifacts:
                resolved_parameters[artifact.parameter_key] = key
        template_body = load_template(resolved)
        full_tags = stack_tags(resolved, dict(tags or {}))
        plan = plan_deploy(self._provisioner.describe(name))
        if plan.action == "blocked":
            raise StackException(plan.reason or f"deploy de {name!r} bloqueado", code="blocked")
        if plan.action == "create":
            self._provisioner.create(
                resolved,
                stack_name=name,
                template_body=template_body,
                parameters=resolved_parameters,
                tags=full_tags,
            )
        else:
            self._provisioner.update(
                resolved,
                stack_name=name,
                template_body=template_body,
                parameters=resolved_parameters,
                tags=full_tags,
            )
        if wait:
            self._provisioner.wait(name, "deployed", wait_timeout)
        status = self._provisioner.describe(name)
        if status is None:
            raise StackException(
                f"la pila {name!r} desapareció justo tras desplegarla", code="failed"
            )
        return status

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
