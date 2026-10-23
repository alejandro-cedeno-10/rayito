"""Lo que el SDK de AWS para Python escribe a nivel DEBUG y Rayito no deja
salir.

Con el logger raíz a DEBUG (o `boto3.set_stream_logger()`), botocore, boto3
y s3transfer escriben los parámetros y el cuerpo de cada petición y de cada
respuesta. Ahí van el valor de un secreto de Secrets Manager
(`PutSecretValue`, `GetSecretValue`), el token de acceso de un sandbox
(`runHookPayload`), las variables de entorno de `CreateMicrovm` o el
`Item` de un `PutItem` (la URL de un webhook y el nombre de su secreto).

Medido con un servidor HTTP local y boto3 1.43.102, botocore 1.43.103 y
s3transfer 0.19.2 (2026-10-23), con llamadas de cliente, de recurso
(`Table.put_item`), de colección (`objects.filter`), `batch_writer` y
`upload_fileobj`, y un valor testigo en cuerpos, `Item`, `Metadata`, claves
y prefijos: lo escriben `botocore.endpoint` («Making request … with
params»), `botocore.parsers` («Response body»), `botocore.regions` (los
parámetros del endpoint), `boto3.resources.action` y
`boto3.resources.collection` («Calling … with {…}»), `boto3.dynamodb.table`
(los `PutRequest` que `batch_writer` descarta) y `s3transfer.tasks` y
`s3transfer.futures` (los `extra_args`). `botocore.auth` se añade porque su
«CanonicalRequest» lleva las cabeceras firmadas, entre ellas el token de
sesión. Fuera queda `urllib3.connectionpool`, que no es del SDK de AWS:
su DEBUG lleva la línea de petición (método, ruta y consulta, con el bucket
y la clave de S3), nunca un cuerpo.

`import rayito` instala `BOTOCORE_BODY_FILTER` en esos loggers: descarta sus
registros por debajo de INFO y deja pasar los demás (los avisos y errores
siguen saliendo, y el resto de loggers no cambia). Puro: sólo la biblioteca
estándar.
"""

from __future__ import annotations

import logging
from typing import Final

#: Los loggers del SDK de AWS para Python cuyo DEBUG lleva parámetros,
#: cuerpos o cabeceras firmadas (la medición del docstring del módulo).
BOTOCORE_BODY_LOGGERS: Final = (
    "botocore.endpoint",
    "botocore.parsers",
    "botocore.auth",
    "botocore.regions",
    "boto3.resources.action",
    "boto3.resources.collection",
    "boto3.dynamodb.table",
    "s3transfer.tasks",
    "s3transfer.futures",
)


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
