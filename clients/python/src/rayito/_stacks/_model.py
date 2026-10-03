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

from dataclasses import dataclass, field
from typing import Final, Literal

from rayito._version import __version__

#: Las tres etiquetas fijas de toda pila `OptionalStack`; nunca las
#: sobreescriben las del llamante (`stack_tags`).
MANAGED_BY_TAG: Final = "rayito-sdk"
FIXED_TAG_KEYS: Final = ("rayito:component", "rayito:managed-by", "rayito:sdk-version")

#: Estado de una pila en `ROLLBACK_COMPLETE`: CloudFormation no permite
#: `UpdateStack` sobre ella, hay que borrarla primero.
ROLLBACK_COMPLETE: Final = "ROLLBACK_COMPLETE"

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
    de la plantilla al que se pasa la clave S3 resultante (`sha256` del
    contenido, `_packaging.artifact_key`). `bucket_parameter_key`, si la
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

    @property
    def default_stack_name(self) -> str:
        return f"rayito-{self.name}"


@dataclass(frozen=True)
class StackStatus:
    name: str
    state: str | None
    outputs: dict[str, str] = field(default_factory=dict)
    reason_code: str | None = None

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


def stack_tags(component: StackComponent, user_tags: dict[str, str]) -> dict[str, str]:
    """Las tres etiquetas fijas siempre ganan; el resto son las del
    llamante (`tags=` de `deploy()`), tal cual."""
    fixed = {
        "rayito:component": component.name,
        "rayito:managed-by": MANAGED_BY_TAG,
        "rayito:sdk-version": __version__,
    }
    merged = dict(user_tags)
    merged.update(fixed)
    return merged
