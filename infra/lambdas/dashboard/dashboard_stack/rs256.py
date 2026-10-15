"""Verificación de firmas RS256 (RSASSA-PKCS1-v1_5 con SHA-256, RFC 8017
§8.2.2) con sólo la biblioteca estándar: el runtime de Lambda no trae
`cryptography` ni PyJWT, y la pila no instala dependencias. Sólo se verifica
con la clave pública del JWKS de Cognito; no hay ningún secreto aquí, así que
la comparación en tiempo constante es una cautela y no una necesidad.

Se compara el mensaje codificado entero (`EMSA-PKCS1-v1_5`) con el esperado,
no se analiza el relleno: no hay forma de aceptar una firma con relleno
manipulado (los ataques de Bleichenbacher sobre verificadores laxos).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from typing import Final

#: `DigestInfo` de SHA-256 con su cabecera DER (RFC 8017 §9.2, nota 1).
SHA256_DIGEST_INFO_PREFIX: Final = bytes.fromhex("3031300d060960864801650304020105000420")
#: Cognito firma con claves de 2048 bits; un módulo menor se rechaza.
MIN_MODULUS_BITS: Final = 2048
MIN_PUBLIC_EXPONENT: Final = 3
#: `0x00 0x01 <relleno 0xFF> 0x00` rodean al `DigestInfo` (RFC 8017 §9.2): tres
#: bytes de marco; con un módulo de `MIN_MODULUS_BITS` el relleno supera de
#: sobra el mínimo de ocho.
FRAMING_BYTES: Final = 3
BASE64URL_ALPHABET: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def b64url_decode(text: str) -> bytes:
    """Base64 URL-safe sin relleno, estricto: un carácter fuera del alfabeto
    es un error (el decodificador de la biblioteca los descarta en silencio)."""
    if not text or not set(text) <= BASE64URL_ALPHABET:
        raise ValueError("base64url inválido")
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        raise ValueError("base64url inválido") from None


def b64url_int(text: str) -> int:
    return int.from_bytes(b64url_decode(text), "big")


def verify_rs256(signing_input: bytes, signature: bytes, *, modulus: int, exponent: int) -> bool:
    if (
        modulus.bit_length() < MIN_MODULUS_BITS
        or exponent < MIN_PUBLIC_EXPONENT
        or exponent % 2 == 0
    ):
        return False
    size = (modulus.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    value = int.from_bytes(signature, "big")
    if value >= modulus:
        return False
    recovered = pow(value, exponent, modulus).to_bytes(size, "big")
    return hmac.compare_digest(recovered, expected_encoding(signing_input, size))


def expected_encoding(signing_input: bytes, size: int) -> bytes:
    digest_info = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(signing_input).digest()
    padding_length = size - len(digest_info) - FRAMING_BYTES
    return b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info
