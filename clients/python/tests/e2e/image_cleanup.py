"""Borra las imágenes MicroVM que construye un e2e (`test_m15_templates.py`).

Cada `Template.build()` crea una imagen nueva con su versión (también cuando
el build falla: la imagen y su versión `FAILED` quedan en la cuenta). Sin
borrarlas, cada corrida deja imágenes huérfanas con su almacenamiento de
snapshot. `BuiltImages` apunta cada nombre **antes** del build y, al cerrar
el fixture (pase o falle el test), borra cada imagen con
`delete-microvm-image`, que acepta una imagen con versiones y la pasa a
`DELETING` en el acto (`AWS_API_NOTES.md` §4), y espera a que desaparezca:

- `ResourceNotFoundException` al borrar o al consultar: ya no existe (el
  build falló antes de crearla, o el borrado terminó);
- `ConflictException`/`ThrottlingException`: la imagen aún está
  `UPDATING` (un build que acaba de fallar) o hay limitación de tasa; se
  reintenta con espera creciente;
- `DELETE_FAILED` o el plazo agotado: queda en la lista de fallos, que el
  fixture convierte en error del test con los nombres para borrarlos a mano.

Necesita `lambda:DeleteMicrovmImage` (la identidad que corre la aceptación;
`CallerPolicy` no la concede a propósito). El reloj y la espera se inyectan
para probar la lógica sin AWS (`tests/unit/test_e2e_image_cleanup.py`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import ClientError

NOT_FOUND = "ResourceNotFoundException"
RETRYABLE_CODES = frozenset({"ConflictException", "ThrottlingException"})
RETRY_BACKOFF_SECONDS = (5.0, 10.0, 20.0, 40.0, 80.0)
POLL_INTERVAL_SECONDS = 5.0
DEFAULT_TIMEOUT_SECONDS = 600.0
DELETED_STATE = "DELETED"
DELETE_FAILED_STATE = "DELETE_FAILED"


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", "Unknown"))


@dataclass
class BuiltImages:
    """Los nombres de imagen que construye una corrida y su borrado."""

    microvms: Any
    resolve_arn: Callable[[str], str]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    names: list[str] = field(default_factory=list)

    def track(self, name: str) -> str:
        """Apunta `name` para borrarlo al final; se llama antes del build."""
        self.names.append(name)
        return name

    def delete_all(self) -> list[str]:
        """Borra y olvida cada imagen apuntada; devuelve `nombre: motivo` de
        las que no se pudieron borrar (vacío si todo quedó limpio)."""
        failures = []
        names, self.names = self.names, []
        for name in names:
            reason = self.delete(self.resolve_arn(name))
            if reason is not None:
                failures.append(f"{name}: {reason}")
        return failures

    def delete(self, arn: str) -> str | None:
        """`None` cuando la imagen ya no existe; si no, el motivo."""
        retries = iter(RETRY_BACKOFF_SECONDS)
        while True:
            try:
                self.microvms.delete_microvm_image(imageIdentifier=arn)
            except ClientError as exc:
                code = _code(exc)
                if code == NOT_FOUND:
                    return None
                backoff = next(retries, None)
                if code not in RETRYABLE_CODES or backoff is None:
                    return code
                self.sleep(backoff)
                continue
            return self.wait_until_gone(arn)

    def wait_until_gone(self, arn: str) -> str | None:
        started = self.clock()
        while True:
            try:
                state = str(self.microvms.get_microvm_image(imageIdentifier=arn)["state"])
            except ClientError as exc:
                if _code(exc) == NOT_FOUND:
                    return None
                return _code(exc)
            if state == DELETED_STATE:
                return None
            if state == DELETE_FAILED_STATE:
                return state
            if self.clock() - started >= self.timeout:
                return f"sigue {state} tras {self.timeout:.0f} s"
            self.sleep(POLL_INTERVAL_SECONDS)
