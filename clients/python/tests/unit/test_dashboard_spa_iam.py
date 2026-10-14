"""La variante `AuthMode=aws-iam` de la SPA: la firma SigV4 del navegador
(`sigv4.js`) coincide con la de `botocore`, que hace de oráculo independiente,
y `api.js` envía a la función de la API la firma, el token de Cognito en su
propia cabecera y nunca `Authorization: Bearer`. Los módulos ES se ejecutan con
Node (se salta si no está)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

STATIC_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "rayito" / "_dashboard" / "_static" / "js"
)
NODE = shutil.which("node")
NODE_TIMEOUT_SECONDS = 30
TIMESTAMP = "20261014T093000Z"
ACCESS_KEY = "ASIA012345678EXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
SESSION_TOKEN = "session/token+with=chars"
REGION = "us-east-1"
SERVICE = "lambda"
BASE = "https://apiapi.lambda-url.us-east-1.on.aws"

pytestmark = pytest.mark.skipif(NODE is None, reason="node no está instalado")


def run_node(source: str) -> Any:
    """El JSON que imprime un módulo ES ejecutado con Node."""
    assert NODE is not None
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", source],
        capture_output=True,
        text=True,
        check=True,
        timeout=NODE_TIMEOUT_SECONDS,
    )
    return json.loads(result.stdout)


def js_signature(method: str, url: str, body: str | None) -> dict[str, str]:
    script = f"""
import {{ signRequest }} from "{(STATIC_DIR / "sigv4.js").as_uri()}";
const headers = await signRequest(
  {{ method: {json.dumps(method)}, url: {json.dumps(url)}, body: {json.dumps(body)} ?? undefined }},
  {{ accessKeyId: {json.dumps(ACCESS_KEY)}, secretKey: {json.dumps(SECRET_KEY)},
    sessionToken: {json.dumps(SESSION_TOKEN)} }},
  {{ region: "{REGION}", service: "{SERVICE}", now: new Date("2026-10-14T09:30:00.000Z") }},
);
console.log(JSON.stringify(headers));
"""
    headers: dict[str, str] = run_node(script)
    return headers


def botocore_signature(method: str, url: str, body: str | None) -> str:
    auth = SigV4Auth(Credentials(ACCESS_KEY, SECRET_KEY, SESSION_TOKEN), SERVICE, REGION)
    request = AWSRequest(
        method=method,
        url=url,
        data=(body or "").encode(),
        headers={"X-Amz-Date": TIMESTAMP, "X-Amz-Security-Token": SESSION_TOKEN},
    )
    request.context["timestamp"] = TIMESTAMP
    request.headers["X-Amz-Content-Sha256"] = auth.payload(request)
    canonical = auth.canonical_request(request)
    return str(auth.signature(auth.string_to_sign(request, canonical), request))


CASES = [
    ("GET", f"{BASE}/api/v1/sandboxes", None),
    ("GET", f"{BASE}/api/v1/quotas?account=Mi%20cuenta&limit=50", None),
    ("GET", f"{BASE}/api/v1/sandboxes?b=2&a=1&a=0&q=x%2By%2A%21%27%28%29~", None),
    ("POST", f"{BASE}/api/v1/sandboxes/microvm-0001/kill", json.dumps({"confirm": True})),
    ("POST", f"{BASE}/api/v1/actions?account=prod", json.dumps({"nombre": "añejo ñ", "n": [1, 2]})),
]


@pytest.mark.parametrize(("method", "url", "body"), CASES)
def test_the_browser_signature_equals_botocore(method: str, url: str, body: str | None) -> None:
    headers = js_signature(method, url, body)
    signature = re.search(r"Signature=([0-9a-f]{64})$", headers["Authorization"])
    assert signature is not None
    assert signature.group(1) == botocore_signature(method, url, body)
    assert headers["X-Amz-Date"] == TIMESTAMP
    assert headers["X-Amz-Security-Token"] == SESSION_TOKEN
    assert headers["Authorization"].startswith(
        f"AWS4-HMAC-SHA256 Credential={ACCESS_KEY}/20261014/{REGION}/{SERVICE}/aws4_request, "
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date;x-amz-security-token, "
    )


SCENARIO = Path(__file__).resolve().parent / "js" / "spa_iam_scenario.mjs"


def run_scenario(*, iam_block: str, forbidden: str = "") -> dict[str, Any]:
    assert NODE is not None
    result = subprocess.run(
        [NODE, str(SCENARIO)],
        capture_output=True,
        text=True,
        check=True,
        timeout=NODE_TIMEOUT_SECONDS,
        env={
            "AUTH_MODULE": (STATIC_DIR / "auth.js").as_uri(),
            "API_MODULE": (STATIC_DIR / "api.js").as_uri(),
            "IAM_BLOCK": iam_block,
            "FORBIDDEN": forbidden,
        },
    )
    outcome: dict[str, Any] = json.loads(result.stdout)
    return outcome


def test_the_api_client_signs_and_sends_the_cognito_token_in_its_own_header() -> None:
    calls = run_scenario(iam_block="iam")["calls"]
    identity = [c for c in calls if "X-Amz-Target" in c["headers"]]
    assert [c["headers"]["X-Amz-Target"] for c in identity] == [
        "AWSCognitoIdentityService.GetId",
        "AWSCognitoIdentityService.GetCredentialsForIdentity",
    ]
    assert all(c["url"] == "https://cognito-identity.us-east-1.amazonaws.com/" for c in identity)
    api_calls = [c for c in calls if c["url"].startswith(BASE)]
    assert [c["method"] for c in api_calls] == ["GET", "POST"]
    assert api_calls[0]["url"] == f"{BASE}/api/v1/sandboxes?limit=5&account=Mi%20cuenta"
    for call in api_calls:
        headers = call["headers"]
        assert headers["X-Rayito-Token"] == "ACCESS"
        assert headers["Authorization"].startswith(
            "AWS4-HMAC-SHA256 Credential=ASIA012345678EXAMPLE/"
        )
        assert "Bearer" not in headers["Authorization"]
        assert call["credentials"] == "omit"
    assert len(identity) == 2


def test_the_default_mode_keeps_the_bearer_header() -> None:
    calls = run_scenario(iam_block="iamOff")["calls"]
    api_calls = [c for c in calls if c["url"].startswith("/api/v1")]
    assert len(api_calls) == 2
    for call in api_calls:
        assert call["headers"]["Authorization"] == "Bearer ACCESS"
        assert "X-Rayito-Token" not in call["headers"]
        assert call["credentials"] == "same-origin"
    assert not [c for c in calls if "X-Amz-Target" in c["headers"]]


IDENTITY_TARGETS = ("GetId", "GetCredentialsForIdentity")
PROVIDER = "cognito-idp.us-east-1.amazonaws.com/us-east-1_AbCdEfGhI"


def identity_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in calls if "X-Amz-Target" in c["headers"]]


def api_calls_of(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in calls if c["url"].startswith(BASE)]


def test_the_identity_pool_logins_carry_the_id_token_not_the_access_token() -> None:
    calls = run_scenario(iam_block="iam")["calls"]
    identity = identity_calls(calls)
    assert len(identity) == len(IDENTITY_TARGETS)
    for call in identity:
        assert call["body"]["Logins"] == {PROVIDER: "IDTOKEN"}


def test_a_403_from_the_iam_layer_is_retried_once_with_fresh_credentials() -> None:
    outcome = run_scenario(iam_block="iam", forbidden="iam")
    calls = outcome["calls"]
    assert outcome["failure"] is None
    assert len(identity_calls(calls)) == 2 * len(IDENTITY_TARGETS)
    assert [c["method"] for c in api_calls_of(calls)] == ["GET", "GET", "POST"]


def test_a_403_from_the_api_is_never_retried() -> None:
    outcome = run_scenario(iam_block="iam", forbidden="api")
    calls = outcome["calls"]
    assert outcome["failure"] == {"name": "ApiFailure", "status": 403, "code": "forbidden"}
    assert len(identity_calls(calls)) == len(IDENTITY_TARGETS)
    assert len(api_calls_of(calls)) == 1
