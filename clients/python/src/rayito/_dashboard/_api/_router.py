"""`DashboardApi`: el núcleo de `/api/v1`, sin transporte ni autenticación
(eso es de cada cáscara: la local con sesión, `Host`/`Origin` y CSRF; la de
la fase 2 con su propio control de acceso). Traduce una `ApiRequest` en
llamadas a `Fleet`, `Quotas` y `UsageCost` y devuelve un documento JSON.

- Sólo lectura por defecto: las acciones exigen `allow_actions=True` y un
  `"confirm": true` en el cuerpo, y se encolan en `BulkJobs` al ritmo del TPS.
- El coste facturado (Cost Explorer, de pago) exige `cost_explorer=True` y
  `?billed=true`; sin ello la API sólo da la estimación, que es gratuita.
- Las respuestas nunca llevan el mensaje de AWS: sólo códigos.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, TypeVar, cast

from rayito._dashboard._api._contract import (
    ACTION_NAMES,
    API_PREFIX,
    API_VERSION,
    DEPLOYMENT_LOCAL,
    ERROR_CODES,
    ERROR_MESSAGES,
    HTTP_ACCEPTED,
    HTTP_OK,
    ApiRequest,
    ApiResponse,
    LogsSource,
)
from rayito._dashboard._api._jobs import BulkJobs, JobQueue, JobsBusy
from rayito._dashboard._api._serialize import (
    billed_json,
    estimate_json,
    event_json,
    image_json,
    image_version_json,
    plan_json,
    quotas_json,
    sandbox_json,
)
from rayito._dashboard._cost import UsageCost
from rayito._dashboard._domain import (
    BULK_OPERATIONS,
    BulkAction,
    BulkPlan,
    QuotaReport,
    TtlCache,
)
from rayito._dashboard._fleet import Fleet, validated_sandbox_ids
from rayito._dashboard._quotas import Quotas
from rayito._dashboard._theme import Branding
from rayito._lifecycle_events._domain import DEFAULT_GET_EVENTS_LIMIT
from rayito._limits import (
    COST_CACHE_TTL_SECONDS,
    COST_EXPLORER_REQUEST_PRICE_USD,
    DASHBOARD_BULK_MAX_SANDBOXES,
    DASHBOARD_EVENTS_MAX_LIMIT,
    DASHBOARD_JOB_POLL_INTERVAL_SECONDS,
    DASHBOARD_LOGS_MAX_LINES,
    DASHBOARD_POLL_INTERVAL_SECONDS,
    DASHBOARD_READ_CACHE_SECONDS,
)
from rayito._version import __version__
from rayito.exceptions import (
    AuthenticationException,
    InvalidArgumentException,
    NotFoundException,
    QuotaExceededException,
    RateLimitException,
    SandboxException,
)

logger = logging.getLogger("rayito.dashboard")

T = TypeVar("T")
Handler = Callable[[ApiRequest, re.Match[str]], ApiResponse]

SEGMENT: Final = r"([^/]+)"
SANDBOX_ACTION_PATTERN: Final = re.compile(
    re.escape(API_PREFIX) + f"/sandboxes/{SEGMENT}/{SEGMENT}"
)
BULK_ACTION_PATTERN: Final = re.compile(re.escape(API_PREFIX) + "/actions")
GET: Final = "GET"
POST: Final = "POST"
COST_SUFFIX: Final = "/cost"
COST_PATH: Final = f"{API_PREFIX}{COST_SUFFIX}"
TRUTHY: Final = frozenset({"1", "true", "yes"})
EVENT_ORDERS: Final = ("asc", "desc")
DEFAULT_EVENT_ORDER: Final = "desc"
MIN_QUERY_LIMIT: Final = 1
#: Estados válidos del filtro `?state=` de `/sandboxes`; el dominio los valida.
SANDBOX_STATE_PARAM: Final = "state"


class ApiError(Exception):
    """Un error que se devuelve tal cual: `code` de `ERROR_CODES` y, si la
    hay, la causa de AWS por su código."""

    def __init__(self, code: str, message: str | None = None, *, aws_code: str | None = None):
        super().__init__(code)
        self.code = code
        self.message = message or ERROR_MESSAGES[code]
        self.aws_code = aws_code


def error_response(error: ApiError) -> ApiResponse:
    document: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.aws_code is not None:
        document["awsCode"] = error.aws_code
    return ApiResponse(ERROR_CODES[error.code], {"error": document})


def translate(exc: Exception) -> ApiError:
    """Una excepción del SDK en un `ApiError`, por clase y código de AWS,
    nunca por el texto del mensaje (que no se repite)."""
    aws_code = getattr(exc, "aws_code", None)
    if isinstance(exc, InvalidArgumentException):
        if aws_code is not None:
            return ApiError("invalid_argument", aws_code=aws_code)
        return ApiError("invalid_argument", str(exc))
    if isinstance(exc, NotFoundException):
        return ApiError("not_found")
    if isinstance(exc, AuthenticationException):
        return ApiError("aws_access_denied", aws_code=aws_code)
    if isinstance(exc, RateLimitException | QuotaExceededException):
        return ApiError("aws_throttled", aws_code=aws_code)
    if isinstance(exc, SandboxException):
        return ApiError("aws_error", aws_code=aws_code)
    return ApiError("internal_error")


@dataclass(frozen=True)
class ActionTargets:
    """Qué pide una petición de acción ya validada: la acción, los ids
    (únicos y con su longitud comprobada) y la confirmación presente."""

    action: BulkAction
    sandbox_ids: tuple[str, ...]


def is_action_route(method: str, path: str) -> bool:
    """`True` para `POST /sandboxes/<id>/<acción>` y `POST /actions`: las dos
    rutas que ejecutan algo sobre un sandbox."""
    return method == POST and (
        SANDBOX_ACTION_PATTERN.fullmatch(path) is not None
        or BULK_ACTION_PATTERN.fullmatch(path) is not None
    )


def action_targets(request: ApiRequest) -> ActionTargets | None:
    """Los objetivos de `POST /sandboxes/<id>/<acción>` y de `POST /actions`,
    o `None` si la petición no es una de esas dos rutas. Lanza el mismo
    `ApiError` que la ruta; una cáscara que necesita saber qué se va a tocar
    antes de ejecutarlo (la auditoría de la pila `dashboard`) lo llama antes
    de delegar."""
    if not is_action_route(request.method, request.path):
        return None
    single = SANDBOX_ACTION_PATTERN.fullmatch(request.path)
    if single is not None:
        return _targets(_action(single.group(2)), [single.group(1)], request)
    body = _json_body(request)
    ids = body.get("sandboxIds")
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise ApiError("invalid_argument", "sandboxIds: se esperaba una lista de ids")
    return _targets(_action(body.get("action")), ids, request)


def _targets(action: BulkAction, ids: list[str], request: ApiRequest) -> ActionTargets:
    if _json_body(request).get("confirm") is not True:
        raise ApiError("confirmation_required")
    if len(ids) > DASHBOARD_BULK_MAX_SANDBOXES:
        raise ApiError(
            "invalid_argument",
            f"sandboxIds: como mucho {DASHBOARD_BULK_MAX_SANDBOXES} ids por acción",
        )
    return ActionTargets(action, validated_sandbox_ids(ids))


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler


class DashboardApi:
    """El contrato `/api/v1` sobre los servicios del SDK."""

    def __init__(
        self,
        *,
        fleet: Fleet,
        quotas: Quotas,
        cost: UsageCost,
        region: str,
        jobs: JobQueue | None = None,
        logs: LogsSource | None = None,
        events_configured: bool = False,
        cost_explorer: bool = False,
        branding: Branding | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fleet = fleet
        self._quotas = quotas
        self._cost = cost
        self._region = region
        self._logs = logs
        self._events_configured = events_configured
        self._cost_explorer = cost_explorer
        self._branding = branding or Branding()
        self._cache: TtlCache[str, Any] = TtlCache(DASHBOARD_READ_CACHE_SECONDS, monotonic)
        self._jobs = jobs or BulkJobs(fleet, on_finished=self.invalidate)
        self._generation = 0
        self._generation_lock = threading.Lock()
        self._routes = self._build_routes()

    @property
    def jobs(self) -> JobQueue:
        return self._jobs

    def invalidate(self) -> None:
        """Descarta las lecturas cacheadas (tras una acción). La generación
        impide que una lectura que empezó antes vuelva a guardar su dato viejo."""
        with self._generation_lock:
            self._generation += 1
        self._cache.clear()

    def close(self) -> None:
        self._jobs.close()

    def requires_csrf(self, request: ApiRequest) -> bool:
        """Si la petición cambia algo o gasta dinero. Las cáscaras le exigen
        entonces su defensa CSRF: toda escritura y la consulta facturada de
        Cost Explorer, que aunque sea un `GET` cuesta cada vez."""
        if request.method != GET:
            return True
        return request.path == COST_PATH and _flag(request.query, "billed")

    def handle(self, request: ApiRequest) -> ApiResponse:
        try:
            return self._dispatch(request)
        except ApiError as error:
            return error_response(error)
        except Exception as exc:
            logger.warning("dashboard %s: %s", request.method, type(exc).__name__)
            return error_response(translate(exc))

    def _dispatch(self, request: ApiRequest) -> ApiResponse:
        matched_path = False
        for route in self._routes:
            match = route.pattern.fullmatch(request.path)
            if match is None:
                continue
            matched_path = True
            if route.method == request.method:
                return route.handler(request, match)
        raise ApiError("method_not_allowed" if matched_path else "not_found")

    def _build_routes(self) -> list[Route]:
        def route(method: str, suffix: str, handler: Handler) -> Route:
            return Route(method, re.compile(re.escape(API_PREFIX) + suffix), handler)

        return [
            route(GET, "/meta", self._meta),
            route(GET, "/sandboxes", self._sandboxes),
            route(POST, f"/sandboxes/{SEGMENT}/{SEGMENT}", self._submit),
            route(GET, "/images", self._images),
            route(GET, "/images/versions", self._image_versions),
            route(GET, "/quotas", self._quotas_report),
            route(GET, COST_SUFFIX, self._cost_report),
            route(GET, "/events", self._events),
            route(GET, "/logs", self._log_lines),
            route(GET, "/actions", self._list_jobs),
            route(GET, "/actions/plan", self._plan),
            route(GET, f"/actions/{SEGMENT}", self._job),
            route(POST, "/actions", self._submit),
        ]

    def _cached(self, key: str, load: Callable[[], T]) -> T:
        found = self._cache.get(key)
        if found is not None:
            return cast(T, found)
        generation = self._generation
        value = load()
        with self._generation_lock:
            if generation == self._generation:
                self._cache.put(key, value)
        return value

    def _ok(self, body: dict[str, Any], status: int = HTTP_OK) -> ApiResponse:
        return ApiResponse(status, body)

    def _meta(self, request: ApiRequest, match: re.Match[str]) -> ApiResponse:
        return self._ok(
            {
                "apiVersion": API_VERSION,
                "rayitoVersion": __version__,
                "region": self._region,
                "deployment": DEPLOYMENT_LOCAL,
                "allowActions": self._fleet.allow_actions,
                "costExplorer": self._cost_explorer_json(),
                "eventsConfigured": self._events_configured,
                "logsAvailable": self._logs is not None,
                "pollIntervalSeconds": DASHBOARD_POLL_INTERVAL_SECONDS,
                "jobPollIntervalSeconds": DASHBOARD_JOB_POLL_INTERVAL_SECONDS,
                "bulkMaxSandboxes": DASHBOARD_BULK_MAX_SANDBOXES,
                "theme": {"name": self._branding.name, "logo": self._branding.logo},
            }
        )

    def _cost_explorer_json(self) -> dict[str, Any]:
        return {
            "enabled": self._cost_explorer,
            "requestPriceUsd": COST_EXPLORER_REQUEST_PRICE_USD,
            "cacheTtlSeconds": COST_CACHE_TTL_SECONDS,
        }

    def _sandboxes(self, request: ApiRequest, match: re.Match[str]) -> ApiResponse:
        states = tuple(request.query.get(SANDBOX_STATE_PARAM, ())) or None
        image = _single(request.query, "image")
        key = f"sandboxes|{states}|{image}"
        views = self._cached(key, lambda: self._fleet.sandboxes(states=states, image=image))
        return self._ok({"sandboxes": [sandbox_json(view) for view in views]})

    def _images(self, request: ApiRequest, match: re.Match[str]) -> ApiResponse:
        name_filter = _single(request.query, "filter")
        images = self._cached(
            f"images|{name_filter}", lambda: self._fleet.images(name_filter=name_filter)
        )
        return self._ok({"images": [image_json(image) for image in images]})

    def _image_versions(self, request: ApiRequest, match: re.Match[str]) -> ApiResponse:
        image = _required(request.query, "image")
        versions = self._cached(f"versions|{image}", lambda: self._fleet.image_versions(image))
        return self._ok(
            {"image": image, "versions": [image_version_json(item) for item in versions]}
        )

    def _quota_report(self) -> QuotaReport:
        return self._cached("quotas", self._quotas.report)

    def _quotas_report(self, request: ApiRequest, match: re.Match[str]) -> ApiResponse:
        return self._ok(quotas_json(self._quota_report()))

    def _cost_report(self, request: ApiRequest, match: re.Match[str]) -> ApiResponse:
        estimate = self._cached("estimate", self._cost.estimate)
        billed = None
        if _flag(request.query, "billed"):
            if not self._cost_explorer:
                raise ApiError("cost_explorer_disabled")
            billed = billed_json(
                self._cost.billed(
                    start=_single(request.query, "start"),
                    end=_single(request.query, "end"),
                    region=_single(request.query, "region"),
                    refresh=_flag(request.query, "refresh"),
                )
            )
        return self._ok(
            {
                "estimated": estimate_json(estimate),
                "billed": billed,
                "costExplorer": self._cost_explorer_json(),
            }
        )

    def _events(self, request: ApiRequest, match: re.Match[str]) -> ApiResponse:
        if not self._events_configured:
            return self._ok({"configured": False, "events": []})
        limit = _bounded_int(
            request.query, "limit", DEFAULT_GET_EVENTS_LIMIT, DASHBOARD_EVENTS_MAX_LIMIT
        )
        order = _single(request.query, "order") or DEFAULT_EVENT_ORDER
        if order not in EVENT_ORDERS:
            raise ApiError("invalid_argument", "order debe ser 'asc' o 'desc'")
        events = self._fleet.events(
            sandbox_id=_single(request.query, "sandboxId"),
            types=tuple(request.query.get("type", ())) or None,
            limit=limit,
            order=order,
        )
        return self._ok({"configured": True, "events": [event_json(event) for event in events]})

    def _log_lines(self, request: ApiRequest, match: re.Match[str]) -> ApiResponse:
        if self._logs is None:
            raise ApiError("not_found", "los logs no están disponibles en este panel")
        (sandbox_id,) = validated_sandbox_ids([_required(request.query, "sandboxId")])
        limit = _bounded_int(
            request.query, "limit", DASHBOARD_LOGS_MAX_LINES, DASHBOARD_LOGS_MAX_LINES
        )
        page = self._logs.read(sandbox_id, limit=limit)
        return self._ok(
            {
                "sandboxId": sandbox_id,
                "truncated": page.truncated,
                "fallback": page.fallback,
                "lines": [
                    {"timestamp": line.timestamp, "message": line.message, "stream": line.stream}
                    for line in page.lines
                ],
            }
        )

    def _list_jobs(self, request: ApiRequest, match: re.Match[str]) -> ApiResponse:
        return self._ok({"jobs": self._jobs.snapshots()})

    def _job(self, request: ApiRequest, match: re.Match[str]) -> ApiResponse:
        job = self._jobs.get(match.group(1))
        if job is None:
            raise ApiError("not_found")
        return self._ok({"job": job})

    def _plan(self, request: ApiRequest, match: re.Match[str]) -> ApiResponse:
        action = _action(_required(request.query, "action"))
        count = _bounded_int(request.query, "count", MIN_QUERY_LIMIT, DASHBOARD_BULK_MAX_SANDBOXES)
        return self._ok({"plan": plan_json(self.plan_for(action, count))})

    def plan_for(self, action: BulkAction, count: int) -> BulkPlan:
        """Cuánto tardaría una acción masiva sobre `count` sandboxes con la
        cuota de TPS aplicada. Es un cálculo local más la lectura de cuotas
        (gratuita); la cáscara de la pila lo usa para rechazar antes de
        auditar un trabajo que no cabe en el tiempo de la función."""
        return self._fleet.plan_bulk(action, count, applied_rate=self._applied_rate(action))

    def _submit(self, request: ApiRequest, match: re.Match[str]) -> ApiResponse:
        self._require_actions()
        targets = action_targets(request)
        if targets is None:
            raise ApiError("not_found")
        rate = self._applied_rate(targets.action)
        plan = self._fleet.plan_bulk(targets.action, len(targets.sandbox_ids), applied_rate=rate)
        try:
            job = self._jobs.submit(
                targets.action, targets.sandbox_ids, plan=plan, applied_rate=rate
            )
        except JobsBusy:
            raise ApiError("jobs_busy") from None
        return self._ok({"job": job}, HTTP_ACCEPTED)

    def _require_actions(self) -> None:
        if not self._fleet.allow_actions:
            raise ApiError("actions_disabled")

    def _applied_rate(self, action: BulkAction) -> float | None:
        """La cuota de TPS aplicada de la operación, o `None` (el ritmo por
        defecto) si Service Quotas no se puede leer: una acción no debe
        bloquearse por un permiso de lectura que falte."""
        try:
            return self._quota_report().applied_rate(BULK_OPERATIONS[action])
        except (SandboxException, AuthenticationException):
            return None


def _single(query: Mapping[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


def _required(query: Mapping[str, list[str]], name: str) -> str:
    value = _single(query, name)
    if not value:
        raise ApiError("invalid_argument", f"falta el parámetro {name}")
    return value


def _flag(query: Mapping[str, list[str]], name: str) -> bool:
    value = _single(query, name)
    return value is not None and value.lower() in TRUTHY


def _bounded_int(query: Mapping[str, list[str]], name: str, default: int, maximum: int) -> int:
    raw = _single(query, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError("invalid_argument", f"{name}: se esperaba un entero") from None
    if not MIN_QUERY_LIMIT <= value <= maximum:
        raise ApiError("invalid_argument", f"{name}: entre {MIN_QUERY_LIMIT} y {maximum}")
    return value


def _action(value: object) -> BulkAction:
    if value not in ACTION_NAMES:
        admitted = ", ".join(ACTION_NAMES)
        raise ApiError("invalid_argument", f"action: no es válida (admitidas: {admitted})")
    return value


def _json_body(request: ApiRequest) -> dict[str, Any]:
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        raise ApiError("invalid_json") from None
    if not isinstance(body, dict):
        raise ApiError("invalid_json")
    return cast(dict[str, Any], body)


__all__ = [
    "ActionTargets",
    "ApiError",
    "DashboardApi",
    "action_targets",
    "error_response",
    "translate",
]
