"""The kernel catalog: one ``KernelLanguage`` per wire name (``python``,
``bash``, ``javascript``, ``typescript``) with the kernelspec it starts, the
silent probe cell that proves the kernel answers before the context is
``ready``, the transport its sockets bind and whether its ``text/plain``
results are cleaned of ANSI escapes.

The probe cells differ per language because they are executed by the kernel
itself, not by the sidecar: ``pass`` is a Python no-op, ``:`` is the shell
builtin that does nothing, ``void 0`` is the JavaScript expression that
evaluates to ``undefined``; the probe runs ``silent=True``, so it produces
no output either way.

``javascript`` and ``typescript`` are served by the Jupyter kernel built into
Deno (AWS_API_NOTES.md Q61), which replaced ``ijavascript``: its ``zeromq``
binding needs a compiler the al2023 ARM64 builder does not have (Q57). Deno
implements ZeroMQ itself on ``Deno.listen()`` and has no ``ipc`` transport,
so its contexts bind loopback TCP instead of the ``ipc`` sockets every other
kernel uses (ADR-013); flipping ``transport`` back is all a future Deno with
``ipc`` needs. Its REPL inspector colours results (Q61), hence the
``text/plain`` strip next to the kernelspec's ``NO_COLOR``.

Which of these kernels an image actually ships is decided by the filesystem
at start (``kernels.install_kernelspecs``), never by this table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

PYTHON: Final = "python"

Transport = Literal["ipc", "tcp"]


@dataclass(frozen=True)
class KernelLanguage:
    name: str
    kernel_name: str
    probe_cell: str
    transport: Transport = "ipc"
    strips_plain_text_ansi: bool = False


LANGUAGES: Final[dict[str, KernelLanguage]] = {
    PYTHON: KernelLanguage(name=PYTHON, kernel_name="rayito", probe_cell="pass"),
    "bash": KernelLanguage(name="bash", kernel_name="rayito-bash", probe_cell=":"),
    "javascript": KernelLanguage(
        name="javascript",
        kernel_name="rayito-javascript",
        probe_cell="void 0",
        transport="tcp",
        strips_plain_text_ansi=True,
    ),
    "typescript": KernelLanguage(
        name="typescript",
        kernel_name="rayito-typescript",
        probe_cell="void 0",
        transport="tcp",
        strips_plain_text_ansi=True,
    ),
}


def language_for(name: str) -> KernelLanguage | None:
    return LANGUAGES.get(name)
