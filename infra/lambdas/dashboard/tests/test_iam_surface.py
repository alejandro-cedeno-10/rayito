"""La variante `AuthMode=aws-iam`: la función de la API sólo sirve `/api/v1`
y sólo a peticiones que la Function URL entregó firmadas, con el token de
Cognito en `X-Rayito-Token`; la función de la página sirve la SPA y la
configuración de inicio de sesión sin ningún permiso sobre MicroVMs."""

from __future__ import annotations

import json
from typing import Any

import _stack_path  # noqa: F401
import pytest
from dashboard_stack.appearance import LiveAppearance
from dashboard_stack.config import SURFACE_IAM_API, ShellConfig
from dashboard_stack.identity import IamEndpoint, IamEndpointCache, parse_iam_endpoint
from dashboard_stack.shell import ShellApp
from dashboard_stack.tokens import AuthUnavailable
from stack_signing import Signer
from test_app import COGNITO_DOMAIN, IDS, Clients, Rig

from rayito._dashboard._theme import Appearance

PAGE_ORIGIN = "https://pagepage.lambda-url.us-east-1.on.aws"
API_URL = "https://apiapi.lambda-url.us-east-1.on.aws/"
POOL_ID = "us-east-1:00000000-0000-0000-0000-000000000001"
SIGNER_ARN = "arn:aws:sts::123456789012:assumed-role/rayito-identity/CognitoIdentityCredentials"
KILL = f"/api/v1/sandboxes/{IDS[0]}/kill"


@pytest.fixture(scope="module")
def signer() -> Signer:
    return Signer()


@pytest.fixture
def api_rig(signer: Signer) -> Rig:
    return Rig(signer, surface=SURFACE_IAM_API, allowed_origin=PAGE_ORIGIN)


def test_a_signed_request_with_the_cognito_token_in_its_own_header_reads(api_rig: Rig) -> None:
    status, _, body = api_rig.call(
        "GET",
        "/api/v1/sandboxes",
        token=api_rig.token(),
        iam_arn=SIGNER_ARN,
        token_header="x-rayito-token",
    )
    assert status == 200 and len(body["sandboxes"]) == len(IDS)


def test_a_request_that_did_not_arrive_signed_is_refused_before_the_token_is_read(
    api_rig: Rig,
) -> None:
    status, _, body = api_rig.call(
        "GET", "/api/v1/sandboxes", token=api_rig.token(), token_header="x-rayito-token"
    )
    assert status == 403 and body["error"]["code"] == "forbidden"
    assert api_rig.plane.listed == 0


def test_the_bearer_authorization_header_is_not_a_token_on_the_api_function(api_rig: Rig) -> None:
    status, _, _ = api_rig.call(
        "GET", "/api/v1/sandboxes", token=api_rig.token(), iam_arn=SIGNER_ARN
    )
    assert status == 401
    assert api_rig.plane.listed == 0


def test_a_signed_request_without_the_cognito_token_is_a_401(api_rig: Rig) -> None:
    status, _, _ = api_rig.call("GET", "/api/v1/sandboxes", iam_arn=SIGNER_ARN)
    assert status == 401


def test_the_api_function_serves_no_page_and_no_config(api_rig: Rig) -> None:
    for path in ("/", "/config.json", "/js/app.js"):
        status, _, _ = api_rig.call("GET", path, iam_arn=SIGNER_ARN)
        assert status == 404, path


def test_an_action_needs_the_page_origin_not_the_function_own_origin(api_rig: Rig) -> None:
    token = api_rig.token("rayito-dashboard-actions")
    own = api_rig.call("POST", KILL, token=token, iam_arn=SIGNER_ARN, token_header="x-rayito-token")
    assert own[0] == 403 and own[2]["error"]["message"] == "origen no admitido"
    other = api_rig.call(
        "POST",
        KILL,
        token=token,
        iam_arn=SIGNER_ARN,
        token_header="x-rayito-token",
        headers={"Origin": "https://evil.example.com"},
    )
    assert other[0] == 403
    assert api_rig.plane.acted == []


def test_an_action_from_the_page_origin_runs_and_is_audited(api_rig: Rig) -> None:
    status, _, _ = api_rig.call(
        "POST",
        KILL,
        token=api_rig.token("rayito-dashboard-actions"),
        iam_arn=SIGNER_ARN,
        token_header="x-rayito-token",
        headers={"Origin": PAGE_ORIGIN},
        body={"confirm": True},
    )
    assert status == 202
    assert api_rig.plane.acted == [("terminate_microvm", IDS[0])]
    assert api_rig.dynamo.items


def test_the_default_surface_keeps_ignoring_the_iam_identity(signer: Signer) -> None:
    rig = Rig(signer)
    status, _, _ = rig.call("GET", "/api/v1/sandboxes", token=rig.token())
    assert status == 200


class FixedEndpoint:
    def endpoint(self) -> IamEndpoint:
        return IamEndpoint(POOL_ID, API_URL)


def shell(clients: Clients | None = None) -> ShellApp:
    return ShellApp(
        config=ShellConfig(
            region="us-east-1",
            user_pool_id="us-east-1_AbCdEfGhI",
            client_name="rayito-spa",
            cognito_domain=COGNITO_DOMAIN,
            identity_parameter="/rayito/dashboard/x/iam",
            theme_parameter=None,
            logo_parameter=None,
        ),
        clients=clients or Clients("client-1"),
        endpoints=FixedEndpoint(),
        appearance=LiveAppearance(lambda: Appearance()),
    )


def get(app: ShellApp, path: str, method: str = "GET") -> dict[str, Any]:
    return app.respond(
        {
            "rawPath": path,
            "headers": {},
            "requestContext": {
                "domainName": "pagepage.lambda-url.us-east-1.on.aws",
                "http": {"method": method},
            },
        }
    )


def test_the_shell_serves_the_page_without_any_token() -> None:
    response = get(shell(), "/")
    assert response["statusCode"] == 200 and "<rayito-app>" in response["body"]


def test_the_public_config_carries_the_iam_block_the_spa_needs() -> None:
    response = get(shell(), "/config.json")
    assert json.loads(response["body"]) == {
        "auth": {
            "type": "cognito",
            "domain": f"https://{COGNITO_DOMAIN}",
            "clientId": "client-1",
            "scope": "openid",
            "iam": {
                "region": "us-east-1",
                "providerName": "cognito-idp.us-east-1.amazonaws.com/us-east-1_AbCdEfGhI",
                "identityPoolId": POOL_ID,
                "apiUrl": API_URL.rstrip("/"),
            },
        }
    }


def test_the_shell_csp_opens_connect_src_only_to_cognito_and_the_api() -> None:
    csp = get(shell(), "/")["headers"]["Content-Security-Policy"]
    connect = next(part for part in csp.split("; ") if part.startswith("connect-src"))
    assert connect.split()[1:] == [
        "'self'",
        f"https://{COGNITO_DOMAIN}",
        "https://cognito-identity.us-east-1.amazonaws.com",
        API_URL.rstrip("/"),
    ]


def test_the_shell_has_no_api() -> None:
    assert get(shell(), "/api/v1/sandboxes")["statusCode"] == 404
    assert get(shell(), "/api/v1/sandboxes", method="POST")["statusCode"] == 404


def test_the_shell_refuses_other_methods() -> None:
    assert get(shell(), "/", method="POST")["statusCode"] == 405


def test_an_ambiguous_client_makes_the_config_unavailable() -> None:
    response = get(shell(Clients("a", "b")), "/config.json")
    assert response["statusCode"] == 503


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        "[]",
        json.dumps({"identityPoolId": POOL_ID}),
        json.dumps({"identityPoolId": POOL_ID, "apiUrl": "http://x.lambda-url.us-east-1.on.aws/"}),
        json.dumps({"identityPoolId": POOL_ID, "apiUrl": "https://evil.example.com/"}),
        json.dumps(
            {"identityPoolId": POOL_ID, "apiUrl": "https://a.lambda-url.us-east-1.on.aws.evil.io/"}
        ),
        json.dumps({"identityPoolId": "eu-west-1:" + POOL_ID.split(":")[1], "apiUrl": API_URL}),
        json.dumps({"identityPoolId": "nope", "apiUrl": API_URL}),
        json.dumps({"identityPoolId": 1, "apiUrl": API_URL}),
    ],
)
def test_a_tampered_identity_parameter_is_unavailable(document: str) -> None:
    with pytest.raises(AuthUnavailable):
        parse_iam_endpoint(document, "us-east-1")


def test_the_identity_parameter_is_read_once_per_ttl() -> None:
    reads: list[int] = []
    now = [0.0]

    def read() -> IamEndpoint:
        reads.append(1)
        return parse_iam_endpoint(
            json.dumps({"identityPoolId": POOL_ID, "apiUrl": API_URL}), "us-east-1"
        )

    cache = IamEndpointCache(read, clock=lambda: now[0], ttl_seconds=10)
    cache.endpoint()
    cache.endpoint()
    now[0] = 11
    cache.endpoint()
    assert len(reads) == 2
