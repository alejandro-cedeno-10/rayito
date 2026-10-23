"""Punto de entrada `rayito` (`[project.scripts]`) y `python -m rayito.cli`."""

from __future__ import annotations

import os
import sys

#: El código de salida cuando el lector cierra la salida (`| head`): el
#: mismo `exit 1` sin traza que Click da a un `BrokenPipeError` dentro de un
#: comando, y el de la CLI de TypeScript (`closed-pipe.ts`).
CLOSED_PIPE_EXIT_CODE = 1

MISSING_EXTRA_MESSAGE = (
    'rayito: la CLI necesita el extra: uv pip install "rayito[cli]" (o pip install "rayito[cli]")'
)


def missing_typer(exc: ModuleNotFoundError) -> bool:
    return exc.name is not None and (exc.name == "typer" or exc.name.startswith("typer."))


def main() -> int:
    try:
        from rayito.cli.app import app
    except ModuleNotFoundError as exc:
        if not missing_typer(exc):
            raise
        print(MISSING_EXTRA_MESSAGE, file=sys.stderr)
        return 2
    try:
        app(prog_name="rayito")
    except SystemExit as exc:
        return exit_status(exc.code)
    except BrokenPipeError:
        return closed_pipe()
    return 0


def closed_pipe() -> int:
    """El lector cerró la salida: `stdout` pasa a `/dev/null` para que el
    vaciado del intérprete al salir no escriba otra traza
    (`Exception ignored ... BrokenPipeError`), y se sale en silencio."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, sys.stdout.fileno())
    os.close(devnull)
    return CLOSED_PIPE_EXIT_CODE


def exit_status(code: object) -> int:
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
