"""The kernel catalog: one ``KernelLanguage`` per wire name (``python``,
``bash``, ``javascript``) with the kernelspec it starts and the silent probe
cell that proves the kernel answers before the context is ``ready``.

The probe cells differ per language because they are executed by the kernel
itself, not by the sidecar: ``pass`` is a Python no-op, ``:`` is the shell
builtin that does nothing, ``void 0`` is the JavaScript expression that
evaluates to ``undefined`` (so ijavascript prints nothing). Which of these
kernels an image actually ships is decided by the filesystem at start
(``kernels.install_kernelspecs``), never by this table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

PYTHON: Final = "python"


@dataclass(frozen=True)
class KernelLanguage:
    name: str
    kernel_name: str
    probe_cell: str


LANGUAGES: Final[dict[str, KernelLanguage]] = {
    PYTHON: KernelLanguage(name=PYTHON, kernel_name="rayito", probe_cell="pass"),
    "bash": KernelLanguage(name="bash", kernel_name="rayito-bash", probe_cell=":"),
    "javascript": KernelLanguage(
        name="javascript", kernel_name="rayito-javascript", probe_cell="void 0"
    ),
}


def language_for(name: str) -> KernelLanguage | None:
    return LANGUAGES.get(name)
