"""El destino de la variante `AuthMode=aws-iam`: el id del identity pool de
Cognito y la URL de la función de la API. La pila los guarda en un parámetro
de SSM (JSON) que crea *después* de las dos Function URL; la función de la
página lo lee en tiempo de ejecución, porque pasarlos por entorno cerraría un
ciclo de dependencias (la URL de la página es la `redirect_uri` del cliente de
Cognito, y el identity pool referencia a ese cliente).

Los dos valores acaban en la configuración pública de la página y en su CSP
(`connect-src`), así que se validan con patrones estrictos: un parámetro
manipulado no puede apuntar a otro destino.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from dashboard_stack.tokens import AuthUnavailable
from rayito._limits import DASHBOARD_STACK_AUTH_CACHE_SECONDS

IDENTITY_POOL_ID_KEY: Final = "identityPoolId"
API_URL_KEY: Final = "apiUrl"
IDENTITY_POOL_ID_PATTERN: Final = re.compile(
    r"[a-z0-9-]+:[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}"
)
#: `https://<id>.lambda-url.<región>.on.aws`, con o sin barra final.
API_URL_PATTERN: Final = re.compile(
    r"https://[a-z0-9]{1,64}\.lambda-url\.[a-z0-9-]{1,32}\.on\.aws/?"
)


@dataclass(frozen=True)
class IamEndpoint:
    identity_pool_id: str
    api_url: str

    @property
    def api_origin(self) -> str:
        return self.api_url.rstrip("/")


def parse_iam_endpoint(text: str, region: str) -> IamEndpoint:
    """Valida el JSON del parámetro. Cualquier defecto es `AuthUnavailable`:
    la página no puede iniciar sesión, y el motivo no se muestra."""
    try:
        document = json.loads(text)
        pool_id = document[IDENTITY_POOL_ID_KEY]
        api_url = document[API_URL_KEY]
    except (ValueError, KeyError, TypeError):
        raise AuthUnavailable("identity") from None
    valid = (
        isinstance(pool_id, str)
        and isinstance(api_url, str)
        and IDENTITY_POOL_ID_PATTERN.fullmatch(pool_id) is not None
        and pool_id.startswith(f"{region}:")
        and API_URL_PATTERN.fullmatch(api_url) is not None
    )
    if not valid:
        raise AuthUnavailable("identity")
    return IamEndpoint(identity_pool_id=pool_id, api_url=api_url)


class IamEndpointCache:
    def __init__(
        self,
        read: Callable[[], IamEndpoint],
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = DASHBOARD_STACK_AUTH_CACHE_SECONDS,
    ) -> None:
        self._read = read
        self._clock = clock
        self._ttl = ttl_seconds
        self._endpoint: IamEndpoint | None = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def endpoint(self) -> IamEndpoint:
        with self._lock:
            now = self._clock()
            if self._endpoint is None or now - self._loaded_at >= self._ttl:
                try:
                    self._endpoint = self._read()
                except AuthUnavailable:
                    raise
                except Exception:
                    raise AuthUnavailable("identity") from None
                self._loaded_at = now
            return self._endpoint
