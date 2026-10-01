"""`KeyValueStoreWriter` falso en memoria, para `m15-custom-domain`
(ADR-024): un `ETag` por almacén que avanza en cada escritura, igual que
el CloudFront KeyValueStore real (optimistic concurrency), y
`ResourceNotFoundException` (vía `CustomDomainException.aws_code`) sobre un
almacén o una clave que no existen — exactamente lo que
`CustomDomain.unregister()` necesita para ser idempotente."""

from __future__ import annotations

from dataclasses import dataclass, field

from rayito._custom_domain._kvs import RESOURCE_NOT_FOUND_CODE
from rayito.exceptions import CustomDomainException


@dataclass
class FakeKeyValueStoreWriter:
    #: `kvs_arn -> {key: value}`; un almacén ausente es "no desplegado".
    stores: dict[str, dict[str, str]] = field(default_factory=dict)
    #: `kvs_arn -> ETag` (un entero creciente como cadena).
    etags: dict[str, int] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def _require_store(self, kvs_arn: str) -> dict[str, str]:
        if kvs_arn not in self.stores:
            raise CustomDomainException("almacén no encontrado", aws_code=RESOURCE_NOT_FOUND_CODE)
        return self.stores[kvs_arn]

    def _bump(self, kvs_arn: str) -> str:
        self.etags[kvs_arn] = self.etags.get(kvs_arn, 0) + 1
        return str(self.etags[kvs_arn])

    def seed(self, kvs_arn: str) -> None:
        """Crea el almacén vacío (como si `deploy()` ya hubiera corrido)."""
        self.stores.setdefault(kvs_arn, {})
        self.etags.setdefault(kvs_arn, 0)

    def describe(self, kvs_arn: str) -> str:
        self.calls.append(("describe", kvs_arn))
        self._require_store(kvs_arn)
        return str(self.etags.get(kvs_arn, 0))

    def put(self, kvs_arn: str, key: str, value: str, *, if_match: str) -> str:
        self.calls.append(("put", kvs_arn, key))
        store = self._require_store(kvs_arn)
        if str(self.etags.get(kvs_arn, 0)) != if_match:
            raise CustomDomainException("ETag no coincide", aws_code="ConflictException")
        store[key] = value
        return self._bump(kvs_arn)

    def delete(self, kvs_arn: str, key: str, *, if_match: str) -> str:
        self.calls.append(("delete", kvs_arn, key))
        store = self._require_store(kvs_arn)
        if str(self.etags.get(kvs_arn, 0)) != if_match:
            raise CustomDomainException("ETag no coincide", aws_code="ConflictException")
        if key not in store:
            raise CustomDomainException("clave no encontrada", aws_code=RESOURCE_NOT_FOUND_CODE)
        del store[key]
        return self._bump(kvs_arn)
