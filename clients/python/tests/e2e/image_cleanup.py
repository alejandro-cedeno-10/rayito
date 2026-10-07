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

Después borra el grupo de logs de la imagen (`/rayito/<nombre>`,
`LOG_GROUP_PREFIX`), que crea el rol de build en el primer build y que
`delete-microvm-image` no toca: sin esto cada corrida deja un
`/rayito/rayito-m15-templates-e2e-*` en CloudWatch. `ResourceNotFoundException`
(el build nunca llegó a escribir logs) no es un fallo; `ThrottlingException`
y `OperationAbortedException` (otra operación sobre el mismo grupo) se
reintentan con la misma espera; cualquier otro código queda en la lista de
fallos como `nombre (logs): código`.

Necesita `lambda:DeleteMicrovmImage` y `logs:DeleteLogGroup` sobre
`/rayito/*` (la identidad que corre la aceptación; `CallerPolicy` no los
concede a propósito). El reloj y la espera se inyectan
para probar la lógica sin AWS (`tests/unit/test_e2e_image_cleanup.py`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import ClientError

from rayito._images import LOG_GROUP_PREFIX

NOT_FOUND = "ResourceNotFoundException"
RETRYABLE_CODES = frozenset({"ConflictException", "ThrottlingException"})
LOG_GROUP_RETRYABLE_CODES = frozenset({"ThrottlingException", "OperationAbortedException"})
RETRY_BACKOFF_SECONDS = (5.0, 10.0, 20.0, 40.0, 80.0)
POLL_INTERVAL_SECONDS = 5.0
DEFAULT_TIMEOUT_SECONDS = 600.0
DELETED_STATE = "DELETED"
DELETE_FAILED_STATE = "DELETE_FAILED"


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", "Unknown"))


def log_group_of(name: str) -> str:
    """El grupo de logs que el build de la imagen `name` escribe."""
    return f"{LOG_GROUP_PREFIX}/{name}"


@dataclass
class BuiltImages:
    """Los nombres de imagen que construye una corrida y su borrado."""

    microvms: Any
    resolve_arn: Callable[[str], str]
    logs: Any
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
            log_reason = self.delete_log_group(log_group_of(name))
            if log_reason is not None:
                failures.append(f"{name} (logs): {log_reason}")
        return failures

    def delete(self, arn: str) -> str | None:
        """`None` cuando la imagen ya no existe; si no, el motivo."""
        code = self._call_with_backoff(
            lambda: self.microvms.delete_microvm_image(imageIdentifier=arn), RETRYABLE_CODES
        )
        if code is None:
            return self.wait_until_gone(arn)
        return None if code == NOT_FOUND else code

    def delete_log_group(self, log_group: str) -> str | None:
        """`None` cuando el grupo ya no existe; si no, el código de AWS."""
        code = self._call_with_backoff(
            lambda: self.logs.delete_log_group(logGroupName=log_group), LOG_GROUP_RETRYABLE_CODES
        )
        return None if code in (None, NOT_FOUND) else code

    def _call_with_backoff(
        self, call: Callable[[], object], retryable: frozenset[str]
    ) -> str | None:
        """Repite `call` con `RETRY_BACKOFF_SECONDS` mientras falle con un
        código de `retryable`; `None` si acabó bien, si no el último código
        (`ResourceNotFoundException` incluido, que nunca se reintenta)."""
        retries = iter(RETRY_BACKOFF_SECONDS)
        while True:
            try:
                call()
            except ClientError as exc:
                code = _code(exc)
                backoff = next(retries, None)
                if code not in retryable or backoff is None:
                    return code
                self.sleep(backoff)
                continue
            return None

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
