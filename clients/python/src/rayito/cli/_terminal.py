"""El puente de terminal de `rayito sandbox create|connect`: una PTY del
sandbox conectada a la terminal local. Biblioteca estándar y SDK; se
importa sin el extra `cli`.

Tres modos de entrada, según `stdin`:

- terminal POSIX: modo raw (`tty.setraw`, atributos restaurados al salir),
  `SIGWINCH` reenvía el tamaño y Ctrl-C viaja como `\\x03`, nunca como
  SIGINT local;
- consola de Windows: `msvcrt.getwch` con las teclas extendidas (flechas,
  Inicio, Fin, Supr) traducidas a secuencias VT y el tamaño sondeado cada
  segundo;
- cualquier otra cosa (tuberías): los bytes tal cual y `\\x04` al EOF.

La lectura corre en un hilo daemon que nunca retrasa el retorno. Los bytes
de la terminal nunca se loguean.
"""

from __future__ import annotations

import importlib
import os
import select
import shutil
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import IO, TYPE_CHECKING, Any, Final, cast

from rayito._models import PtySize
from rayito.exceptions import (
    CommandExitException,
    NotFoundException,
    SandboxException,
    TimeoutException,
)

if TYPE_CHECKING:
    from rayito.sandbox_sync.main import Sandbox
    from rayito.sandbox_sync.pty import PtyHandle

IS_WINDOWS: Final = os.name == "nt"
DEFAULT_SIZE: Final = PtySize(cols=80, rows=24)
READ_CHUNK_BYTES: Final = 4096
EOF_BYTE: Final = b"\x04"
EXIT_TIMEOUT: Final = 124
RESIZE_POLL_SECONDS: Final = 1.0
KEY_POLL_SECONDS: Final = 0.02
READER_POLL_SECONDS: Final = 0.05
READER_STOP_SECONDS: Final = 1.0
WINDOWS_EXTENDED_PREFIXES: Final = ("\x00", "\xe0")
WINDOWS_EXTENDED_KEYS: Final[Mapping[str, str]] = {
    "H": "\x1b[A",
    "P": "\x1b[B",
    "K": "\x1b[D",
    "M": "\x1b[C",
    "G": "\x1b[H",
    "O": "\x1b[F",
    "S": "\x1b[3~",
}

Sender = Callable[[bytes], None]


def run_terminal(
    sandbox: Sandbox,
    *,
    user: str | None = None,
    cwd: str | None = None,
    envs: Mapping[str, str] | None = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> int:
    """Abre la PTY (`timeout=None`), reenvía la entrada y devuelve el código
    de salida del shell: el `exit_code` de un `CommandExitException` y 124
    si el agente la cierra por timeout."""
    source = sys.stdin if stdin is None else stdin
    sink = sys.stdout if stdout is None else stdout
    handle = sandbox.pty.create(
        size=initial_size(source),
        user=user,
        cwd=cwd,
        envs=envs,
        on_data=output_writer(sink),
        timeout=None,
    )
    restore = start_input(source, handle)
    try:
        return exit_code_of(handle)
    finally:
        restore()


def initial_size(stdin: IO[str]) -> PtySize:
    if not is_terminal(stdin):
        return DEFAULT_SIZE
    return current_terminal_size()


def current_terminal_size() -> PtySize:
    columns, lines = shutil.get_terminal_size((DEFAULT_SIZE.cols, DEFAULT_SIZE.rows))
    return PtySize(cols=max(1, columns), rows=max(1, lines))


def is_terminal(stream: IO[str]) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def output_writer(stdout: IO[str]) -> Callable[[bytes], None]:
    binary = getattr(stdout, "buffer", None)

    def write(data: bytes) -> None:
        if binary is not None:
            binary.write(data)
            binary.flush()
            return
        stdout.write(data.decode("utf-8", errors="replace"))
        stdout.flush()

    return write


def exit_code_of(handle: PtyHandle) -> int:
    try:
        return handle.wait().exit_code
    except CommandExitException as exc:
        return exc.exit_code
    except TimeoutException:
        return EXIT_TIMEOUT


def start_input(stdin: IO[str], handle: PtyHandle) -> Callable[[], None]:
    """Arranca el lector del modo que toque y devuelve cómo deshacer lo que
    haya cambiado en la terminal local."""
    send = input_sender(handle)
    if is_terminal(stdin) and not IS_WINDOWS:
        return start_posix_terminal(stdin, handle, send)
    if is_terminal(stdin):
        start_daemon(lambda: pump_windows_console(handle, send))
        return nothing_to_restore
    start_daemon(lambda: pump_pipe(stdin, send))
    return nothing_to_restore


def nothing_to_restore() -> None:
    return None


def input_sender(handle: PtyHandle) -> Sender:
    """`send_input` que devuelve en silencio si la PTY ya terminó: el lector
    puede ir por detrás del `exit`."""

    def send(data: bytes) -> None:
        try:
            handle.send_input(data)
        except NotFoundException:
            return

    return send


def start_daemon(target: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=guarded(target), name="rayito-terminal-input", daemon=True)
    thread.start()
    return thread


def guarded(target: Callable[[], None]) -> Callable[[], None]:
    """Un lector que falla (PTY cerrada, sandbox caído) sólo deja de leer: el
    resultado lo da `wait()` en el hilo principal."""

    def run() -> None:
        try:
            target()
        except (SandboxException, OSError, ValueError):
            return

    return run


def pump_pipe(stdin: IO[str], send: Sender) -> None:
    reader = binary_reader(stdin)
    while True:
        chunk = reader(READ_CHUNK_BYTES)
        if not chunk:
            send(EOF_BYTE)
            return
        send(chunk)


def binary_reader(stdin: IO[str]) -> Callable[[int], bytes]:
    binary = getattr(stdin, "buffer", None)
    if binary is None:
        return lambda size: stdin.read(size).encode("utf-8")
    read1 = getattr(binary, "read1", None)
    return cast("Callable[[int], bytes]", read1 if callable(read1) else binary.read)


def translate_windows_key(prefix: str, code: str) -> str | None:
    """La segunda mitad de una tecla extendida de `getwch` en su secuencia
    VT; `None` si no tiene traducción."""
    if prefix not in WINDOWS_EXTENDED_PREFIXES:
        return None
    return WINDOWS_EXTENDED_KEYS.get(code)


def pump_windows_console(handle: PtyHandle, send: Sender) -> None:
    msvcrt = importlib.import_module("msvcrt")
    size = current_terminal_size()
    next_size_check = time.monotonic() + RESIZE_POLL_SECONDS
    while True:
        if msvcrt.kbhit():
            character = msvcrt.getwch()
            if character in WINDOWS_EXTENDED_PREFIXES:
                sequence = translate_windows_key(character, msvcrt.getwch())
                if sequence is not None:
                    send(sequence.encode("utf-8"))
                continue
            send(character.encode("utf-8"))
            continue
        if time.monotonic() >= next_size_check:
            size = resize_if_changed(handle, size)
            next_size_check = time.monotonic() + RESIZE_POLL_SECONDS
        time.sleep(KEY_POLL_SECONDS)


def resize_if_changed(handle: PtyHandle, previous: PtySize) -> PtySize:
    size = current_terminal_size()
    if size != previous:
        handle.resize(size)
    return size


def start_posix_terminal(stdin: IO[str], handle: PtyHandle, send: Sender) -> Callable[[], None]:
    """Modo raw y `SIGWINCH`; lo devuelto restaura los atributos guardados y
    el manejador anterior."""
    termios = importlib.import_module("termios")
    tty = importlib.import_module("tty")
    descriptor = stdin.fileno()
    saved = termios.tcgetattr(descriptor)
    tty.setraw(descriptor)
    restore_handler = install_resize_handler(handle)
    stop = threading.Event()
    reader = start_daemon(lambda: pump_descriptor(descriptor, send, os.read, stop))

    def restore() -> None:
        stop.set()
        reader.join(READER_STOP_SECONDS)
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
        restore_handler()

    return restore


def install_resize_handler(handle: PtyHandle) -> Callable[[], None]:
    """`SIGWINCH` lanza el `resize` en un hilo aparte (un RPC dentro del
    manejador bloquearía el hilo principal). Sólo el hilo principal puede
    instalar manejadores: en otro hilo, o sin `SIGWINCH`, no se instala
    nada. Lo devuelto reinstala el manejador anterior."""
    resize_signal = getattr(signal, "SIGWINCH", None)
    if resize_signal is None or threading.current_thread() is not threading.main_thread():
        return nothing_to_restore

    def on_resize(signum: int, frame: Any) -> None:
        start_daemon(lambda: handle.resize(current_terminal_size()))

    previous = signal.signal(resize_signal, on_resize)

    def reinstall() -> None:
        signal.signal(resize_signal, previous)

    return reinstall


def pump_descriptor(
    descriptor: int,
    send: Sender,
    read: Callable[[int, int], bytes],
    stop: threading.Event,
) -> None:
    """Lee la terminal hasta EOF o hasta `stop`: espera con `select` en
    tramos cortos, así al volver `run_terminal` ningún hilo sigue
    consumiendo la entrada local (y cerrar el descriptor no queda a la
    espera de un `read` bloqueado, como pasa con las PTY de macOS)."""
    while not stop.is_set():
        ready, _, _ = select.select([descriptor], [], [], READER_POLL_SECONDS)
        if not ready or stop.is_set():
            continue
        chunk = read(descriptor, READ_CHUNK_BYTES)
        if not chunk:
            send(EOF_BYTE)
            return
        send(chunk)


__all__ = ["pump_descriptor", "run_terminal", "translate_windows_key"]
