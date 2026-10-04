"""Lectura de "fallo diferido" por logs (investigación TPL-1/Q83, TPL-5/Q85):
dado que el grupo de logs de la imagen recibe la salida completa de
BuildKit de golpe al terminar el build (`#N [k/n] RUN ...`, `exit code: N`),
`parse_build_failure` la relee para decir qué paso falló, con qué comando y
código de salida, sin volver a correr nada. Puro: recibe líneas de texto ya
traídas por `_build.py` (`logs:GetLogEvents`), nunca llama a AWS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: `#5 [3/6] RUN pip install ...` — el paso `k` de `n` y el comando
#: (BuildKit, investigación TPL-1: "`#N [k/n] RUN …`").
_STEP_RE: Final = re.compile(r"^#\d+ \[(\d+)/(\d+)\] (.+)$")
#: `#5 ERROR: executor failed running [...]: exit code: 3`.
_EXIT_CODE_RE: Final = re.compile(r"exit code:\s*(\d+)")
#: Cuántas líneas finales del grupo de logs guarda `log_tail` (suficiente
#: para ver el error sin repetir el log entero en la excepción).
_LOG_TAIL_LINES: Final = 20

_CLIENT_ERROR_MARKER: Final = "HTTP 4xx"
_SERVER_ERROR_MARKER: Final = "HTTP 5xx"


@dataclass(frozen=True, slots=True)
class FailureDetail:
    """Lo que `BuildException` expone cuando un `RUN` falló: `step`/`command`
    vienen del log de BuildKit, `exit_code` de la línea `exit code: N`,
    `log_tail` son las últimas líneas del stream para contexto."""

    step: int | None
    command: str | None
    exit_code: int | None
    log_tail: str | None


def parse_build_failure(log_lines: list[str]) -> FailureDetail:
    """Recorre `log_lines` (ya en orden cronológico) y se queda con el
    último paso `RUN` mencionado y el último `exit code:` visto: BuildKit
    repite el paso que falló varias veces (anuncio + salida + error), así
    que la última aparición es la más completa."""
    step: int | None = None
    command: str | None = None
    exit_code: int | None = None
    for line in log_lines:
        step_match = _STEP_RE.match(line.strip())
        if step_match:
            step = int(step_match.group(1))
            command = step_match.group(3)
        exit_match = _EXIT_CODE_RE.search(line)
        if exit_match:
            exit_code = int(exit_match.group(1))
    tail = "\n".join(log_lines[-_LOG_TAIL_LINES:]) if log_lines else None
    return FailureDetail(step=step, command=command, exit_code=exit_code, log_tail=tail)


def classify_ready_failure(state_reason: str | None) -> str | None:
    """`stateReason` de una versión `FAILED` por un `ready_cmd`/CMD que
    respondió 4xx o 5xx (investigación Q85: "Ready hook check failed: the
    application returned a {client,server} error (HTTP {4xx,5xx})
    response") -> `"ready_client_error"`/`"ready_server_error"`; cualquier
    otro motivo (paso de Dockerfile, timeout) da `None`, y entonces el
    fallo se explica por `parse_build_failure` en su lugar."""
    if not state_reason:
        return None
    if _SERVER_ERROR_MARKER in state_reason:
        return "ready_server_error"
    if _CLIENT_ERROR_MARKER in state_reason:
        return "ready_client_error"
    return None
