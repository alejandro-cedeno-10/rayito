"""Helpers de `ready_cmd` para `set_start_cmd` (E2B `readycmd.py`, puros:
compilan a una cadena de shell que `rayd` ejecuta con `/bin/sh -c` y
comprueban el código de salida, 0 = listo). Cada helper acepta `.timeout()`
para fijar `ReadyPoll.timeout_seconds` en vez del valor por defecto.

Cada argumento va entrecomillado con `shlex.quote` (POSIX `'...'`, sin
expansión de `$()`); `ready-cmds.ts` usa el mismo algoritmo y
`testdata/templates/dockerfile-cases.json` fija la salida de ambos.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Final

from rayito._templates._instructions import ReadyPoll

#: Cadencia de sondeo por defecto de `ready_cmd` (investigación §3.5: rayd
#: sondea hasta que `ready_cmd` sale con 0 o se agota el plazo).
DEFAULT_READY_POLL_INTERVAL_SECONDS: Final = 0.5
#: Plazo por defecto antes de que `rayd` considere el `ready_cmd` fallido
#: (alineado con `readyTimeoutInSeconds` de `IMAGE_HOOKS`, 600 s: AWS deja
#: de esperar ahí de todos modos, así que un plazo más corto en el lado de
#: `rayd` es el que de verdad decide cuándo se da por vencido).
DEFAULT_READY_TIMEOUT_SECONDS: Final = 60.0


@dataclass(frozen=True, slots=True)
class ReadyCommand:
    """Un `ready_cmd` compilado: el comando de shell y el sondeo con el que
    `rayd` lo ejecuta."""

    cmd: str
    poll: ReadyPoll

    def timeout(self, seconds: float) -> ReadyCommand:
        """Sustituye `ReadyPoll.timeout_seconds` por `seconds` (debe ser >
        0); la cadencia de sondeo no cambia."""
        if seconds <= 0:
            raise ValueError(f"timeout debe ser > 0, se dio {seconds!r}")
        return ReadyCommand(self.cmd, ReadyPoll(self.poll.interval_seconds, seconds))


def _ready_command(cmd: str) -> ReadyCommand:
    return ReadyCommand(
        cmd, ReadyPoll(DEFAULT_READY_POLL_INTERVAL_SECONDS, DEFAULT_READY_TIMEOUT_SECONDS)
    )


def wait_for_port(port: int) -> ReadyCommand:
    """Listo cuando algo escucha en `port` (TCP, loopback)."""
    return _ready_command(f"cat < /dev/null > /dev/tcp/127.0.0.1/{port}")


def wait_for_url(url: str, *, status: int = 200) -> ReadyCommand:
    """Listo cuando `url` responde `status` (curl, sólo cabeceras)."""
    return _ready_command(
        f"test \"$(curl -s -o /dev/null -w '%{{http_code}}' {shlex.quote(url)})\" = '{status}'"
    )


def wait_for_process(name: str) -> ReadyCommand:
    """Listo cuando hay un proceso cuyo nombre contiene `name` (`pgrep -f`)."""
    return _ready_command(f"pgrep -f {shlex.quote(name)} > /dev/null")


def wait_for_file(path: str) -> ReadyCommand:
    """Listo cuando `path` existe."""
    return _ready_command(f"test -e {shlex.quote(path)}")
