"""Lo que botocore escribe a nivel DEBUG y Rayito no deja salir.

Con el logger raíz a DEBUG (o `boto3.set_stream_logger()`), botocore escribe
el cuerpo de cada petición y de cada respuesta: `botocore.endpoint` («Making
request for … with params», «Sending http request») y `botocore.parsers`
(«Response body»). Ahí van el valor de un secreto de Secrets Manager
(`PutSecretValue`, `GetSecretValue`), el token de acceso de un sandbox
(`runHookPayload`) o las variables de entorno de `CreateMicrovm`. Medido con
un servidor HTTP local y boto3 (2026-10-23): esos dos loggers son los únicos
que llevan el cuerpo; `botocore.auth` se añade porque su «CanonicalRequest»
lleva las cabeceras firmadas, entre ellas el token de sesión.

`import rayito` instala `BOTOCORE_BODY_FILTER` en esos loggers: descarta sus
registros por debajo de INFO y deja pasar los demás (avisos y errores de
botocore siguen saliendo, y el resto de loggers de botocore no cambia).
Puro: sólo la biblioteca estándar.
"""

from __future__ import annotations

import logging
from typing import Final

#: Los loggers de botocore cuyo DEBUG lleva cuerpos o cabeceras firmadas.
BOTOCORE_BODY_LOGGERS: Final = ("botocore.endpoint", "botocore.parsers", "botocore.auth")


class BotocoreBodyFilter(logging.Filter):
    """Deja pasar sólo los registros de INFO en adelante."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.INFO


BOTOCORE_BODY_FILTER: Final = BotocoreBodyFilter()


def drop_botocore_body_logs() -> None:
    """Instala `BOTOCORE_BODY_FILTER` en cada logger de
    `BOTOCORE_BODY_LOGGERS`; llamarla otra vez no añade otro."""
    for name in BOTOCORE_BODY_LOGGERS:
        logger = logging.getLogger(name)
        if BOTOCORE_BODY_FILTER not in logger.filters:
            logger.addFilter(BOTOCORE_BODY_FILTER)
