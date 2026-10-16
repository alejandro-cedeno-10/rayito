"""Procesos hijos de los drivers (`opencode`, `http-app`, `command`): cada
uno nace en su propio grupo de procesos para poder matar a todo su árbol, y
`kill()` es idempotente. Sólo biblioteca estándar."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from typing import IO, Final

#: Lectura de un fragmento de stdout: más pequeño que cualquier línea de
#: evento razonable, pero sin una llamada de sistema por byte.
READ_CHUNK_BYTES: Final = 65536
#: Cuánto se espera a que un hijo muera tras `SIGKILL` antes de seguir.
KILL_WAIT_SECONDS: Final = 5.0


class ChildProcess:
    """Un hijo con su propio grupo de procesos."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str | None = None,
        stdin: bool = False,
        capture_stdout: bool = True,
    ) -> None:
        self._process = subprocess.Popen(
            list(argv),
            env=dict(env),
            cwd=cwd,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @property
    def stdin(self) -> IO[bytes]:
        assert self._process.stdin is not None
        return self._process.stdin

    def send_and_close(self, data: bytes) -> None:
        """Escribe `data` en stdin y lo cierra; un hijo que ya salió no es
        un error."""
        with contextlib.suppress(BrokenPipeError, OSError):
            self.stdin.write(data)
            self.stdin.flush()
        with contextlib.suppress(BrokenPipeError, OSError):
            self.stdin.close()

    def lines(self, max_line_bytes: int) -> Iterator[tuple[bytes, bool]]:
        """Las líneas de stdout como `(bytes, descartada)`: una más larga
        que `max_line_bytes` se descarta entera, no se parte."""
        assert self._process.stdout is not None
        descriptor = self._process.stdout.fileno()
        pending = b""
        overflow = False
        for chunk in iter(lambda: os.read(descriptor, READ_CHUNK_BYTES), b""):
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                yield (b"", True) if overflow or len(line) > max_line_bytes else (line, False)
                overflow = False
            if len(pending) > max_line_bytes:
                pending = b""
                overflow = True
        if pending and not overflow:
            yield pending, False

    def wait(self) -> int:
        return self._process.wait()

    def poll(self) -> int | None:
        return self._process.poll()

    def kill(self) -> None:
        """`SIGKILL` al grupo entero; no falla si ya terminó."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._process.wait(timeout=KILL_WAIT_SECONDS)
