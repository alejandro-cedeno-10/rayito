"""Pone `infra/lambdas/quota_alerts/` en `sys.path` para importar
`quota_alert_check` y `handler` como lo hace el runtime de Lambda (la raíz del
zip). Cada test lo importa primero en vez de usar un `conftest.py`, como las
otras pruebas de Lambda del repositorio (un `conftest` homónimo se pisaría en
la misma sesión de pytest)."""

from __future__ import annotations

import sys
from pathlib import Path

LAMBDA_ROOT = Path(__file__).resolve().parent.parent
TESTS_ROOT = Path(__file__).resolve().parent
for entry in (LAMBDA_ROOT, TESTS_ROOT):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
