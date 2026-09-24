"""Resumen seguro de un error de botocore para el `__cause__` de las
excepciones propias (paridad con `src/aws/sanitize.ts` del SDK TypeScript).

botocore no cuelga la petición firmada de un `ClientError`: su `response`
sólo trae `Error` y `ResponseMetadata` (cabeceras de la *respuesta*). Pero lo
que AWS devuelve sí repite la firma: un `SignatureDoesNotMatch` de S3 trae
`AWSAccessKeyId`, `StringToSign` y `CanonicalRequest` (con el valor de
`x-amz-security-token`) en `response["Error"]`, y un
`InvalidSignatureException` de un servicio JSON mete la cadena canónica en el
`Message`, que `str(exc)` repite. Con `raise ... from exc` todo eso llegaba a
cualquier traceback, `logger.exception` o Sentry. Aquí sólo sobrevive una
lista cerrada de campos y el texto pasa por `redact_aws_text`.
"""

from __future__ import annotations

import re
from typing import Any, Final

REDACTED: Final = "<redacted>"
MAX_MESSAGE_CHARS: Final = 2048

_TEXT_RULES: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # La cadena canónica y la cadena a firmar de un error de firma.
    (
        re.compile(r"\s*The Canonical String for this request should have been.*\Z", re.I | re.S),
        f" {REDACTED}",
    ),
    (re.compile(r"\s*The String-to-Sign should have been.*\Z", re.I | re.S), f" {REDACTED}"),
    # La query entera de una URL prefirmada, y cualquier parámetro de firma suelto.
    (re.compile(r"\?[^\s'\"<>]*X-Amz-[^\s'\"<>]*", re.I), f"?{REDACTED}"),
    (
        re.compile(r"X-Amz-(?:Security-Token|Credential|Signature)(?:=[^\s&'\"<>]*)?", re.I),
        REDACTED,
    ),
    (re.compile(r"(authorization\s*[:=]\s*)[^\r\n'\"<>]+", re.I), rf"\g<1>{REDACTED}"),
    (re.compile(r"(AWS4-HMAC-SHA256\s+)Credential=[^\r\n'\"<>]+", re.I), rf"\g<1>{REDACTED}"),
    (re.compile(r"(x-amz-security-token\s*[:=]\s*)[^\s&'\"<>]+", re.I), rf"\g<1>{REDACTED}"),
    (re.compile(r"((?:^|[^A-Za-z-])Signature\s*[:=]\s*)[0-9a-f]{16,}", re.I), rf"\g<1>{REDACTED}"),
    (re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA)[A-Z0-9]{12,}\b"), REDACTED),
)


def redact_aws_text(text: str) -> str:
    """Quita de un texto de AWS la cadena canónica, las cabeceras de firma,
    los parámetros de una URL prefirmada y los ids de clave de acceso; lo
    acota a 2048 caracteres. Pura y total."""
    result = text
    for pattern, replacement in _TEXT_RULES:
        result = pattern.sub(replacement, result)
    return result if len(result) <= MAX_MESSAGE_CHARS else f"{result[:MAX_MESSAGE_CHARS]}…"


class AwsErrorSummary(Exception):
    """Lo que queda de un error de botocore: `name` (el `Code` del servicio o
    el nombre de la clase de botocore), `code`, el mensaje redactado y de
    `ResponseMetadata` sólo `status_code`, `request_id`,
    `extended_request_id` (`HostId` de S3) y `attempts`. Nunca `response`,
    cabeceras, cuerpo ni credenciales."""

    def __init__(
        self,
        name: str,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
        extended_request_id: str | None = None,
        attempts: int | None = None,
    ) -> None:
        super().__init__(f"{name}: {message}" if message else name)
        self.name = name
        self.message = message
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.extended_request_id = extended_request_id
        self.attempts = attempts

    def __repr__(self) -> str:
        fields = ", ".join(
            f"{name}={value!r}"
            for name, value in (
                ("name", self.name),
                ("code", self.code),
                ("status_code", self.status_code),
                ("request_id", self.request_id),
                ("extended_request_id", self.extended_request_id),
                ("attempts", self.attempts),
            )
            if value is not None
        )
        return f"AwsErrorSummary({fields})"


def _text(value: Any) -> str | None:
    return redact_aws_text(value) if isinstance(value, str) and value else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def sanitize_aws_error(exc: BaseException, *, include_message: bool = True) -> AwsErrorSummary:
    """El resumen seguro de un `ClientError` o un `BotoCoreError`.
    `include_message=False` deja fuera el mensaje: las transferencias por S3
    nunca nombran bucket, clave ni host, y un `EndpointConnectionError` sí."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        message = redact_aws_text(str(exc)) if include_message else ""
        return AwsErrorSummary(type(exc).__name__, message)
    error = response.get("Error")
    error = error if isinstance(error, dict) else {}
    metadata = response.get("ResponseMetadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    code = _text(error.get("Code"))
    return AwsErrorSummary(
        code or type(exc).__name__,
        (_text(error.get("Message")) or "") if include_message else "",
        code=code,
        status_code=_integer(metadata.get("HTTPStatusCode")),
        request_id=_text(metadata.get("RequestId")),
        extended_request_id=_text(metadata.get("HostId")),
        attempts=_integer(metadata.get("RetryAttempts")),
    )


__all__ = ["AwsErrorSummary", "redact_aws_text", "sanitize_aws_error"]
