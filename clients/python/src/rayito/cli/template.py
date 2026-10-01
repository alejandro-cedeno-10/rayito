"""Stub de `rayito template` para `m15-templates` (M15 foundations). La
feature sustituye esto por los comandos reales (`build`, `status`, `logs`)
en su propio cambio; el registro en `cli/app.py` no cambia para eso.
"""

from __future__ import annotations

import typer

template_app = typer.Typer(no_args_is_help=True, help="Pendiente de m15-templates.")


@template_app.callback()
def _pending() -> None:
    """Templates declarativos (Template.build): pendiente de m15-templates."""
