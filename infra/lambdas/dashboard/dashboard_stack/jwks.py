"""Las dos fuentes de verdad que la validación del token necesita de
Cognito, con caché: el JWKS del pool (HTTPS, sin credenciales) y los ids de
sus clientes de aplicación (`cognito-idp:ListUserPoolClients`). La Function
URL y el cliente del pool no pueden referirse entre sí en la plantilla (el
cliente necesita la URL de retorno y la Lambda el id del cliente), así que la
Lambda lo descubre aquí por el nombre del cliente de la SPA (que la plantilla
sí le pasa, porque un nombre no crea dependencia). El pool puede tener otros
clientes (uno de CLI o de máquina a máquina): sólo el de la SPA es la
audiencia de los tokens que el panel acepta.

Un `kid` desconocido fuerza una recarga del JWKS (Cognito rota claves), pero
como mucho una cada `DASHBOARD_STACK_JWKS_REFRESH_MIN_SECONDS`: quien manda
tokens con `kid` inventados no puede convertir la Lambda en un cliente HTTP
a su ritmo.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from collections.abc import Callable
from typing import Any, Final

from dashboard_stack.tokens import AuthUnavailable, SigningKey, signing_key_from_jwk
from rayito._limits import (
    DASHBOARD_STACK_AUTH_CACHE_SECONDS,
    DASHBOARD_STACK_HTTP_TIMEOUT_SECONDS,
    DASHBOARD_STACK_JWKS_MAX_BYTES,
    DASHBOARD_STACK_JWKS_REFRESH_MIN_SECONDS,
)

USER_POOL_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9-]+_[A-Za-z0-9]+")
RSA_KEY_TYPE: Final = "RSA"
#: `ListUserPoolClients` admite hasta 60 resultados por página.
LIST_CLIENTS_PAGE_SIZE: Final = 60


def issuer_url(region: str, user_pool_id: str) -> str:
    """El `iss` de los tokens del pool; el id se valida porque forma parte de
    una URL a la que la Lambda se conecta."""
    if USER_POOL_ID_PATTERN.fullmatch(user_pool_id) is None:
        raise ValueError("user_pool_id no tiene la forma <región>_<id>")
    return f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"


def fetch_jwks(url: str) -> bytes:
    """Descarga acotada (tiempo y tamaño) del JWKS. La URL la construye
    `issuer_url`: nunca viene de la petición."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=DASHBOARD_STACK_HTTP_TIMEOUT_SECONDS) as response:
        body: bytes = response.read(DASHBOARD_STACK_JWKS_MAX_BYTES + 1)
    if len(body) > DASHBOARD_STACK_JWKS_MAX_BYTES:
        raise ValueError("JWKS demasiado grande")
    return body


class JwksCache:
    def __init__(
        self,
        fetch: Callable[[], bytes],
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = DASHBOARD_STACK_AUTH_CACHE_SECONDS,
        refresh_min_seconds: float = DASHBOARD_STACK_JWKS_REFRESH_MIN_SECONDS,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._ttl = ttl_seconds
        self._refresh_min = refresh_min_seconds
        self._keys: dict[str, SigningKey] = {}
        self._loaded_at: float | None = None
        self._lock = threading.Lock()

    def key(self, kid: str) -> SigningKey | None:
        with self._lock:
            now = self._clock()
            if self._loaded_at is None or now - self._loaded_at >= self._ttl:
                self._load(now)
            found = self._keys.get(kid)
            if found is None and self._may_refresh(now):
                self._load(now)
                found = self._keys.get(kid)
            return found

    def _may_refresh(self, now: float) -> bool:
        return self._loaded_at is not None and now - self._loaded_at >= self._refresh_min

    def _load(self, now: float) -> None:
        try:
            document = json.loads(self._fetch())
            keys = {
                str(jwk["kid"]): signing_key_from_jwk(jwk)
                for jwk in document["keys"]
                if jwk.get("kty") == RSA_KEY_TYPE
            }
        except (OSError, ValueError, KeyError, TypeError):
            raise AuthUnavailable("jwks") from None
        self._keys = keys
        self._loaded_at = now


class ClientIdCache:
    def __init__(
        self,
        list_clients: Callable[[], frozenset[str]],
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = DASHBOARD_STACK_AUTH_CACHE_SECONDS,
    ) -> None:
        self._list_clients = list_clients
        self._clock = clock
        self._ttl = ttl_seconds
        self._ids: frozenset[str] = frozenset()
        self._loaded_at: float | None = None
        self._lock = threading.Lock()

    def client_ids(self) -> frozenset[str]:
        with self._lock:
            now = self._clock()
            if self._loaded_at is None or now - self._loaded_at >= self._ttl:
                try:
                    self._ids = self._list_clients()
                except Exception:
                    raise AuthUnavailable("clients") from None
                self._loaded_at = now
            return self._ids


def list_user_pool_client_ids(client: Any, user_pool_id: str, client_name: str) -> frozenset[str]:
    """Los ids de los clientes de aplicación del pool que se llaman
    `client_name` (`ListUserPoolClients`, paginado)."""
    ids: set[str] = set()
    params: dict[str, Any] = {"UserPoolId": user_pool_id, "MaxResults": LIST_CLIENTS_PAGE_SIZE}
    while True:
        page = client.list_user_pool_clients(**params)
        ids.update(
            str(item["ClientId"])
            for item in page.get("UserPoolClients", [])
            if item.get("ClientName") == client_name
        )
        token = page.get("NextToken")
        if not token:
            return frozenset(ids)
        params["NextToken"] = token
