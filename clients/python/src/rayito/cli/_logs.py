"""Logs de runtime de un sandbox en CloudWatch: resolución del stream y
paginación de eventos.

El grupo por defecto es `/rayito/<nombre de la imagen>` (el que el SDK pasa
con `logging="cloudwatch"` y el que publica la imagen); el stream se llama
`YYYY/MM/DD[<imageVersion>]<microvmId>` (`AWS_API_NOTES.md` §14, medido).
Si el nombre exacto no existe (un stream creado otro día UTC, `Q55`), se
recorren como mucho 10 páginas de 50 streams ordenados por último evento
buscando los que terminan en `]<microvmId>`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError

from rayito._models import SandboxInfo, template_name_from_arn
from rayito._sandbox_base import LOG_GROUP_PREFIX

STREAM_PAGE_SIZE = 50
MAX_SCAN_PAGES = 10
DEFAULT_EVENT_LIMIT = 1000
NOT_FOUND = "ResourceNotFoundException"
RELATIVE_SINCE = re.compile(r"^(\d+)([smhd])$")
RELATIVE_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
NO_LOGS_MESSAGE = (
    "sin logs: el sandbox se lanzó con logging disabled, sin executionRoleArn, "
    "o en otro grupo (--log-group)"
)


class LogsNotFound(Exception):
    """Ni el stream esperado ni ninguno del sandbox existe en el grupo."""

    def __init__(self, group: str) -> None:
        super().__init__(f"{NO_LOGS_MESSAGE}; grupo consultado: {group}")
        self.group = group


def default_log_group(template_arn: str) -> str:
    return f"{LOG_GROUP_PREFIX}/{template_name_from_arn(template_arn)}"


def expected_stream_name(info: SandboxInfo) -> str:
    day = info.started_at.astimezone(UTC).strftime("%Y/%m/%d")
    return f"{day}[{info.template_version}]{info.sandbox_id}"


def stream_names(page: dict[str, Any]) -> list[str]:
    return [str(stream["logStreamName"]) for stream in page.get("logStreams", [])]


def exact_stream(logs: Any, group: str, candidate: str) -> str | None:
    response = logs.describe_log_streams(logGroupName=group, logStreamNamePrefix=candidate)
    return candidate if candidate in stream_names(response) else None


def scan_streams(logs: Any, group: str, sandbox_id: str) -> list[str]:
    suffix = f"]{sandbox_id}"
    found: list[str] = []
    token: str | None = None
    for _ in range(MAX_SCAN_PAGES):
        params: dict[str, Any] = {
            "logGroupName": group,
            "orderBy": "LastEventTime",
            "descending": True,
            "limit": STREAM_PAGE_SIZE,
        }
        if token is not None:
            params["nextToken"] = token
        page = logs.describe_log_streams(**params)
        found.extend(name for name in stream_names(page) if name.endswith(suffix))
        token = page.get("nextToken")
        if not token:
            break
    return found


def find_streams(logs: Any, group: str, info: SandboxInfo) -> list[str]:
    """El stream con el nombre esperado o, si no existe, los que el
    recorrido acotado encuentra; `LogsNotFound` si no hay ninguno o el
    grupo no existe."""
    try:
        exact = exact_stream(logs, group, expected_stream_name(info))
        if exact is not None:
            return [exact]
        found = scan_streams(logs, group, info.sandbox_id)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == NOT_FOUND:
            raise LogsNotFound(group) from exc
        raise
    if not found:
        raise LogsNotFound(group)
    return found


def iter_events(
    logs: Any,
    group: str,
    stream: str,
    *,
    start_time_ms: int | None = None,
    limit: int = DEFAULT_EVENT_LIMIT,
) -> Iterator[dict[str, Any]]:
    """`get-log-events` desde el principio hasta que `nextForwardToken` se
    repite (fin del stream) o se alcanzan `limit` eventos."""
    emitted = 0
    token: str | None = None
    while emitted < limit:
        params: dict[str, Any] = {
            "logGroupName": group,
            "logStreamName": stream,
            "startFromHead": True,
        }
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if token is not None:
            params["nextToken"] = token
        page = logs.get_log_events(**params)
        for event in page.get("events", []):
            yield event
            emitted += 1
            if emitted >= limit:
                return
        next_token = page.get("nextForwardToken")
        if not next_token or next_token == token:
            return
        token = next_token


def parse_since(text: str, now: datetime | None = None) -> datetime:
    """`30m`, `2h`, `1d` (relativo a `now`) o una fecha ISO-8601; una fecha
    sin zona se interpreta en UTC."""
    current = now or datetime.now(UTC)
    relative = RELATIVE_SINCE.match(text.strip())
    if relative:
        amount, unit = relative.groups()
        return current - timedelta(**{RELATIVE_UNITS[unit]: int(amount)})
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"--since no reconocido: {text!r} (usa 30m, 2h, 1d o ISO-8601)") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def event_timestamp(event: dict[str, Any]) -> str:
    moment = datetime.fromtimestamp(int(event["timestamp"]) / 1000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
