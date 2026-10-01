"""Stub de `rayito domain` para `m15-custom-domain` (M15 foundations). La
feature sustituye esto por los comandos reales en su propio cambio; el
registro en `cli/app.py` no cambia para eso.
"""

from __future__ import annotations

import typer

domain_app = typer.Typer(no_args_is_help=True, help="Pendiente de m15-custom-domain.")


@domain_app.callback()
def _pending() -> None:
    """Dominio propio (domain=): pendiente de m15-custom-domain."""
