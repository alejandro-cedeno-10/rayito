"""El adaptador de logs del panel: lee el final del stream de CloudWatch de
un sandbox con las mismas funciones que `rayito sandbox logs`. Sólo
lecturas (`GetMicrovm`, `DescribeLogStreams`, `GetLogEvents`) y sólo cuando
la SPA pide los logs de un sandbox. CloudWatch no publica una tarifa propia
para esas dos llamadas (https://aws.amazon.com/cloudwatch/pricing/, consultado
2026-10-13): sólo cobra ingesta, almacenamiento y Logs Insights, y el panel
no hace ninguna consulta de Logs Insights. Muestra las
últimas líneas, que son las que importan al operar. Vive fuera de
`rayito.cli` para que las dos cáscaras del panel (local y en la cuenta)
compartan el mismo adaptador."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from rayito._aws import ControlPlane, sanitized_client_error
from rayito._cloudwatch_logs import (
    LogsNotFound,
    default_log_group,
    event_timestamp,
    expected_stream_name,
    find_streams,
    tail_events,
)
from rayito._dashboard._api._contract import LogLine, LogPage
from rayito._limits import DASHBOARD_LOG_LINE_MAX_CHARS


class CloudWatchLogsSource:
    def __init__(
        self, logs: Any, control_plane: ControlPlane, *, log_group: str | None = None
    ) -> None:
        self._logs = logs
        self._control_plane = control_plane
        self._log_group = log_group

    def read(self, sandbox_id: str, *, limit: int) -> LogPage:
        """Las últimas `limit` líneas en orden cronológico; un error de
        CloudWatch sale como excepción del SDK (con su resumen sin secretos
        en `__cause__`), nunca como `ClientError`."""
        failure: tuple[Exception, BaseException] | None = None
        try:
            return self._read(sandbox_id, limit)
        except ClientError as exc:
            failure = sanitized_client_error(exc)
        raise failure[0] from failure[1]

    def _read(self, sandbox_id: str, limit: int) -> LogPage:
        info = self._control_plane.get_microvm(sandbox_id)
        group = self._log_group or default_log_group(info.template)
        try:
            streams = find_streams(self._logs, group, info)
        except LogsNotFound:
            return LogPage((), truncated=False)
        fallback = streams != [expected_stream_name(info)]
        lines: list[LogLine] = []
        for stream in streams:
            events = tail_events(self._logs, group, stream, limit=limit + 1 - len(lines))
            lines[:0] = [
                LogLine(
                    event_timestamp(event),
                    str(event["message"]).rstrip("\n")[:DASHBOARD_LOG_LINE_MAX_CHARS],
                    stream,
                )
                for event in events
            ]
            if len(lines) > limit:
                break
        return LogPage(tuple(lines[-limit:]), truncated=len(lines) > limit, fallback=fallback)
