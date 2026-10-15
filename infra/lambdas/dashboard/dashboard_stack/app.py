"""La cáscara de entrada de la Lambda: de un evento de la Function URL a una
respuesta, sobre los mismos `DashboardApi` y SPA que `rayito dashboard`.

Orden de cada petición (cada paso corta antes del siguiente y ninguno de los
primeros toca AWS):

1. La SPA (`GET` a cualquier ruta fuera de `/api/v1`) es la única ruta sin
   autenticar, junto con `/config.json` (valores públicos del Hosted UI).
2. `/api/v1`: método admitido; en un `POST`, `Origin` igual al propio origen,
   `Content-Type: application/json` y cuerpo acotado.
3. El token de acceso de Cognito, en `Authorization: Bearer`, validado entero
   (`TokenVerifier`) antes de enrutar nada. No hay cookie de sesión, así que
   no hace falta token CSRF.
4. Una acción (`POST /sandboxes/<id>/<acción>`, `POST /actions`) exige que la
   pila tenga rol de acciones, que el llamante esté en el grupo de acciones
   según su token y que Cognito confirme en vivo que sigue habilitado y en
   ese grupo (`directory.py`), todo *antes* de asumir ese rol; escribe la auditoría antes de ejecutar y el
   resultado después.
5. Todo lo demás lo resuelve `DashboardApi` con el rol de sólo lectura.

Con `AuthMode=aws-iam` (`SURFACE_IAM_API`) esta misma clase corre en la
función de la API, que sólo sirve `/api/v1` (la página la sirve otra función,
`shell.py`): la Function URL ya exigió la firma SigV4 y la función lo
comprueba otra vez (`requestContext.authorizer.iam`), el token de Cognito viaja
en `X-Rayito-Token` (la cabecera `Authorization` la ocupa la firma) y el
`Origin` de un `POST` debe ser el de la página, no el propio.

Toda respuesta lleva las cabeceras de seguridad (`_headers.py`). Nunca se
registra el token, la cabecera `Authorization` ni el cuerpo: sólo códigos.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol
from urllib.parse import parse_qs, unquote

from dashboard_stack.appearance import LiveAppearance
from dashboard_stack.audit import AuditLog, AuditUnavailable
from dashboard_stack.config import SURFACE_FULL, SURFACE_IAM_API, StackConfig
from dashboard_stack.tokens import (
    AuthUnavailable,
    Caller,
    ClientIdSource,
    TokenError,
    TokenVerifier,
)
from rayito._dashboard._api._contract import (
    API_PREFIX,
    DEPLOYMENT_STACK,
    HTTP_OK,
    ApiRequest,
    ApiResponse,
)
from rayito._dashboard._api._headers import LOGO_HEADERS, security_headers
from rayito._dashboard._api._router import (
    ApiError,
    DashboardApi,
    action_targets,
    error_response,
    is_action_route,
)
from rayito._dashboard._api._static import LOGO_PATH
from rayito._limits import (
    DASHBOARD_REQUEST_MAX_BODY_BYTES,
    DASHBOARD_STACK_ACTIONS_GROUP,
    DASHBOARD_STACK_LAMBDA_TIMEOUT_MARGIN_SECONDS,
    DASHBOARD_STACK_LAMBDA_TIMEOUT_SECONDS,
)

logger = logging.getLogger("rayito.dashboard")

GET: Final = "GET"
POST: Final = "POST"
ALLOWED_METHODS: Final = (GET, POST)
JSON_TYPE: Final = "application/json"
JSON_CONTENT_TYPE: Final = "application/json; charset=utf-8"
CONFIG_PATH: Final = "/config.json"
META_PATH: Final = f"{API_PREFIX}/meta"
BEARER_SCHEME: Final = "bearer"
IAM_TOKEN_HEADER: Final = "x-rayito-token"
COGNITO_SCOPE: Final = "openid"
#: Dos años, el valor habitual de HSTS para un origen que sólo sirve HTTPS.
HSTS_VALUE: Final = "max-age=63072000; includeSubDomains"
UNKNOWN_SOURCE_IP: Final = "unknown"
UNAUTHORIZED_NO_TOKEN: Final = "falta el token: inicia sesión en el panel"
UNAUTHORIZED_BAD_TOKEN: Final = "token no válido o caducado: vuelve a iniciar sesión"
FORBIDDEN_ORIGIN: Final = "origen no admitido"
FORBIDDEN_GROUP: Final = "la cuenta no pertenece al grupo de acciones"
FORBIDDEN_UNSIGNED: Final = "la petición no llegó firmada por una identidad de IAM"
#: Lo máximo que puede durar un trabajo dentro de una petición: el tiempo de la
#: función menos un margen para la auditoría y la respuesta.
MAX_JOB_SECONDS: Final = (
    DASHBOARD_STACK_LAMBDA_TIMEOUT_SECONDS - DASHBOARD_STACK_LAMBDA_TIMEOUT_MARGIN_SECONDS
)
JOB_TOO_LONG: Final = (
    "la acción tardaría unos {seconds:.0f} s con el ritmo de tu cuota y la función admite "
    f"{MAX_JOB_SECONDS} s: elige menos sandboxes"
)


class ActionsUnavailable(Exception):
    """No se pudo asumir el rol de acciones."""


class CallerDirectory(Protocol):
    def may_act(self, caller: Caller) -> bool: ...


class ActionsGateway(Protocol):
    def api_for(self, caller: Caller) -> DashboardApi: ...


@dataclass(frozen=True)
class Incoming:
    method: str
    path: str
    query: Mapping[str, list[str]]
    headers: Mapping[str, str]
    body: bytes
    domain: str
    request_id: str
    source_ip: str
    #: El ARN de la identidad que firmó la petición (SigV4) cuando la Function
    #: URL es `AWS_IAM`; `None` si la URL no firma o la petición no llegó firmada.
    iam_arn: str | None = None


def incoming_from_event(event: Mapping[str, Any]) -> Incoming:
    """Un evento de Function URL (formato de carga 2.0)."""
    context = event.get("requestContext", {})
    http = context.get("http", {})
    raw_body = event.get("body") or ""
    body = base64.b64decode(raw_body) if event.get("isBase64Encoded") else raw_body.encode()
    return Incoming(
        method=str(http.get("method", "")).upper(),
        path=unquote(str(event.get("rawPath", "/"))),
        query=parse_qs(str(event.get("rawQueryString", ""))),
        headers={str(name).lower(): str(value) for name, value in event.get("headers", {}).items()},
        body=body,
        domain=str(context.get("domainName", "")),
        request_id=str(context.get("requestId", "")),
        source_ip=str(http.get("sourceIp", UNKNOWN_SOURCE_IP)),
        iam_arn=_iam_arn(context),
    )


def _iam_arn(context: Mapping[str, Any]) -> str | None:
    authorizer = context.get("authorizer")
    iam = authorizer.get("iam") if isinstance(authorizer, Mapping) else None
    arn = iam.get("userArn") if isinstance(iam, Mapping) else None
    return arn if isinstance(arn, str) and arn else None


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    content_type: str
    headers: Mapping[str, str] = field(default_factory=dict)


def json_reply(response: ApiResponse) -> Reply:
    return Reply(
        response.status,
        json.dumps(response.body, ensure_ascii=False).encode(),
        JSON_CONTENT_TYPE,
    )


def lambda_response(reply: Reply, headers: Mapping[str, str]) -> dict[str, Any]:
    """`Reply` al formato de respuesta de la Function URL; las cabeceras de
    seguridad van debajo de las propias de la respuesta."""
    return {
        "statusCode": reply.status,
        "headers": {"Content-Type": reply.content_type, **headers, **reply.headers},
        "body": reply.body.decode("utf-8"),
        "isBase64Encoded": False,
    }


def error_reply(error: Exception, request: Incoming) -> Reply:
    """El error de una petición como respuesta JSON, sin detalle de los
    inesperados (sólo el tipo, en el log)."""
    if isinstance(error, ApiError):
        return json_reply(error_response(error))
    if isinstance(error, AuthUnavailable):
        logger.warning("dashboard auth: %s no disponible", error)
        return json_reply(error_response(ApiError("auth_unavailable")))
    logger.warning("dashboard %s: %s", request.method, type(error).__name__)
    return json_reply(error_response(ApiError("internal_error")))


class StackApp:
    def __init__(
        self,
        *,
        config: StackConfig,
        verifier: TokenVerifier,
        clients: ClientIdSource,
        read_api: DashboardApi,
        actions: ActionsGateway | None,
        audit: AuditLog,
        appearance: LiveAppearance,
        directory: CallerDirectory | None = None,
        actions_group: str = DASHBOARD_STACK_ACTIONS_GROUP,
        clock: Callable[[], float] = time.time,
        surface: str = SURFACE_FULL,
    ) -> None:
        self._surface = surface
        self._config = config
        self._verifier = verifier
        self._clients = clients
        self._read_api = read_api
        self._actions = actions
        self._audit = audit
        self._appearance = appearance
        self._directory = directory
        self._actions_group = actions_group
        self._clock = clock
        self._headers = {
            **security_headers([f"https://{config.cognito_domain}"]),
            "Strict-Transport-Security": HSTS_VALUE,
        }

    def respond(self, event: Mapping[str, Any]) -> dict[str, Any]:
        request = incoming_from_event(event)
        try:
            reply = self._route(request)
        except Exception as error:
            reply = error_reply(error, request)
        return lambda_response(reply, self._headers)

    def _route(self, request: Incoming) -> Reply:
        if request.path == API_PREFIX or request.path.startswith(f"{API_PREFIX}/"):
            return self._api(request)
        if self._surface == SURFACE_IAM_API:
            raise ApiError("not_found")
        return self._shell(request)

    def _shell(self, request: Incoming) -> Reply:
        if request.method != GET:
            raise ApiError("method_not_allowed")
        if request.path == CONFIG_PATH:
            return self._config_reply()
        found = self._appearance.files.get(request.path)
        if found is None:
            raise ApiError("not_found")
        return Reply(HTTP_OK, found[0], found[1], LOGO_HEADERS if request.path == LOGO_PATH else {})

    def _config_reply(self) -> Reply:
        ids = self._clients.client_ids()
        if len(ids) != 1:
            raise AuthUnavailable("clients")
        (client_id,) = ids
        document = {
            "auth": {
                "type": "cognito",
                "domain": f"https://{self._config.cognito_domain}",
                "clientId": client_id,
                "scope": COGNITO_SCOPE,
            }
        }
        return json_reply(ApiResponse(HTTP_OK, document))

    def _api(self, request: Incoming) -> Reply:
        if request.method not in ALLOWED_METHODS:
            raise ApiError("method_not_allowed")
        if self._surface == SURFACE_IAM_API and request.iam_arn is None:
            raise ApiError("forbidden", FORBIDDEN_UNSIGNED)
        if request.method == POST:
            self._check_post(request)
        caller = self._authenticate(request)
        api_request = ApiRequest(request.method, request.path, request.query, request.body)
        if is_action_route(request.method, request.path):
            return json_reply(self._action(request, api_request, caller))
        response = self._read_api.handle(api_request)
        if request.path == META_PATH and response.status == HTTP_OK:
            response.body["deployment"] = DEPLOYMENT_STACK
            response.body["allowActions"] = self._may_act(caller)
            branding = self._appearance.branding
            response.body["theme"] = {"name": branding.name, "logo": branding.logo}
        return json_reply(response)

    def _check_post(self, request: Incoming) -> None:
        expected_origin = self._config.allowed_origin or f"https://{request.domain}"
        if request.headers.get("origin") != expected_origin:
            raise ApiError("forbidden", FORBIDDEN_ORIGIN)
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != JSON_TYPE:
            raise ApiError("unsupported_media_type")
        if len(request.body) > DASHBOARD_REQUEST_MAX_BODY_BYTES:
            raise ApiError("payload_too_large")

    def _authenticate(self, request: Incoming) -> Caller:
        token = self._presented_token(request)
        if not token:
            raise ApiError("unauthorized", UNAUTHORIZED_NO_TOKEN)
        try:
            return self._verifier.verify(token)
        except TokenError as error:
            logger.info("dashboard auth: token rechazado (%s)", error.code)
            raise ApiError("unauthorized", UNAUTHORIZED_BAD_TOKEN) from None

    def _presented_token(self, request: Incoming) -> str:
        if self._surface == SURFACE_IAM_API:
            return request.headers.get(IAM_TOKEN_HEADER, "").strip()
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return token.strip() if scheme.lower() == BEARER_SCHEME else ""

    def _may_act(self, caller: Caller) -> bool:
        return self._actions is not None and self._actions_group in caller.groups

    def _action(self, request: Incoming, api_request: ApiRequest, caller: Caller) -> ApiResponse:
        if self._actions is None:
            raise ApiError("actions_disabled", "la pila se desplegó sin acciones (AllowActions)")
        if self._actions_group not in caller.groups:
            raise ApiError("forbidden", FORBIDDEN_GROUP)
        if self._directory is not None and not self._directory.may_act(caller):
            raise ApiError("forbidden", FORBIDDEN_GROUP)
        targets = action_targets(api_request)
        if targets is None:
            raise ApiError("not_found")
        plan = self._read_api.plan_for(targets.action, len(targets.sandbox_ids))
        if plan.estimated_seconds > MAX_JOB_SECONDS:
            raise ApiError("invalid_argument", JOB_TOO_LONG.format(seconds=plan.estimated_seconds))
        try:
            self._audit.requested(
                caller,
                targets.action,
                targets.sandbox_ids,
                request_id=request.request_id,
                source_ip=request.source_ip,
            )
        except AuditUnavailable:
            raise ApiError("audit_unavailable") from None
        response = self._execute(self._actions, caller, api_request)
        self._audit.outcome(
            caller,
            targets.action,
            request_id=request.request_id,
            source_ip=request.source_ip,
            http_status=response.status,
            counts=_counts(response),
        )
        self._read_api.invalidate()
        return response

    def _execute(
        self, gateway: ActionsGateway, caller: Caller, api_request: ApiRequest
    ) -> ApiResponse:
        try:
            api = gateway.api_for(caller)
        except ActionsUnavailable:
            return error_response(ApiError("aws_access_denied"))
        return api.handle(api_request)


def _counts(response: ApiResponse) -> dict[str, int]:
    job = response.body.get("job")
    counts = job.get("counts", {}) if isinstance(job, dict) else {}
    return {str(name): int(value) for name, value in counts.items()}


__all__ = [
    "ActionsGateway",
    "ActionsUnavailable",
    "CallerDirectory",
    "Incoming",
    "Reply",
    "StackApp",
    "error_reply",
    "incoming_from_event",
    "json_reply",
    "lambda_response",
]
