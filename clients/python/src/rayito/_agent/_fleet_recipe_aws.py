"""El adaptador de Parameter Store de la receta de la flota (ADR-034 D1-D3,
`AWS_API_NOTES.md` «Receta de la flota y claves de corta duración de
Bedrock»).

Sólo tres operaciones y sólo estos parámetros: `PutParameter` (`Name`,
`Value`, `Type="SecureString"`, `Tier="Standard"`, `Overwrite=True`,
`Description`), `GetParameter` (`Name`, `WithDecryption=True`) y
`DeleteParameter` (`Name`). Nunca `KeyId` (la clave gestionada `aws/ssm`),
`Tags`, `Policies` ni `AllowedPattern`. El cliente `ssm` se crea en la
primera llamada.

Los errores se mapean por código: lo que no debe hacer fallar un despliegue
ni una reconexión sale como `RecipeWarning` con su código de aviso; lo demás,
como `SandboxException` sin el mensaje de AWS (puede nombrar el parámetro).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final, Protocol

from botocore.exceptions import BotoCoreError, ClientError

from rayito._agent._fleet_recipe import (
    RECIPE_ACCESS_DENIED,
    RECIPE_NOT_SAVED,
    RECIPE_TOO_LARGE,
    FleetRecipe,
    RecipeWarning,
    decode_recipe,
    encode_recipe,
    recipe_document,
    recipe_parameter_name,
    recipe_project,
)
from rayito._aws import LazyClient, aws_code
from rayito._aws_sanitize import sanitize_aws_error
from rayito._limits import AGENT_RECIPE_DESCRIPTION
from rayito.exceptions import SandboxException

if TYPE_CHECKING:
    from rayito._agent._project_domain import AgentProject

logger = logging.getLogger("rayito.agent")

#: `Type` y `Tier` del parámetro: cifrado con `aws/ssm`, nivel gratuito.
RECIPE_PARAMETER_TYPE: Final = "SecureString"
RECIPE_PARAMETER_TIER: Final = "Standard"
#: Códigos de AWS (AWS_API_NOTES.md, tabla de errores de la receta).
PARAMETER_NOT_FOUND: Final = "ParameterNotFound"
ACCESS_DENIED: Final = "AccessDeniedException"
#: Lo que impide guardar sin que el despliegue falle: el límite de
#: parámetros, un nombre que la regla previa debió rechazar y la limitación
#: de tasa tras los reintentos `standard` del cliente.
NOT_SAVED_CODES: Final = frozenset(
    {
        "ParameterLimitExceeded",
        "ParameterPatternMismatchException",
        "HierarchyLevelLimitExceededException",
        "TooManyUpdates",
        "ThrottlingException",
    }
)
SSM_IAM_ACTIONS: Final = {
    "put_parameter": "ssm:PutParameter",
    "get_parameter": "ssm:GetParameter",
    "delete_parameter": "ssm:DeleteParameter",
}


class FleetRecipeStore(Protocol):
    """Dónde vive la receta de cada flota (el puerto de la flota y del
    despliegue). `parameter` es la ruta de `recipe_parameter_name`."""

    def save(self, parameter: str, value: str) -> None: ...

    def load(self, parameter: str) -> str | None: ...

    def delete(self, parameter: str) -> None: ...


class SsmRecipeStore:
    """`FleetRecipeStore` sobre Parameter Store.

    Coste y activación
    -------------------
    Activa: `deploy_agent()` con `save_recipe=True` (por defecto) guarda;
        `AgentFleet.connect(nombre)` sin `project` lee; `stop()` borra la que
        leyó o guardó. Construirlo no llama a AWS.
    Recursos y llamadas AWS: un parámetro `SecureString` estándar por flota
        (`PutParameter`, `GetParameter` con `WithDecryption`,
        `DeleteParameter`) y una petición de KMS (`aws/ssm`) por escritura y
        por lectura.
    Coste aproximado: los parámetros estándar y sus llamadas no tienen cargo
        (https://aws.amazon.com/systems-manager/pricing/); KMS, 20 000
        peticiones gratis al mes y después $0,03 por 10 000
        (https://aws.amazon.com/kms/pricing/), consultado 2026-10-22.
    IAM: `ssm:PutParameter`, `ssm:GetParameter` y `ssm:DeleteParameter`
        sobre `parameter/rayito/agents/*` (sentencia `AgentRecipe` de
        `infra/iam.yaml`); ningún permiso de KMS con `aws/ssm`.
    Cómo apagarla: `AgentDeployOptions(save_recipe=False)`
        (`--no-save-recipe`) no guarda; `AgentFleet.connect(...,
        use_recipe=False)` o `project=` no lee.
    Ejemplo:
        store = SsmRecipeStore(region="us-east-1")
        store.load("/rayito/agents/my-agent/recipe")
    """

    def __init__(self, *, region: str | None = None, session: Any = None) -> None:
        self._client = LazyClient("ssm", region=region, session=session)

    def __repr__(self) -> str:
        return "SsmRecipeStore()"

    def save(self, parameter: str, value: str) -> None:
        """`PutParameter` con sobrescritura. Sin permiso:
        `recipe_access_denied`; límite, nombre o tasa: `recipe_not_saved`."""
        self._call(
            "put_parameter",
            Name=parameter,
            Value=value,
            Type=RECIPE_PARAMETER_TYPE,
            Tier=RECIPE_PARAMETER_TIER,
            Overwrite=True,
            Description=AGENT_RECIPE_DESCRIPTION,
        )

    def load(self, parameter: str) -> str | None:
        """El valor descifrado, o `None` si la flota no tiene receta."""
        try:
            response = self._call("get_parameter", Name=parameter, WithDecryption=True)
        except _NotFound:
            return None
        value = response.get("Parameter", {}).get("Value")
        return value if isinstance(value, str) else None

    def delete(self, parameter: str) -> None:
        """`DeleteParameter`; borrar una receta que no existe no es un error."""
        try:
            self._call("delete_parameter", Name=parameter)
        except _NotFound:
            return

    def _call(self, operation: str, **params: Any) -> dict[str, Any]:
        try:
            response = getattr(self._client.get(), operation)(**params)
        except (ClientError, BotoCoreError) as exc:
            raise translate_recipe_error(operation, exc) from sanitize_aws_error(
                exc, include_message=False
            )
        return dict(response)


class _NotFound(Exception):
    """`ParameterNotFound`: la flota no tiene receta."""


def translate_recipe_error(operation: str, exc: BaseException) -> Exception:
    """El error de AWS mapeado por código (nunca por el mensaje): sin
    receta, aviso (sin permiso o sin poder guardar) o `SandboxException`."""
    code = aws_code(exc)
    if code == PARAMETER_NOT_FOUND:
        return _NotFound()
    if code == ACCESS_DENIED:
        return RecipeWarning(RECIPE_ACCESS_DENIED)
    if operation == "put_parameter" and code in NOT_SAVED_CODES:
        return RecipeWarning(RECIPE_NOT_SAVED)
    label = code or type(exc).__name__
    return SandboxException(
        f"Parameter Store falló en {SSM_IAM_ACTIONS[operation]} ({label})", aws_code=code
    )


def default_recipe_store(region: str | None, session: Any) -> FleetRecipeStore:
    """La tienda de recetas de producción para la región y la sesión de la
    flota (sin llamar a AWS)."""
    return SsmRecipeStore(region=region, session=session)


#: El detalle del paso `recipe` de un despliegue cuando la receta se guardó.
RECIPE_SAVED: Final = "saved"


def warn_recipe(code: str) -> str:
    """Escribe el aviso de la receta en el log (sólo el código, nunca la ruta
    ni el valor) y lo devuelve."""
    logger.warning("receta de la flota: %s", code)
    return code


def store_recipe(store: FleetRecipeStore, recipe: FleetRecipe) -> str:
    """Guarda la receta y devuelve `saved` o el código del aviso: nunca
    falla. Una receta que no cabe borra la anterior (`recipe_too_large`)."""
    parameter = recipe_parameter_name(recipe.name)
    if parameter is None:
        return warn_recipe(RECIPE_NOT_SAVED)
    value = encode_recipe(recipe_document(recipe))
    try:
        if value is None:
            store.delete(parameter)
            return warn_recipe(RECIPE_TOO_LARGE)
        store.save(parameter, value)
    except RecipeWarning as warning:
        return warn_recipe(RECIPE_TOO_LARGE if value is None else warning.code)
    except SandboxException:
        return warn_recipe(RECIPE_TOO_LARGE if value is None else RECIPE_NOT_SAVED)
    return RECIPE_SAVED


def load_recipe(store: FleetRecipeStore, name: str) -> tuple[FleetRecipe, AgentProject] | None:
    """La receta de la flota `name` y su proyecto, o `None` si no tiene, no
    hay permiso (`recipe_access_denied`) o no se puede leer
    (`recipe_unreadable`). Cualquier otro error de AWS se propaga."""
    parameter = recipe_parameter_name(name)
    if parameter is None:
        return None
    try:
        value = store.load(parameter)
        if value is None:
            return None
        recipe = decode_recipe(value)
        return recipe, recipe_project(recipe)
    except RecipeWarning as warning:
        warn_recipe(warning.code)
        return None


def forget_recipe(store: FleetRecipeStore, name: str) -> None:
    """Borra la receta de la flota `name`. Sin permiso, sólo un aviso."""
    parameter = recipe_parameter_name(name)
    if parameter is None:
        return
    try:
        store.delete(parameter)
    except RecipeWarning as warning:
        warn_recipe(warning.code)


__all__ = [
    "RECIPE_SAVED",
    "FleetRecipeStore",
    "SsmRecipeStore",
    "default_recipe_store",
    "forget_recipe",
    "load_recipe",
    "store_recipe",
    "translate_recipe_error",
    "warn_recipe",
]
