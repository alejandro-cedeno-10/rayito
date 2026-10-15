"""Pone `infra/lambdas/dashboard/` en `sys.path` para importar `dashboard_stack`
como lo hace el runtime de Lambda (la raíz del zip). Cada test lo importa
primero (`import _stack_path`) en vez de usar un `conftest.py`: las pruebas de
`events_webhooks` hacen `from conftest import ...` y, ejecutadas en la misma
sesión de pytest, otro `conftest` con el mismo nombre se lo pisaría."""

from __future__ import annotations

import sys
from pathlib import Path

LAMBDA_ROOT = Path(__file__).resolve().parent.parent
if str(LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(LAMBDA_ROOT))
