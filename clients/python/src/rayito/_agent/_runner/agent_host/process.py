"""Procesos hijos de los drivers (`opencode`, `http-app`, `command`, `hermes`):
cada uno nace en su propio grupo de procesos para poder matar a todo su
árbol, y `kill()` es idempotente. `Supervisor` vigila al hijo de un driver de
larga vida (`http-app`, `hermes`): si muere después de estar listo lo
relanza con una espera creciente y, pasado su presupuesto, lo da por caído
(`app_crashloop`). Sólo biblioteca estándar."""

from __future__ import annotations

import atexit
import contextlib
import os
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Any, Final

from .diagnostics import sanitize_diagnostics
from .protocol import AgentFailure

#: Lectura de un fragmento de stdout: más pequeño que cualquier línea de
#: evento razonable, pero sin una llamada de sistema por byte.
READ_CHUNK_BYTES: Final = 65536
#: Cuánto se espera a que un hijo muera tras `SIGKILL` antes de seguir.
KILL_WAIT_SECONDS: Final = 5.0
#: Cuánto se espera a que el lector de stderr vea el final de un hijo que ya
#: salió antes de leer su cola.
STDERR_DRAIN_SECONDS: Final = 1.0
#: Lo que se conserva de la salida de error de un hijo: el final, no el
#: principio, porque ahí está el motivo de un fallo.
STDERR_TAIL_BYTES: Final = 8192
#: Cuántas veces se relanza una app que muere dentro de la ventana, la
#: ventana, y la espera inicial y máxima entre relanzamientos
#: (`AGENT_APP_RESTART_MAX`, `AGENT_APP_RESTART_WINDOW_SECONDS`,
#: `AGENT_APP_RESTART_BACKOFF_SECONDS` y `AGENT_APP_RESTART_BACKOFF_MAX_SECONDS`
#: de `limits.json`).
DEFAULT_RESTART_MAX: Final = 3
DEFAULT_RESTART_WINDOW_SECONDS: Final = 300.0
DEFAULT_RESTART_BACKOFF_SECONDS: Final = 2.0
DEFAULT_RESTART_BACKOFF_MAX_SECONDS: Final = 30.0
#: Cada cuánto se sondea la salud de una app que arranca.
HEALTH_POLL_SECONDS: Final = 0.2
#: Plazo por defecto para que una app esté lista tras (re)lanzarla.
DEFAULT_START_TIMEOUT_SECONDS: Final = 90.0
#: Estados de un hijo supervisado.
STATE_RUNNING: Final = "running"
STATE_RESTARTING: Final = "restarting"
STATE_FAILED: Final = "failed"
STATE_STOPPED: Final = "stopped"
#: `detail_code` del marcador de fallo cuando se agota el presupuesto de
#: relanzamientos.
CRASHLOOP_CODE: Final = "app_crashloop"


class _StderrTail:
    """Lee el stderr de un hijo en un hilo y guarda sólo sus últimos
    `STDERR_TAIL_BYTES` bytes, de modo que un hijo muy ruidoso no llena el
    pipe ni la memoria."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self._buffer = b""
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        descriptor = self._stream.fileno()
        for chunk in iter(lambda: os.read(descriptor, READ_CHUNK_BYTES), b""):
            with self._lock:
                self._buffer = (self._buffer + chunk)[-STDERR_TAIL_BYTES:]

    def text(self) -> str:
        self._thread.join(timeout=STDERR_DRAIN_SECONDS)
        with self._lock:
            text = self._buffer.decode("utf-8", errors="replace")
            full = len(self._buffer) >= STDERR_TAIL_BYTES
        # Con el búfer lleno el corte cae a mitad de línea: se descarta ese
        # trozo para que un valor partido no escape a la sustitución.
        newline = text.find("\n")
        return text[newline + 1 :] if full and 0 <= newline < len(text) - 1 else text


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
        capture_stderr_tail: bool = False,
    ) -> None:
        self._process = subprocess.Popen(
            list(argv),
            env=dict(env),
            cwd=cwd,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_stderr_tail else subprocess.DEVNULL,
            start_new_session=True,
        )
        self._stderr_tail = (
            _StderrTail(self._process.stderr)
            if capture_stderr_tail and self._process.stderr is not None
            else None
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

    def stderr_text(self) -> str:
        """El final de lo que escribió el hijo en su salida de error (vacío si
        no se pidió `capture_stderr_tail`)."""
        return "" if self._stderr_tail is None else self._stderr_tail.text()

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


@dataclass(frozen=True)
class RestartPolicy:
    """Cuántos relanzamientos admite un hijo y con qué esperas."""

    max_restarts: int = DEFAULT_RESTART_MAX
    window_seconds: float = DEFAULT_RESTART_WINDOW_SECONDS
    backoff_seconds: float = DEFAULT_RESTART_BACKOFF_SECONDS
    backoff_max_seconds: float = DEFAULT_RESTART_BACKOFF_MAX_SECONDS

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> RestartPolicy:
        """Las claves `restart_max`, `restart_window_seconds`,
        `restart_backoff_seconds` y `restart_backoff_max_seconds` de la
        configuración del driver; una ausente o inválida usa el valor por
        defecto."""
        defaults = cls()
        return cls(
            max_restarts=_non_negative(config.get("restart_max"), defaults.max_restarts),
            window_seconds=float(
                _non_negative(config.get("restart_window_seconds"), defaults.window_seconds)
            ),
            backoff_seconds=float(
                _non_negative(config.get("restart_backoff_seconds"), defaults.backoff_seconds)
            ),
            backoff_max_seconds=float(
                _non_negative(
                    config.get("restart_backoff_max_seconds"), defaults.backoff_max_seconds
                )
            ),
        )


def _non_negative(value: object, default: Any) -> Any:
    valid = isinstance(value, int | float) and not isinstance(value, bool) and value >= 0
    return value if valid else default


class RestartBudget:
    """El presupuesto de relanzamientos: cuenta las muertes dentro de la
    ventana y decide cuánto esperar antes del siguiente. Puro: el reloj lo
    pone quien llama."""

    def __init__(self, policy: RestartPolicy) -> None:
        self._policy = policy
        self._deaths: deque[float] = deque()

    def record_death(self, now: float) -> float | None:
        """Registra una muerte en `now` y devuelve los segundos de espera
        antes de relanzar, o `None` si el presupuesto se agotó: la muerte
        `max_restarts + 1` dentro de la ventana es un bucle de caídas."""
        horizon = now - self._policy.window_seconds
        while self._deaths and self._deaths[0] <= horizon:
            self._deaths.popleft()
        if len(self._deaths) >= self._policy.max_restarts:
            return None
        self._deaths.append(now)
        delay = self._policy.backoff_seconds * 2 ** (len(self._deaths) - 1)
        return float(min(delay, self._policy.backoff_max_seconds))


FailureListener = Callable[[str, str | None], None]


class Supervisor:
    """Arranca el hijo de un driver, espera a que esté listo y lo vigila.

    El primer arranque es síncrono y un fallo es `AgentFailure`
    (`failure_codes[0]` si el hijo sale, `failure_codes[1]` si no contesta a
    tiempo) con el final de su salida de error ya saneado: no se relanza,
    porque lo más probable es una app mal configurada. Una muerte posterior
    pasa a `restarting`, espera `RestartBudget` y relanza; si el relanzado
    no queda listo cuenta como otra muerte. Agotado el presupuesto el
    estado es `failed` y los oyentes reciben `app_crashloop` con la cola de
    su salida de error. `spawn` debe crear el hijo con
    `capture_stderr_tail=True`; `probe` dice si ya contesta."""

    def __init__(
        self,
        spawn: Callable[[], ChildProcess],
        probe: Callable[[], bool],
        *,
        failure_codes: tuple[str, str],
        policy: RestartPolicy | None = None,
        start_timeout_seconds: float = DEFAULT_START_TIMEOUT_SECONDS,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._spawn = spawn
        self._probe = probe
        self._failure_codes = failure_codes
        self._budget = RestartBudget(policy or RestartPolicy())
        self._start_timeout = start_timeout_seconds
        self._environ = os.environ if environ is None else environ
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._state = STATE_STOPPED
        self._child: ChildProcess | None = None
        self._listeners: list[FailureListener] = []
        self._monitor: threading.Thread | None = None
        self.restarts = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def add_failure_listener(self, listener: FailureListener) -> None:
        self._listeners.append(listener)

    def start(self) -> None:
        """El primer arranque; vuelve cuando la app está lista."""
        child = self._spawn()
        with self._lock:
            self._child = child
        self._stop.clear()
        try:
            self._await_ready(child)
        except AgentFailure:
            self.stop()
            raise
        with self._lock:
            self._state = STATE_RUNNING
        atexit.register(self.stop)
        self._monitor = threading.Thread(target=self._watch, daemon=True)
        self._monitor.start()

    def stop(self) -> None:
        """Deja de vigilar y mata al hijo; es idempotente."""
        self._stop.set()
        with self._lock:
            child, self._child = self._child, None
            self._state = STATE_STOPPED
        if child is not None:
            child.kill()

    def _set_state(self, state: str) -> None:
        with self._lock:
            if self._state != STATE_STOPPED:
                self._state = state

    def _current(self) -> ChildProcess | None:
        with self._lock:
            return self._child

    def _failure(self, code: str, child: ChildProcess) -> AgentFailure:
        text = sanitize_diagnostics(child.stderr_text(), self._environ)
        return AgentFailure("runtime_error", code, text or None)

    def _await_ready(self, child: ChildProcess) -> None:
        deadline = self._clock() + self._start_timeout
        while self._clock() < deadline and not self._stop.is_set():
            if child.poll() is not None:
                raise self._failure(self._failure_codes[0], child)
            if self._probe():
                return
            self._stop.wait(HEALTH_POLL_SECONDS)
        raise self._failure(self._failure_codes[1], child)

    def _watch(self) -> None:
        while not self._stop.is_set():
            child = self._current()
            if child is None:
                return
            child.wait()
            if self._stop.is_set() or not self._recover(child):
                return

    def _recover(self, dead: ChildProcess) -> bool:
        """Relanza hasta que el hijo queda listo; `False` si se paró o se
        agotó el presupuesto. Antes se mata a lo que haya dejado el hijo muerto
        en su grupo (el servidor que lanzaba un envoltorio como `npm run`):
        seguiría ocupando el puerto del relanzado."""
        dead.kill()
        last = dead
        while True:
            delay = self._budget.record_death(self._clock())
            if delay is None:
                self._give_up(last)
                return False
            self._set_state(STATE_RESTARTING)
            if self._stop.wait(delay):
                return False
            child = self._spawn()
            with self._lock:
                if self._stop.is_set():
                    child.kill()
                    return False
                self._child = child
            self.restarts += 1
            try:
                self._await_ready(child)
            except AgentFailure:
                child.kill()
                last = child
                continue
            self._set_state(STATE_RUNNING)
            return True

    def _give_up(self, child: ChildProcess) -> None:
        self._set_state(STATE_FAILED)
        text = sanitize_diagnostics(child.stderr_text(), self._environ)
        for listener in self._listeners:
            listener(CRASHLOOP_CODE, text or None)
