"""Puerto `KeyValueStoreWriter` y su adaptador boto3 real
(`CloudFrontKvsWriter`), para `m15-custom-domain` (ADR-024).

Verificado offline contra botocore 1.43.103, modelo
`cloudfront-keyvaluestore/2022-07-26` (`AWS_API_NOTES.md` §29):
`DescribeKeyValueStore(KvsARN) -> ETag`, `PutKey(KvsARN, Key, Value,
IfMatch) -> ETag` y `DeleteKey(KvsARN, Key, IfMatch) -> ETag` son
optimistic-concurrency sobre un `ETag` que cada llamada encadena a la
siguiente (la respuesta de un `PutKey` ya trae el `ETag` que pide el
siguiente, sin otro `DescribeKeyValueStore`).

**`service-2.json` declara `signatureVersion: v4`, pero eso no es lo que de
verdad firma la petición**: `endpoint-rule-set-1.json` de este servicio fija
`authSchemes: [{"name": "sigv4a", ...}]` en sus reglas de endpoint, y es la
resolución de endpoint la que elige el firmante, no `metadata.
signatureVersion` (una corrección anterior en este mismo fichero asumía lo
contrario; revertida tras comprobar botocore 1.43.103 con credenciales
ficticias y un hook `before-send`: sin `awscrt` instalado, `boto3`
`describe_key_value_store` lanza `MissingDependencyException` — "This
operation requires an additional dependency. Use pip install
botocore[crt]"). Por eso el plano de datos de este servicio sí necesita
SigV4A: Python instala `awscrt` como el extra opcional `rayito[custom-domain]`
(`pyproject.toml`); sin él, cualquier llamada falla con un
`CustomDomainException` que nombra ese extra (`_wrap`, más abajo), nunca con
el `MissingDependencyException` crudo de botocore.
"""

from __future__ import annotations

from typing import Protocol

import boto3

from rayito._aws import LazyClient
from rayito._aws import aws_code as _aws_code
from rayito._aws_sanitize import sanitize_aws_error
from rayito.exceptions import CustomDomainException

#: `ResourceNotFoundException` de `DeleteKey`/`PutKey` (botocore 1.43.103,
#: modelo `cloudfront-keyvaluestore`): el almacén o la clave no existen.
#: `CustomDomain.unregister` la usa para ser idempotente (borrar una ruta
#: que ya no está no es un error).
RESOURCE_NOT_FOUND_CODE = "ResourceNotFoundException"

#: El nombre de clase que botocore usa para "falta una dependencia opcional"
#: (aquí, `awscrt` para firmar SigV4A); no trae `.response`, así que
#: `aws_code()` no la reconoce y hay que mirar `type(exc).__name__` a mano
#: (verificado offline contra botocore 1.43.103, AWS_API_NOTES.md §29).
_MISSING_CRT_DEPENDENCY_EXCEPTION = "MissingDependencyException"

#: El extra de `pyproject.toml` que trae `awscrt`; nombrado en el mensaje de
#: `_wrap` cuando falta, para que instalarlo sea un solo comando.
_CUSTOM_DOMAIN_EXTRA = "rayito[custom-domain]"


class KeyValueStoreWriter(Protocol):
    """Lo que `CustomDomain` necesita del KeyValueStore de CloudFront, sin
    saber si detrás hay boto3 de verdad o un fake de test."""

    def describe(self, kvs_arn: str) -> str:
        """El `ETag` vigente del almacén."""
        ...

    def put(self, kvs_arn: str, key: str, value: str, *, if_match: str) -> str:
        """Escribe `key`; devuelve el `ETag` nuevo, encadenable al siguiente `put`/`delete`."""
        ...

    def delete(self, kvs_arn: str, key: str, *, if_match: str) -> str:
        """Idempotente: borrar una clave que ya no está no es un error."""
        ...


def _wrap(exc: Exception) -> CustomDomainException:
    if type(exc).__name__ == _MISSING_CRT_DEPENDENCY_EXCEPTION:
        return CustomDomainException(
            f"CustomDomain (domain=/register()/unregister()/refresh()) necesita el paquete "
            f"opcional 'awscrt' para firmar SigV4A: instala pip install '{_CUSTOM_DOMAIN_EXTRA}'",
            status_code=None,
            grpc_code=None,
            aws_code=None,
        )
    return CustomDomainException(
        str(sanitize_aws_error(exc)),
        status_code=None,
        grpc_code=None,
        aws_code=_aws_code(exc),
    )


class CloudFrontKvsWriter:
    """Adaptador real; construirlo no hace ninguna llamada a AWS (cliente
    perezoso, `LazyClient`)."""

    def __init__(
        self, *, region: str | None = None, session: boto3.session.Session | None = None
    ) -> None:
        self._client = LazyClient("cloudfront-keyvaluestore", region=region, session=session)

    def describe(self, kvs_arn: str) -> str:
        try:
            response = self._client.get().describe_key_value_store(KvsARN=kvs_arn)
        except Exception as exc:
            raise _wrap(exc) from exc
        return str(response["ETag"])

    def put(self, kvs_arn: str, key: str, value: str, *, if_match: str) -> str:
        try:
            response = self._client.get().put_key(
                KvsARN=kvs_arn, Key=key, Value=value, IfMatch=if_match
            )
        except Exception as exc:
            raise _wrap(exc) from exc
        return str(response["ETag"])

    def delete(self, kvs_arn: str, key: str, *, if_match: str) -> str:
        try:
            response = self._client.get().delete_key(KvsARN=kvs_arn, Key=key, IfMatch=if_match)
        except Exception as exc:
            raise _wrap(exc) from exc
        return str(response["ETag"])
