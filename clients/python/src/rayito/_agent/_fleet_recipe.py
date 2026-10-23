"""La receta de una flota de agentes (`rayito.agent-recipe/1`, ADR-034 D1 y
D2): lo que `deploy_agent()` usó para arrancarla (el proyecto tras las
banderas, la versión de la imagen, los presets de las pasarelas, el escalado
y la renovación de claves), para que `AgentFleet.connect(nombre)` pueda crear
VMs iguales en cualquier proceso sin el proyecto.

Dominio puro: construir el documento, codificarlo (JSON compacto con claves
ordenadas o, si no cabe en un parámetro estándar, el sobre `gzip+base64`),
decodificarlo y validarlo, y el nombre del parámetro. La E/S con Parameter
Store vive en `_fleet_recipe_aws.py`.

La receta sólo lleva nombres de secretos, nunca valores ni el access token.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import gzip
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from rayito._agent._scaling_domain import ScalingOptions
from rayito._limits import (
    AGENT_RECIPE_MAX_BYTES,
    AGENT_RECIPE_PARAMETER_TEMPLATE,
    AGENT_RECIPE_SCHEMA,
)
from rayito._version import __version__
from rayito.exceptions import InvalidArgumentException

if TYPE_CHECKING:
    from rayito._agent._project_domain import AgentProject

#: El sobre de una receta que no cabe en `agentRecipeMaxBytes` como JSON.
RECIPE_ENCODING_GZIP: Final = "gzip+base64"
#: La regla de nombres de Parameter Store para cada nivel de la ruta
#: (API_PutParameter.html, consultado 2026-10-22).
RECIPE_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9_.-]+$")
#: Los nombres que cumplen el patrón pero son un nivel relativo de la ruta
#: (`/rayito/agents/../recipe`): nunca nombran una receta.
RECIPE_RESERVED_NAMES: Final = frozenset({".", ".."})
#: Nivel de compresión y `mtime` del gzip del sobre: fijos, para que la misma
#: receta dé siempre los mismos bytes.
RECIPE_GZIP_LEVEL: Final = 9
RECIPE_GZIP_MTIME: Final = 0
#: `savedBy` de las recetas que guarda este SDK.
RECIPE_SAVED_BY: Final = f"python/{__version__}"

#: Los avisos de la receta (lista cerrada, los mismos en los dos SDK). Nunca
#: llevan la ruta del parámetro ni su valor.
RECIPE_ACCESS_DENIED: Final = "recipe_access_denied"
RECIPE_NOT_SAVED: Final = "recipe_not_saved"
RECIPE_TOO_LARGE: Final = "recipe_too_large"
RECIPE_UNREADABLE: Final = "recipe_unreadable"
#: La receta pide renovar las claves de Bedrock pero quien conecta no lo
#: pidió (`renew_gateway_keys=True`): la flota no renueva ninguna.
RECIPE_RENEWAL_IGNORED: Final = "recipe_renewal_ignored"
RECIPE_WARNING_CODES: Final[tuple[str, ...]] = (
    RECIPE_ACCESS_DENIED,
    RECIPE_NOT_SAVED,
    RECIPE_RENEWAL_IGNORED,
    RECIPE_TOO_LARGE,
    RECIPE_UNREADABLE,
)

_SCHEMA_KEY: Final = "schema"
_ENCODING_KEY: Final = "encoding"
_DATA_KEY: Final = "data"
_ENVELOPE_KEYS: Final = frozenset({_SCHEMA_KEY, _ENCODING_KEY, _DATA_KEY})
_SCALING_KEYS: Final[tuple[tuple[str, str], ...]] = (
    ("minVms", "min_vms"),
    ("maxVms", "max_vms"),
    ("warmVms", "warm_vms"),
    ("sessions", "sessions"),
    ("scaleInAfterSeconds", "scale_in_after_seconds"),
)
#: El directorio base del proyecto rehecho desde la receta: sus rutas
#: relativas no se usan para servir (la imagen ya está construida).
RECIPE_PROJECT_BASE_DIR: Final = "."


class RecipeWarning(Exception):
    """Una receta que no se guardó, no se pudo leer o no se pudo borrar: la
    flota sigue como en 0.16 y quien la captura emite `code` como aviso."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FleetRecipe:
    """Una receta ya validada. `project` es el documento de `AgentProject`
    (`to_dict()`) tras las banderas del despliegue; `image_version` la versión
    de la imagen (`None`: la más reciente activa)."""

    name: str
    saved_by: str
    image_name: str
    image_version: str | None
    project: Mapping[str, object]
    gateway_presets: Mapping[str, str] = field(default_factory=dict)
    scaling: ScalingOptions = field(default_factory=ScalingOptions)
    renew_gateway_keys: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "project", MappingProxyType(dict(self.project)))
        object.__setattr__(self, "gateway_presets", MappingProxyType(dict(self.gateway_presets)))


def recipe_parameter_name(fleet_name: str) -> str | None:
    """El parámetro de la receta de `fleet_name`
    (`agentRecipeParameterTemplate`), o `None` si el nombre no cumple la
    regla de Parameter Store: esa flota no tiene receta (`recipe_not_saved`)."""
    if fleet_name in RECIPE_RESERVED_NAMES or not RECIPE_NAME_PATTERN.fullmatch(fleet_name):
        return None
    return AGENT_RECIPE_PARAMETER_TEMPLATE.format(name=fleet_name)


def renewal_on_connect(
    recipe: FleetRecipe | None, requested: bool | None
) -> tuple[bool, str | None]:
    """Si una flota reconectada renueva las claves de Bedrock y el aviso que
    toca (`testdata/fleet-recipe/renewal.json`, `connect`).

    Sólo renueva quien lo pide (`requested=True`): la receta nunca la activa,
    porque la clave se firma con las credenciales de quien conecta. Si la
    receta la pedía y el llamante no dijo nada (`None`), el aviso es
    `recipe_renewal_ignored`; un `False` explícito no avisa."""
    if requested is None and recipe is not None and recipe.renew_gateway_keys:
        return False, RECIPE_RENEWAL_IGNORED
    return requested is True, None


def recipe_of(
    project: AgentProject,
    *,
    name: str,
    image_version: str | None,
    scaling: ScalingOptions,
    renew_gateway_keys: bool,
    saved_by: str = RECIPE_SAVED_BY,
) -> FleetRecipe:
    """La receta de una flota recién arrancada con `project`."""
    return FleetRecipe(
        name=name,
        saved_by=saved_by,
        image_name=project.template or project.name,
        image_version=image_version,
        project=project.to_dict(),
        gateway_presets=dict(project.gateway_presets),
        scaling=scaling,
        renew_gateway_keys=renew_gateway_keys,
    )


def recipe_document(recipe: FleetRecipe) -> dict[str, object]:
    """El documento `rayito.agent-recipe/1`, en camelCase en los dos SDK y
    con el escalado en segundos."""
    image: dict[str, object] = {"name": recipe.image_name}
    if recipe.image_version is not None:
        image["version"] = recipe.image_version
    return {
        _SCHEMA_KEY: AGENT_RECIPE_SCHEMA,
        "name": recipe.name,
        "savedBy": recipe.saved_by,
        "image": image,
        "project": dict(recipe.project),
        "gatewayPresets": dict(recipe.gateway_presets),
        "scaling": {key: getattr(recipe.scaling, attr) for key, attr in _SCALING_KEYS},
        "renewGatewayKeys": recipe.renew_gateway_keys,
    }


def canonical_json(document: Mapping[str, object]) -> str:
    """JSON compacto con las claves ordenadas y sin escapar lo que no es
    ASCII, como `JSON.stringify` con las claves ordenadas: los dos SDK
    escriben los mismos bytes. Un `float` entero se escribe como entero."""
    return json.dumps(
        _canonical(document), separators=(",", ":"), sort_keys=True, ensure_ascii=False
    )


def encode_recipe(document: Mapping[str, object]) -> str | None:
    """El valor del parámetro: el JSON si cabe en `agentRecipeMaxBytes`; si
    no, el sobre `gzip+base64`; `None` si tampoco cabe (`recipe_too_large`)."""
    plain = canonical_json(document)
    if _fits(plain):
        return plain
    compressed = gzip.compress(
        plain.encode("utf-8"), compresslevel=RECIPE_GZIP_LEVEL, mtime=RECIPE_GZIP_MTIME
    )
    envelope = canonical_json(
        {
            _SCHEMA_KEY: AGENT_RECIPE_SCHEMA,
            _ENCODING_KEY: RECIPE_ENCODING_GZIP,
            _DATA_KEY: base64.b64encode(compressed).decode("ascii"),
        }
    )
    return envelope if _fits(envelope) else None


def decode_recipe(value: str) -> FleetRecipe:
    """La receta de un valor de Parameter Store, en cualquiera de sus dos
    formas. Otro esquema, otra codificación o un documento mal formado:
    `RecipeWarning(recipe_unreadable)`."""
    document = _json_object(value)
    if _ENCODING_KEY in document:
        document = _unwrap(document)
    return recipe_from_document(document)


def recipe_from_document(document: Mapping[str, object]) -> FleetRecipe:
    """La receta de un documento ya decodificado, validada."""
    if document.get(_SCHEMA_KEY) != AGENT_RECIPE_SCHEMA:
        raise RecipeWarning(RECIPE_UNREADABLE)
    image = _mapping(document.get("image"))
    project = _mapping(document.get("project"))
    presets = _mapping(document.get("gatewayPresets"))
    scaling = _mapping(document.get("scaling"))
    renew = document.get("renewGatewayKeys")
    name = document.get("name")
    saved_by = document.get("savedBy")
    image_name = image.get("name")
    version = image.get("version")
    if (
        not isinstance(name, str)
        or not isinstance(saved_by, str)
        or not isinstance(image_name, str)
        or not (version is None or isinstance(version, str))
        or not isinstance(renew, bool)
        or not all(isinstance(value, str) for value in presets.values())
    ):
        raise RecipeWarning(RECIPE_UNREADABLE)
    return FleetRecipe(
        name=name,
        saved_by=saved_by,
        image_name=image_name,
        image_version=version,
        project=project,
        gateway_presets={str(key): str(value) for key, value in presets.items()},
        scaling=_scaling_of(scaling),
        renew_gateway_keys=renew,
    )


def recipe_project(recipe: FleetRecipe) -> AgentProject:
    """El `AgentProject` de la receta, leído como un fichero de proyecto y
    con sus presets de pasarela. Un documento que el lector rechaza es
    `RecipeWarning(recipe_unreadable)`."""
    from rayito._agent._project_domain import AgentProject

    try:
        project = AgentProject.from_dict(recipe.project, base_dir=RECIPE_PROJECT_BASE_DIR)
        return _with_presets(project, recipe.gateway_presets)
    except InvalidArgumentException:
        raise RecipeWarning(RECIPE_UNREADABLE) from None


def _with_presets(project: AgentProject, presets: Mapping[str, str]) -> AgentProject:
    return dataclasses.replace(project, gateway_presets=dict(presets))


def _scaling_of(scaling: Mapping[str, object]) -> ScalingOptions:
    try:
        return ScalingOptions(**{attr: scaling[key] for key, attr in _SCALING_KEYS})  # type: ignore[arg-type]
    except (KeyError, TypeError, InvalidArgumentException):
        raise RecipeWarning(RECIPE_UNREADABLE) from None


def _fits(value: str) -> bool:
    return len(value.encode("utf-8")) <= AGENT_RECIPE_MAX_BYTES


def _json_object(value: str) -> Mapping[str, object]:
    try:
        document = json.loads(value)
    except ValueError:
        raise RecipeWarning(RECIPE_UNREADABLE) from None
    return _mapping(document)


def _unwrap(envelope: Mapping[str, object]) -> Mapping[str, object]:
    data = envelope.get(_DATA_KEY)
    if (
        set(envelope) != _ENVELOPE_KEYS
        or envelope.get(_SCHEMA_KEY) != AGENT_RECIPE_SCHEMA
        or envelope.get(_ENCODING_KEY) != RECIPE_ENCODING_GZIP
        or not isinstance(data, str)
    ):
        raise RecipeWarning(RECIPE_UNREADABLE)
    try:
        plain = gzip.decompress(base64.b64decode(data, validate=True)).decode("utf-8")
    except (binascii.Error, OSError, EOFError, UnicodeDecodeError):
        raise RecipeWarning(RECIPE_UNREADABLE) from None
    return _json_object(plain)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RecipeWarning(RECIPE_UNREADABLE)
    return value


def _canonical(value: object) -> object:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    return value


__all__ = [
    "RECIPE_ACCESS_DENIED",
    "RECIPE_ENCODING_GZIP",
    "RECIPE_NOT_SAVED",
    "RECIPE_RENEWAL_IGNORED",
    "RECIPE_SAVED_BY",
    "RECIPE_TOO_LARGE",
    "RECIPE_UNREADABLE",
    "RECIPE_WARNING_CODES",
    "FleetRecipe",
    "RecipeWarning",
    "canonical_json",
    "decode_recipe",
    "encode_recipe",
    "recipe_document",
    "recipe_from_document",
    "recipe_of",
    "recipe_parameter_name",
    "recipe_project",
    "renewal_on_connect",
]
