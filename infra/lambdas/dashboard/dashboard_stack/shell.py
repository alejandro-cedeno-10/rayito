"""La función de la página de `AuthMode=aws-iam`: sirve la SPA y la
configuración pública de inicio de sesión sin pedir firma (un navegador no
puede firmar la navegación a la página con SigV4) y nada más.

Su rol de IAM sólo escribe sus logs, lista los clientes del pool de Cognito y
lee el parámetro del identity pool y, si los hay, los del tema y el logo: no
tiene permisos sobre MicroVMs, Service Quotas, tablas ni roles de acciones, y
esta clase no construye ninguna API. Una petición a `/api/v1` es un 404: la API
vive en la otra función, tras la firma SigV4 de su Function URL.

`/config.json` añade a lo de la variante por defecto el bloque `iam` (región,
proveedor de identidad, identity pool y URL de la API) que la SPA necesita para
pedir credenciales a Cognito y firmar sus llamadas. El CSP de esta función
abre `connect-src` sólo al Hosted UI, al endpoint de identidades de Cognito de
la región y a la URL de la API.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Protocol

from dashboard_stack.app import (
    COGNITO_SCOPE,
    CONFIG_PATH,
    GET,
    HSTS_VALUE,
    Reply,
    error_reply,
    incoming_from_event,
    json_reply,
    lambda_response,
)
from dashboard_stack.appearance import LiveAppearance
from dashboard_stack.config import ShellConfig
from dashboard_stack.identity import IamEndpoint
from dashboard_stack.tokens import AuthUnavailable, ClientIdSource
from rayito._dashboard._api._contract import API_PREFIX, HTTP_OK, ApiResponse
from rayito._dashboard._api._headers import LOGO_HEADERS, security_headers
from rayito._dashboard._api._router import ApiError
from rayito._dashboard._api._static import LOGO_PATH

#: El endpoint de identidades de Cognito de una región (el que la SPA llama
#: sin firmar para canjear su token por credenciales temporales).
IDENTITY_ENDPOINT: Final = "https://cognito-identity.{region}.amazonaws.com"
PROVIDER_NAME: Final = "cognito-idp.{region}.amazonaws.com/{user_pool_id}"


class IamEndpointSource(Protocol):
    def endpoint(self) -> IamEndpoint: ...


class ShellApp:
    def __init__(
        self,
        *,
        config: ShellConfig,
        clients: ClientIdSource,
        endpoints: IamEndpointSource,
        appearance: LiveAppearance,
    ) -> None:
        self._config = config
        self._clients = clients
        self._endpoints = endpoints
        self._appearance = appearance

    def respond(self, event: Mapping[str, Any]) -> dict[str, Any]:
        request = incoming_from_event(event)
        try:
            reply = self._route(request)
            headers = self._headers()
        except Exception as error:
            reply = error_reply(error, request)
            headers = self._headers(strict=True)
        return lambda_response(reply, headers)

    def _headers(self, *, strict: bool = False) -> dict[str, str]:
        connect = [
            f"https://{self._config.cognito_domain}",
            IDENTITY_ENDPOINT.format(region=self._config.region),
        ]
        if not strict:
            connect.append(self._endpoints.endpoint().api_origin)
        return {**security_headers(connect), "Strict-Transport-Security": HSTS_VALUE}

    def _route(self, request: Any) -> Reply:
        if request.path == API_PREFIX or request.path.startswith(f"{API_PREFIX}/"):
            raise ApiError("not_found")
        if request.method != GET:
            raise ApiError("method_not_allowed")
        if request.path == CONFIG_PATH:
            return self._config_reply()
        found = self._appearance.files.get(request.path)
        if found is None:
            raise ApiError("not_found")
        return Reply(HTTP_OK, found[0], found[1], LOGO_HEADERS if request.path == LOGO_PATH else {})

    def _config_reply(self) -> Reply:
        ids = self._clients.client_ids()
        if len(ids) != 1:
            raise AuthUnavailable("clients")
        (client_id,) = ids
        endpoint = self._endpoints.endpoint()
        document = {
            "auth": {
                "type": "cognito",
                "domain": f"https://{self._config.cognito_domain}",
                "clientId": client_id,
                "scope": COGNITO_SCOPE,
                "iam": {
                    "region": self._config.region,
                    "providerName": PROVIDER_NAME.format(
                        region=self._config.region, user_pool_id=self._config.user_pool_id
                    ),
                    "identityPoolId": endpoint.identity_pool_id,
                    "apiUrl": endpoint.api_origin,
                },
            }
        }
        return json_reply(ApiResponse(HTTP_OK, document))
