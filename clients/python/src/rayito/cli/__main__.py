"""Punto de entrada `rayito` (`[project.scripts]`) y `python -m rayito.cli`."""

from __future__ import annotations

import sys

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
    return 0


def exit_status(code: object) -> int:
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
