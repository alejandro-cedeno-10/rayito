"""`e2b.Secret`/`e2b.AsyncSecret` (E2B 2.51.0, `e2b/secret/`) sobre
`SecretStore`: el CRUD de E2B contra AWS Secrets Manager en tu cuenta.

Mismos nombres, parámetros y resultados que E2B; las divergencias, escritas
(nunca aproximadas) en docs/site/docs/e2b-compat.md:

- `SecretInfo.secret_id` es el ARN de Secrets Manager, no `sec_…`.
- Los nombres se validan como en la API de E2B antes de llamar a AWS
  (1-128 caracteres `[A-Za-z0-9_-]`, se normalizan a minúsculas, el prefijo
  `sec_` está reservado) y se guardan como `<secret_prefix><nombre>`
  (`rayito/` por defecto). Un argumento `secret` que empieza por `arn:` se
  usa tal cual (es el `secret_id`).
- `metadata` ≤ 2048 caracteres codificados (E2B: 8 KiB); no hay tope de 100
  secretos por proyecto.
- `fill(name)` devuelve la cadena literal `${e2b.secrets.<name>}` sin llamar
  a AWS, como E2B, pero **nada la resuelve**: Rayito no tiene el inyector de
  `network.rules`, y ninguna ruta del SDK sustituye placeholders dentro de
  `envs`. La inyección de Rayito es `secrets=` del SDK nativo.
- `iam_token` sigue siendo `UnimplementedError` (motivo de `iam`).
- Los `ApiParams` de E2B (`api_key`, `domain`, `request_timeout`, …) no
  aplican a Secrets Manager: avisan con `RayitoCompatWarning` y se ignoran.
  Las opciones propias son `region`, `session`, `secret_prefix` y
  `kms_key_id`.

Coste: cada `Secret.create` empieza a facturar $0,40/mes hasta `destroy`
(ver `SecretStore` y docs/site/docs/secrets.md). Importar `rayito.e2b` no
crea ningún cliente.
"""

from __future__ import annotations

import asyncio
import re
import threading
import warnings
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar, Final, NoReturn

from rayito._secrets import DEFAULT_SECRET_PREFIX, SecretInfo, SecretPage, SecretStore
from rayito.e2b._connection import IGNORED_API_PARAMS, reject_unknown_params
from rayito.e2b._unimplemented import unimplemented
from rayito.exceptions import InvalidArgumentException, RayitoCompatWarning

E2B_NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
RESERVED_NAME_PREFIX: Final = "sec_"
INVALID_FILL_CHARS: Final = re.compile(r"[{}\x00-\x1f\x7f-\x9f]")
RAYITO_OPTIONS: Final = frozenset({"region", "session", "secret_prefix", "kms_key_id"})
NO_MORE_ITEMS: Final = "No more items to fetch"
SECRET_APPLIED_NOTHING: Final = "Secret habla con Secrets Manager con las credenciales de AWS"


def normalize_secret_name(name: str) -> str:
    """La regla de nombres de la API de E2B, antes de llamar a AWS. El error
    nunca repite el nombre (E2B: "names are confidential selectors")."""
    if not isinstance(name, str) or not E2B_NAME_PATTERN.fullmatch(name):
        raise InvalidArgumentException("nombre de secreto inválido: 1-128 caracteres [A-Za-z0-9_-]")
    normalized = name.lower()
    if normalized.startswith(RESERVED_NAME_PREFIX):
        raise InvalidArgumentException("el prefijo sec_ está reservado en los nombres de secreto")
    return normalized


def secret_selector(secret: str) -> str:
    """`secret` de E2B es "ID o nombre": un ARN (nuestro `secret_id`) va tal
    cual; un nombre se normaliza."""
    if isinstance(secret, str) and secret.startswith("arn:"):
        return secret
    return normalize_secret_name(secret)


def fill(secret: str) -> str:
    """`Secret.fill` de E2B: el placeholder literal, sin llamar a AWS."""
    if not isinstance(secret, str) or not secret or INVALID_FILL_CHARS.search(secret):
        raise InvalidArgumentException(
            "nombre de secreto no utilizable en un placeholder: no vacío y sin '{', '}' ni "
            "caracteres de control"
        )
    return f"${{e2b.secrets.{secret}}}"


_stores_lock = threading.Lock()
_stores: dict[tuple[Any, ...], SecretStore] = {}


def resolve_store(bound: Mapping[str, Any], opts: Mapping[str, Any], *, call: str) -> SecretStore:
    """El `SecretStore` de una llamada: las opciones de Rayito de la llamada
    ganan a las del cliente `E2B(...)`; los `ApiParams` de E2B avisan y se
    ignoran. Un store por combinación y proceso (un solo cliente boto3)."""
    rayito = {key: opts[key] for key in RAYITO_OPTIONS if opts.get(key) is not None}
    api_params = {key: value for key, value in opts.items() if key not in RAYITO_OPTIONS}
    reject_unknown_params(api_params, call=call)
    for name, value in api_params.items():
        if value is not None and value is not False:
            reason = IGNORED_API_PARAMS.get(name, SECRET_APPLIED_NOTHING)
            warnings.warn(f"{name} ignorado: {reason}", RayitoCompatWarning, stacklevel=4)
    merged = {**{k: v for k, v in bound.items() if k in RAYITO_OPTIONS}, **rayito}
    key = (
        merged.get("region"),
        merged.get("session"),
        merged.get("secret_prefix", DEFAULT_SECRET_PREFIX),
        merged.get("kms_key_id"),
    )
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = SecretStore(region=key[0], session=key[1], prefix=key[2], kms_key_id=key[3])
            _stores[key] = store
        return store


class _PaginatorState:
    """El estado de `PaginatorBase` de E2B: `limit`, `has_next`, `next_token`."""

    def __init__(
        self,
        store: SecretStore,
        limit: int | None = None,
        next_token: str | None = None,
    ) -> None:
        self._store = store
        self.limit = limit
        self._has_next = True
        self._next_token = next_token

    @property
    def has_next(self) -> bool:
        """True si quedan elementos por traer."""
        return self._has_next

    @property
    def next_token(self) -> str | None:
        """El token de la página siguiente."""
        return self._next_token

    def _fetch(self) -> list[SecretInfo]:
        if not self._has_next:
            raise Exception(NO_MORE_ITEMS)
        page: SecretPage = self._store.list(limit=self.limit, next_token=self._next_token)
        self._next_token = page.next_token
        self._has_next = bool(page.next_token)
        return page.items


class SecretPaginator(_PaginatorState):
    """`e2b.SecretPaginator`: `while paginator.has_next: paginator.next_items()`."""

    def next_items(self, **opts: Any) -> list[SecretInfo]:
        """La página siguiente (`ListSecrets`). Lanza si `has_next` es False."""
        return self._fetch()


class AsyncSecretPaginator(_PaginatorState):
    """`e2b.AsyncSecretPaginator`: `next_items` es una corrutina."""

    async def next_items(self, **opts: Any) -> list[SecretInfo]:
        """La página siguiente (`ListSecrets` en `asyncio.to_thread`)."""
        return await asyncio.to_thread(self._fetch)


class Secret:
    """`e2b.Secret` sobre AWS Secrets Manager (`SecretStore`). Los valores
    son de sólo escritura: ninguna lectura los devuelve.

    Coste y activación
    -------------------
    Activa: llamar a `Secret.create/update/get_info/list/exists/destroy` es la
        opción explícita; `fill` e importar el módulo no llaman a AWS.
    Recursos y llamadas AWS: un secreto de Secrets Manager por `create`
        (`CreateSecret`); `DescribeSecret`, `PutSecretValue`, `UpdateSecret`,
        `ListSecrets`, `DeleteSecret` (AWS_API_NOTES.md §19).
    Coste aproximado: $0,40 por secreto y mes hasta `destroy` + $0,05 por
        10 000 llamadas (us-east-1, 2026-09-30).
    IAM: la política `RayitoSecretsAdmin` de `infra/secrets-access.yaml`.
    Cómo apagarla: no llames al CRUD; `Secret.destroy(name)` para dejar de pagar.
    Ejemplo:
        from rayito.e2b import Secret
        info = Secret.create("openai-key", "sk-...", region="us-east-1")
        Secret.fill("openai-key")  # '${e2b.secrets.openai-key}' (no se resuelve)
        Secret.destroy("openai-key")
    """

    _bound_params: ClassVar[Mapping[str, Any]] = MappingProxyType({})

    @classmethod
    def _store(cls, opts: Mapping[str, Any], call: str) -> SecretStore:
        return resolve_store(cls._bound_params, opts, call=call)

    @classmethod
    def create(
        cls,
        name: str,
        value: str,
        metadata: dict[str, str] | None = None,
        **opts: Any,
    ) -> SecretInfo:
        """Crea el secreto con su primer valor (versión 1)."""
        return cls._store(opts, "create").create(
            normalize_secret_name(name), value, metadata=metadata
        )

    @classmethod
    def update(
        cls,
        secret: str,
        value: str,
        metadata: dict[str, str] | None = None,
        **opts: Any,
    ) -> SecretInfo:
        """Guarda `value` como versión nueva; `metadata` sustituye la guardada."""
        return cls._store(opts, "update").update(secret_selector(secret), value, metadata=metadata)

    @classmethod
    def get_info(cls, secret: str, **opts: Any) -> SecretInfo:
        """Metadatos del secreto (`SecretNotFoundException` si no existe)."""
        return cls._store(opts, "get_info").get_info(secret_selector(secret))

    @classmethod
    def list(
        cls,
        limit: int | None = None,
        next_token: str | None = None,
        **opts: Any,
    ) -> SecretPaginator:
        """Paginador sobre los secretos del prefijo."""
        return SecretPaginator(cls._store(opts, "list"), limit=limit, next_token=next_token)

    @classmethod
    def exists(cls, secret: str, **opts: Any) -> bool:
        """True si el secreto existe."""
        return cls._store(opts, "exists").exists(secret_selector(secret))

    @classmethod
    def destroy(cls, secret: str, **opts: Any) -> bool:
        """Lo borra sin ventana de recuperación: True si lo borró, False si no
        existía (como E2B; ver `SecretStore.destroy`)."""
        return cls._store(opts, "destroy").destroy(secret_selector(secret))

    @staticmethod
    def fill(secret: str) -> str:
        """`${e2b.secrets.<name>}` sin llamar a AWS. Rayito no lo resuelve."""
        return fill(secret)

    @staticmethod
    def iam_token(*, audience: str, token_type: str) -> NoReturn:
        """`UnimplementedError`: los MicroVMs no emiten tokens con audiencia."""
        raise unimplemented("iam")


class AsyncSecret:
    """`e2b.AsyncSecret`: la misma semántica que `Secret`, con corrutinas
    (boto3 en `asyncio.to_thread`). Los valores son de sólo escritura.

    Coste y activación
    -------------------
    Activa: llamar a `AsyncSecret.create/update/get_info/list/exists/destroy`
        es la opción explícita; `fill` e importar el módulo no llaman a AWS.
    Recursos y llamadas AWS: un secreto de Secrets Manager por `create`
        (`CreateSecret`); `DescribeSecret`, `PutSecretValue`, `UpdateSecret`,
        `ListSecrets`, `DeleteSecret` (AWS_API_NOTES.md §19).
    Coste aproximado: $0,40 por secreto y mes hasta `destroy` + $0,05 por
        10 000 llamadas (us-east-1, 2026-09-30).
    IAM: la política `RayitoSecretsAdmin` de `infra/secrets-access.yaml`.
    Cómo apagarla: no llames al CRUD; `await AsyncSecret.destroy(name)` para
        dejar de pagar.
    Ejemplo:
        from rayito.e2b import AsyncSecret
        info = await AsyncSecret.create("openai-key", "sk-...", region="us-east-1")
        await AsyncSecret.destroy("openai-key")
    """

    _bound_params: ClassVar[Mapping[str, Any]] = MappingProxyType({})

    @classmethod
    def _store(cls, opts: Mapping[str, Any], call: str) -> SecretStore:
        return resolve_store(cls._bound_params, opts, call=call)

    @classmethod
    async def create(
        cls,
        name: str,
        value: str,
        metadata: dict[str, str] | None = None,
        **opts: Any,
    ) -> SecretInfo:
        store = cls._store(opts, "create")
        normalized = normalize_secret_name(name)
        return await asyncio.to_thread(store.create, normalized, value, metadata=metadata)

    @classmethod
    async def update(
        cls,
        secret: str,
        value: str,
        metadata: dict[str, str] | None = None,
        **opts: Any,
    ) -> SecretInfo:
        store = cls._store(opts, "update")
        selector = secret_selector(secret)
        return await asyncio.to_thread(store.update, selector, value, metadata=metadata)

    @classmethod
    async def get_info(cls, secret: str, **opts: Any) -> SecretInfo:
        store = cls._store(opts, "get_info")
        return await asyncio.to_thread(store.get_info, secret_selector(secret))

    @classmethod
    def list(
        cls,
        limit: int | None = None,
        next_token: str | None = None,
        **opts: Any,
    ) -> AsyncSecretPaginator:
        return AsyncSecretPaginator(cls._store(opts, "list"), limit=limit, next_token=next_token)

    @classmethod
    async def exists(cls, secret: str, **opts: Any) -> bool:
        store = cls._store(opts, "exists")
        return await asyncio.to_thread(store.exists, secret_selector(secret))

    @classmethod
    async def destroy(cls, secret: str, **opts: Any) -> bool:
        """Lo borra sin ventana de recuperación: True si lo borró, False si no
        existía (como E2B; ver `SecretStore.destroy`)."""
        store = cls._store(opts, "destroy")
        return await asyncio.to_thread(store.destroy, secret_selector(secret))

    @staticmethod
    def fill(secret: str) -> str:
        return fill(secret)

    @staticmethod
    def iam_token(*, audience: str, token_type: str) -> NoReturn:
        raise unimplemented("iam")


__all__ = [
    "AsyncSecret",
    "AsyncSecretPaginator",
    "Secret",
    "SecretInfo",
    "SecretPaginator",
    "fill",
    "normalize_secret_name",
]
