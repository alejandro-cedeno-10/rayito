"""Una clave RSA de prueba con la que se firman tokens como Cognito, y las
constantes que comparten los tests de la pila `dashboard`."""

from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

REGION = "us-east-1"
USER_POOL_ID = f"{REGION}_AbCdEfGhI"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
CLIENT_ID = "client-spa-0000"
KEY_ID = "kid-current"
RSA_KEY_BITS = 2048
RSA_PUBLIC_EXPONENT = 65537
NOW = 1_800_000_000.0
HOUR_SECONDS = 3600


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Signer:
    """Firma tokens RS256 con una clave de prueba y publica su JWK."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(RSA_PUBLIC_EXPONENT, RSA_KEY_BITS)

    @property
    def numbers(self) -> rsa.RSAPublicNumbers:
        return self.key.public_key().public_numbers()

    def jwk(self, kid: str = KEY_ID) -> dict[str, str]:
        def encode(value: int) -> str:
            return b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))

        return {
            "kty": "RSA",
            "kid": kid,
            "alg": "RS256",
            "use": "sig",
            "n": encode(self.numbers.n),
            "e": encode(self.numbers.e),
        }

    def jwks(self) -> bytes:
        return json.dumps({"keys": [self.jwk()]}).encode()

    def sign(self, signing_input: bytes) -> bytes:
        return self.key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())

    def token(
        self,
        claims: dict[str, object] | None = None,
        *,
        header: dict[str, object] | None = None,
    ) -> str:
        body = {
            "iss": ISSUER,
            "token_use": "access",
            "client_id": CLIENT_ID,
            "sub": "subject-0001",
            "username": "ada@example.com",
            "exp": NOW + HOUR_SECONDS,
            "cognito:groups": [],
            **(claims or {}),
        }
        head = {"alg": "RS256", "kid": KEY_ID, **(header or {})}
        signing_input = f"{b64url(json.dumps(head).encode())}.{b64url(json.dumps(body).encode())}"
        return f"{signing_input}.{b64url(self.sign(signing_input.encode()))}"
