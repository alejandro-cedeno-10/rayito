"""Dominio puro del convenio `OptionalStack` (M15 foundations, ADR-016): qué
es un componente opcional (`StackComponent`), qué devuelve consultarlo
(`StackStatus`), qué hacer dado el estado actual (`plan_deploy`) y cómo se
etiqueta siempre (`stack_tags`). Nada aquí importa `boto3`: las llamadas a
CloudFormation viven en `_cloudformation.py`, detrás del puerto
`StackProvisioner` (`_port.py`).

Cada componente (`metadata-index`, `secrets-access`, y los stubs de las
ocho funciones 0.6) es una `StackComponent` en `_registry.py`; ninguno se
despliega automáticamente — `OptionalStacks.deploy()` es siempre una
llamada explícita del SDK o de `rayito stack deploy`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from rayito._version import __version__
from rayito.exceptions import InvalidArgumentException

#: Las tres etiquetas fijas de toda pila `OptionalStack`; nunca las
#: sobreescriben las del llamante (`stack_tags`).
MANAGED_BY_TAG: Final = "rayito-sdk"
#: La etiqueta fija con la versión del SDK que desplegó la pila por última vez.
SDK_VERSION_TAG_KEY: Final = "rayito:sdk-version"
FIXED_TAG_KEYS: Final = ("rayito:component", "rayito:managed-by", SDK_VERSION_TAG_KEY)

#: Estado de una pila en `ROLLBACK_COMPLETE`: CloudFormation no permite
#: `UpdateStack` sobre ella, hay que borrarla primero.
ROLLBACK_COMPLETE: Final = "ROLLBACK_COMPLETE"

#: Los estados terminales en los que un `deploy` fallido tiene un recurso
#: causante que explicar (`failure_reason`). Otros fallos terminales
#: (`UPDATE_ROLLBACK_FAILED`, `DELETE_FAILED`) no se explican: el motivo
#: podría venir de una operación anterior. Fuente: los valores de
#: `StackStatus` de
#: https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_Stack.html
#: (consultada 2026-10-27).
EXPLAINABLE_ROLLBACK_STATES: Final = frozenset(
    {"ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "UPDATE_ROLLBACK_COMPLETE"}
)
#: Estados de un recurso que marcan su fallo. Fuente: el `enum`
#: `ResourceStatus` de
#: https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_StackEvent.html
#: (consultada 2026-10-27).
FAILED_RESOURCE_STATUSES: Final = frozenset({"CREATE_FAILED", "UPDATE_FAILED"})
#: Estados del evento de la propia pila con el que empieza una operación
#: (misma fuente que `FAILED_RESOURCE_STATUSES`).
OPERATION_START_STATUSES: Final = frozenset({"CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS"})

DeployAction = Literal["create", "update", "blocked"]


@dataclass(frozen=True)
class StackParameter:
    name: str
    description: str
    required: bool = False
    default: str | None = None
    no_echo: bool = False


@dataclass(frozen=True)
class StackArtifact:
    """Un fichero que `deploy()` sube a `artifact_bucket` antes de
    desplegar la plantilla (código Lambda); `parameter_key` es el parámetro
    de la plantilla al que se pasa la clave S3 resultante
    (`rayito/stacks/<componente>/<sha256>.zip`, `_packaging.artifact_key`).
    `bucket_parameter_key`, si la
    plantilla también necesita el bucket (p. ej. `Code.S3Bucket` de una
    Lambda), es el parámetro al que `deploy()` pasa el mismo
    `artifact_bucket`: así ni la CLI genérica ni la fachada de la función
    tienen que repetirlo como `--param`."""

    name: str
    parameter_key: str
    bucket_parameter_key: str | None = None


@dataclass(frozen=True)
class CostStatement:
    """El bloque "Coste y activación" de cada componente, de forma
    estructurada: `creates` son los recursos que la pila crea, `idle_monthly`
    el coste en reposo, `per_use` el coste por uso (vacío si no lo hay),
    `removal` qué se conserva o se borra con `destroy` y `source` la
    referencia con fecha de la página de precios consultada."""

    creates: tuple[str, ...]
    idle_monthly: str
    per_use: tuple[str, ...] = ()
    removal: str = "destroy() borra todo lo que esta pila creó"
    source: str = ""


@dataclass(frozen=True)
class StackComponent:
    name: str
    description: str
    cost: CostStatement
    parameters: tuple[StackParameter, ...] = ()
    artifacts: tuple[StackArtifact, ...] = ()
    #: Sólo `CAPABILITY_IAM`/`CAPABILITY_NAMED_IAM`; `CAPABILITY_AUTO_EXPAND`
    #: no hace falta (ningún componente usa macros ni SAM transforms).
    capabilities: tuple[str, ...] = ()
    #: `False` para un componente que `list()` ya describe (nombre, coste,
    #: parámetros previstos) pero cuya función todavía no tiene plantilla:
    #: `deploy`/`status`/`destroy` lanzan `UnimplementedError` antes de
    #: tocar AWS. Cada feature pasa esto a `True` en su propio cambio al
    #: añadir `infra/<component>.yaml` y su entrada en `_packaging.py`.
    supported: bool = True
    #: Validación de coherencia entre parámetros que la plantilla no puede
    #: expresar (CloudFormation no compara números). Recibe los valores
    #: efectivos del despliegue y lanza `InvalidArgumentException`; corre
    #: antes de llamar a AWS.
    validate: Callable[[Mapping[str, str]], None] | None = None

    @property
    def default_stack_name(self) -> str:
        return f"rayito-{self.name}"


@dataclass(frozen=True)
class StackStatus:
    name: str
    state: str | None
    outputs: dict[str, str] = field(default_factory=dict)
    reason_code: str | None = None
    #: `Parameters[].{ParameterKey: ParameterValue}` de `DescribeStacks`: los
    #: valores con los que la pila está desplegada ahora (CloudFormation ya
    #: enmascara los `NoEcho`). `plan_parameters` lo usa para no pisarlos.
    parameters: dict[str, str] = field(default_factory=dict)
    #: `Tags[].{Key: Value}` de `DescribeStacks`: las etiquetas de la pila,
    #: las fijas de `stack_tags` incluidas (`rayito:sdk-version` es la versión
    #: del SDK que la desplegó por última vez).
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def exists(self) -> bool:
        return self.state is not None


@dataclass(frozen=True)
class DeployPlan:
    """Qué hacer con `deploy()` dado el estado actual. `NoChange` no es un
    resultado de este plan: sólo se sabe tras intentar `UpdateStack`
    (`StackProvisioner.update()` devuelve `"changed"`/`"no_changes"`), así
    que esta función sólo decide entre crear, actualizar o bloquear."""

    action: DeployAction
    reason: str | None = None


def plan_deploy(current: StackStatus | None) -> DeployPlan:
    """`current=None` (la pila no existe) -> `create`; `ROLLBACK_COMPLETE`
    -> `blocked` (CloudFormation rechaza `UpdateStack` sobre ella: hay que
    `destroy()` primero); cualquier otro estado existente -> `update`."""
    if current is None or not current.exists:
        return DeployPlan("create")
    if current.state == ROLLBACK_COMPLETE:
        return DeployPlan(
            "blocked",
            f"la pila {current.name!r} está en ROLLBACK_COMPLETE: bórrala (destroy) antes de "
            "volver a desplegarla",
        )
    return DeployPlan("update")


@dataclass(frozen=True)
class ParameterChange:
    """Un parámetro cuyo valor cambiaría con el `deploy()`; `before` es
    `None` si la pila no existe o todavía no lo tenía."""

    name: str
    before: str | None
    after: str


@dataclass(frozen=True)
class ParameterPlan:
    """Qué parámetros manda `deploy()`: `values` con `ParameterValue` y
    `keep_previous` con `UsePreviousValue=True` (sólo en `UpdateStack`: los
    que el llamante no pasó y la pila ya tiene)."""

    values: dict[str, str]
    keep_previous: tuple[str, ...] = ()

    def changes(self, current: StackStatus | None) -> tuple[ParameterChange, ...]:
        """Los de `values` que difieren de los actuales; los de
        `keep_previous` nunca cambian."""
        before = current.parameters if current is not None else {}
        return tuple(
            ParameterChange(name, before.get(name), value)
            for name, value in sorted(self.values.items())
            if before.get(name) != value
        )


def reject_unknown_parameters(component: StackComponent, given: dict[str, str]) -> None:
    known = {parameter.name for parameter in component.parameters}
    unknown = sorted(set(given) - known)
    if unknown:
        raise InvalidArgumentException(f"parámetros desconocidos para {component.name}: {unknown}")


def plan_parameters(
    component: StackComponent, given: dict[str, str], current: StackStatus | None
) -> ParameterPlan:
    """Los valores por defecto del catálogo sólo se aplican al crear. Al
    actualizar una pila existente, un parámetro que el llamante no pasó y la
    pila ya tiene se conserva (`UsePreviousValue`) en vez de volver a su
    valor por defecto: redesplegar sin repetir cada `--param` no puede
    ensanchar una política (`s3-mounts` `Prefixes`), reemplazar una tabla
    (`metadata-index` `TableName`) ni quitar un permiso (`secrets-access`
    `KmsKeyArn`). Un parámetro nuevo de la plantilla que la pila aún no
    tiene recibe su valor por defecto también al actualizar."""
    reject_unknown_parameters(component, given)
    previous = current.parameters if current is not None and current.exists else None
    values = dict(given)
    keep_previous: list[str] = []
    missing: list[str] = []
    for parameter in component.parameters:
        if parameter.name in given:
            continue
        if previous is not None and parameter.name in previous:
            keep_previous.append(parameter.name)
        elif parameter.default is not None:
            values[parameter.name] = parameter.default
        elif parameter.required:
            missing.append(parameter.name)
    if missing:
        raise InvalidArgumentException(
            f"faltan parámetros obligatorios para {component.name}: {sorted(missing)}"
        )
    if component.validate is not None:
        retained = {name: previous[name] for name in keep_previous if previous is not None}
        component.validate({**retained, **values})
    return ParameterPlan(values=values, keep_previous=tuple(sorted(keep_previous)))


def stack_tags(component: StackComponent, user_tags: dict[str, str]) -> dict[str, str]:
    """Las tres etiquetas fijas siempre ganan; el resto son las del
    llamante (`tags=` de `deploy()`), tal cual."""
    fixed = {
        "rayito:component": component.name,
        "rayito:managed-by": MANAGED_BY_TAG,
        SDK_VERSION_TAG_KEY: __version__,
    }
    merged = dict(user_tags)
    merged.update(fixed)
    return merged


@dataclass(frozen=True)
class StackEvent:
    """Un evento de `DescribeStackEvents` (`AWS_API_NOTES.md` §21): sólo los
    tres campos que usa `first_failed_resource`."""

    logical_resource_id: str | None
    resource_status: str | None
    resource_status_reason: str | None


def first_failed_resource(events: Sequence[StackEvent], stack_name: str) -> StackEvent | None:
    """El recurso que causó el fallo de la última operación de la pila.

    `events` viene del más nuevo al más antiguo, el orden de la API. Se
    recorre hasta el evento de inicio de operación de la propia pila
    (`logical_resource_id == stack_name`), para no mezclar el fallo de un
    despliegue anterior. De los recursos con estado fallido se devuelve el
    más antiguo: los posteriores suelen ser cancelaciones por esa causa."""
    earliest: StackEvent | None = None
    for event in events:
        if event.logical_resource_id == stack_name:
            if event.resource_status in OPERATION_START_STATUSES:
                break
            continue
        if event.resource_status in FAILED_RESOURCE_STATUSES:
            earliest = event
    return earliest


def describe_failed_resource(event: StackEvent) -> str:
    """`<LogicalResourceId> (<ResourceStatus>): <ResourceStatusReason>`."""
    return (
        f"{event.logical_resource_id or ''} ({event.resource_status or ''}): "
        f"{event.resource_status_reason or ''}"
    )
