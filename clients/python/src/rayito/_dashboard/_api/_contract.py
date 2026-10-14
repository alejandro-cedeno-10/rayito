"""El contrato de `/api/v1`: versión, rutas, códigos de error cerrados y los
tipos de petición y respuesta del núcleo. El esquema JSON de las respuestas
vive en `testdata/dashboard/api-v1.schema.json`, y los tests de contrato lo
aplican a cada cáscara."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

API_VERSION: Final = "v1"
API_PREFIX: Final = f"/api/{API_VERSION}"

DEPLOYMENT_LOCAL: Final = "local"
"""`meta.deployment` del panel local (`rayito dashboard`): las opciones se dan por la CLI."""
DEPLOYMENT_STACK: Final = "stack"
"""`meta.deployment` del panel en tu cuenta: las opciones salen de la pila y de Cognito."""

HTTP_OK: Final = 200
HTTP_ACCEPTED: Final = 202
HTTP_BAD_REQUEST: Final = 400
HTTP_UNAUTHORIZED: Final = 401
HTTP_FORBIDDEN: Final = 403
HTTP_NOT_FOUND: Final = 404
HTTP_METHOD_NOT_ALLOWED: Final = 405
HTTP_PAYLOAD_TOO_LARGE: Final = 413
HTTP_UNSUPPORTED_MEDIA_TYPE: Final = 415
HTTP_MISDIRECTED: Final = 421
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_INTERNAL_ERROR: Final = 500
HTTP_BAD_GATEWAY: Final = 502
HTTP_SERVICE_UNAVAILABLE: Final = 503

#: Los `error.code` posibles: una lista cerrada que el esquema enumera.
ERROR_CODES: Final[dict[str, int]] = {
    "invalid_argument": HTTP_BAD_REQUEST,
    "invalid_json": HTTP_BAD_REQUEST,
    "confirmation_required": HTTP_BAD_REQUEST,
    "unauthorized": HTTP_UNAUTHORIZED,
    "forbidden": HTTP_FORBIDDEN,
    "csrf_failed": HTTP_FORBIDDEN,
    "actions_disabled": HTTP_FORBIDDEN,
    "cost_explorer_disabled": HTTP_FORBIDDEN,
    "aws_access_denied": HTTP_FORBIDDEN,
    "not_found": HTTP_NOT_FOUND,
    "method_not_allowed": HTTP_METHOD_NOT_ALLOWED,
    "payload_too_large": HTTP_PAYLOAD_TOO_LARGE,
    "unsupported_media_type": HTTP_UNSUPPORTED_MEDIA_TYPE,
    "misdirected_request": HTTP_MISDIRECTED,
    "aws_throttled": HTTP_TOO_MANY_REQUESTS,
    "jobs_busy": HTTP_TOO_MANY_REQUESTS,
    "internal_error": HTTP_INTERNAL_ERROR,
    "aws_error": HTTP_BAD_GATEWAY,
    "auth_unavailable": HTTP_SERVICE_UNAVAILABLE,
    "audit_unavailable": HTTP_SERVICE_UNAVAILABLE,
}

ERROR_MESSAGES: Final[dict[str, str]] = {
    "invalid_argument": "AWS rechazó un argumento de la petición",
    "invalid_json": "el cuerpo no es un objeto JSON válido",
    "confirmation_required": 'la acción exige "confirm": true',
    "unauthorized": "falta la sesión: abre la URL que imprimió `rayito dashboard`",
    "forbidden": "origen no admitido",
    "csrf_failed": "falta o no coincide el token CSRF",
    "actions_disabled": "las acciones están desactivadas: arranca con --allow-actions",
    "cost_explorer_disabled": (
        "el coste facturado está desactivado: arranca con --cost-explorer "
        "(cada consulta a Cost Explorer se factura)"
    ),
    "aws_access_denied": "AWS rechazó las credenciales o falta un permiso IAM",
    "not_found": "no existe",
    "method_not_allowed": "método no admitido",
    "payload_too_large": "el cuerpo supera el tope",
    "unsupported_media_type": "el cuerpo tiene que ser application/json",
    "misdirected_request": "Host no admitido",
    "aws_throttled": "AWS limitó la tasa: reintenta en unos segundos",
    "internal_error": "error interno del panel",
    "aws_error": "AWS devolvió un error",
    "jobs_busy": "ya hay demasiadas acciones en cola: espera a que terminen",
    "auth_unavailable": "no se pudo comprobar la identidad: reintenta en unos segundos",
    "audit_unavailable": "no se pudo escribir el registro de auditoría: la acción no se ejecutó",
}

ACTION_NAMES: Final = ("kill", "suspend", "resume")


@dataclass(frozen=True)
class ApiRequest:
    """Una petición ya desligada del transporte: `path` sin la cadena de
    consulta y `query` ya partida."""

    method: str
    path: str
    query: Mapping[str, list[str]] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: dict[str, Any]


class ApiBackend(Protocol):
    """Lo que una cáscara (el servidor local) necesita de la API: atender una
    petición, saber si exige CSRF y cerrarse. `DashboardApi` y `AccountsApi`
    lo cumplen."""

    def handle(self, request: ApiRequest) -> ApiResponse: ...

    def requires_csrf(self, request: ApiRequest) -> bool: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class LogLine:
    timestamp: str
    message: str
    stream: str


@dataclass(frozen=True)
class LogPage:
    """`fallback` avisa de que el stream esperado no existía y las líneas
    vienen de streams encontrados por nombre, que otro sandbox con el mismo
    grupo de logs pudo haber fabricado."""

    lines: tuple[LogLine, ...]
    truncated: bool
    fallback: bool = False


class LogsSource(Protocol):
    """Los últimos `limit` eventos del stream de CloudWatch de un sandbox;
    `truncated` avisa de que hay más. Un sandbox sin logs (sin
    `executionRoleArn`) devuelve una página vacía."""

    def read(self, sandbox_id: str, *, limit: int) -> LogPage: ...


def iso_utc(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")
