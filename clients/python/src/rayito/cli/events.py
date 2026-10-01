"""Stub de `rayito events` para `m15-events-webhooks` (M15 foundations). La
feature sustituye esto por los comandos reales (`deploy`, `status`,
`destroy`, `webhook add|list|remove`, `list`) en su propio cambio; el
registro en `cli/app.py` no cambia para eso.
"""

from __future__ import annotations

import typer

events_app = typer.Typer(no_args_is_help=True, help="Pendiente de m15-events-webhooks.")


@events_app.callback()
def _pending() -> None:
    """Eventos de ciclo de vida y webhooks (events=): pendiente de m15-events-webhooks."""
