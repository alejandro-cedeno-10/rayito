"""Puerto `KeyValueStoreWriter` y su adaptador boto3 real
(`CloudFrontKvsWriter`), para `m15-custom-domain` (ADR-024).

Verificado offline contra botocore 1.43.103, modelo
`cloudfront-keyvaluestore/2022-07-26` (`AWS_API_NOTES.md` §29):
`DescribeKeyValueStore(KvsARN) -> ETag`, `PutKey(KvsARN, Key, Value,
IfMatch) -> ETag` y `DeleteKey(KvsARN, Key, IfMatch) -> ETag` son
optimistic-concurrency sobre un `ETag` que cada llamada encadena a la
siguiente (la respuesta de un `PutKey` ya trae el `ETag` que pide el
siguiente, sin otro `DescribeKeyValueStore`). El modelo declara
`signatureVersion: v4`: a pesar de lo que suponía la investigación previa
(`docs/research/2026-10-e2b-out-of-scope.md` §8), este servicio **no**
necesita SigV4A ni el peer `awscrt`/`@aws-sdk/signature-v4a` — una
corrección documentada en `AWS_API_NOTES.md` §29.
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
