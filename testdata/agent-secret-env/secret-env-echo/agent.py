"""Agente de eco de `secret_env` (ADR-033 D4), sin modelo ni red: dice qué
variables recibió y un resumen de 8 caracteres del SHA-256 de cada valor, para
comprobar en la aceptación que los secretos llegaron sin imprimirlos nunca.

Lee el entorno al importarse: si el host no los hubiera puesto antes de cargar
este módulo, `present` saldría vacío.
"""

import hashlib
import json
import os

EXPECTED = ("DATABASE_URL", "TICKETS_TOKEN", "APP_DB_HOST")
DIGEST_CHARS = 8
AT_IMPORT = {name: os.environ.get(name) for name in EXPECTED}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:DIGEST_CHARS]


def echo(request: dict) -> dict:
    present = [name for name, value in AT_IMPORT.items() if value is not None]
    digests = {name: _digest(AT_IMPORT[name]) for name in present}
    return {"text": json.dumps({"present": present, "sha256_8": digests}, sort_keys=True)}
