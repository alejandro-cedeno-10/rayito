"""El punto de entrada de la Function URL (`Handler: handler.lambda_handler`
en `infra/dashboard.yaml`). La aplicación se construye en la primera
invocación de cada entorno de ejecución y se reutiliza después."""

from __future__ import annotations

import os
from functools import cache
from typing import Any

from dashboard_stack.app import StackApp
from dashboard_stack.wiring import build_app


@cache
def application() -> StackApp:
    return build_app(os.environ)


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    return application().respond(event)
