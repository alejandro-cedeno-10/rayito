"""Diagnóstico de un arranque fallido (ADR-028): la cola acotada y sin
secretos de la salida de error de la app del usuario, o el mensaje de la
excepción, que el host deja en el marcador de fallo para que `deploy()` la
muestre en su error.

Sólo biblioteca estándar y nada de `rayito`. Un texto del usuario nunca
llega al marcador sin pasar por `sanitize_diagnostics`: se le quitan los
caracteres de control, los valores de las variables de entorno del proceso
(salvo las estructurales de `STRUCTURAL_ENV_NAMES`), los patrones con forma de
credencial y se recorta a `MAX_DIAGNOSTICS_BYTES`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Final

#: Tope en bytes UTF-8 del diagnóstico (`AGENT_STARTUP_DIAGNOSTICS_MAX_BYTES`
#: de `limits.json`).
MAX_DIAGNOSTICS_BYTES: Final = 2048
#: Lo que sustituye a un valor retirado.
REDACTED: Final = "[redacted]"
#: Un valor de entorno más corto que esto no se retira: sería ruido (`1`,
#: `utf-8`) y no es una credencial.
MIN_REDACTED_VALUE_LENGTH: Final = 8
#: Largo mínimo de un valor que el host exportó desde Secrets Manager
#: (`extra_values`, `agentSecretEnvRedactMinChars`): se retira aunque sea más
#: corto que `MIN_REDACTED_VALUE_LENGTH`, porque sí es un secreto.
MIN_EXTRA_REDACTED_VALUE_LENGTH: Final = 4
#: Variables cuyo valor es estructura del sandbox (rutas, idioma), no un
#: secreto; sus valores se dejan ver para que una traza se pueda leer.
STRUCTURAL_ENV_NAMES: Final = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "HOME",
        "PWD",
        "OLDPWD",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        "HOSTNAME",
        "VIRTUAL_ENV",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    }
)
#: Largo mínimo de una clave con prefijo de proveedor (`sk-...`), de los dos
#: tramos de un JWT y de una cadena sin separadores que se toma por un token.
PROVIDER_KEY_MIN_LENGTH: Final = 16
JWT_SEGMENT_MIN_LENGTH: Final = 8
TOKEN_RUN_MIN_LENGTH: Final = 40
_CONTROL_CHARACTERS: Final = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]")
#: Cada patrón con grupo conserva el grupo 1 (el nombre y el separador) y
#: retira el resto; sin grupo se retira todo lo que casa. El orden importa:
#: `Authorization` va antes que `Bearer` para retirar también el esquema
#: `Basic` y su valor.
_CREDENTIAL_PATTERNS: Final = (
    re.compile(r"(?i)(\bauthorization[\"']?\s*[=:]\s*).*"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        rf"\beyJ[A-Za-z0-9_-]{{{JWT_SEGMENT_MIN_LENGTH},}}"
        rf"\.[A-Za-z0-9_-]{{{JWT_SEGMENT_MIN_LENGTH},}}(?:\.[A-Za-z0-9_-]*)?"
    ),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(rf"\bsk-[A-Za-z0-9_-]{{{PROVIDER_KEY_MIN_LENGTH},}}"),
    re.compile(r"(://[^\s/:@]+:)[^\s/@]+(?=@)"),
    re.compile(
        r"(?i)((?<![A-Za-z0-9_])[A-Za-z0-9_]*"
        r"(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|credential|private[_-]?key)"
        r"[A-Za-z0-9_]*[\"']?\s*[=:]\s*[\"']?)[^\s\"',}]+"
    ),
    re.compile(rf"(?<![A-Za-z0-9_/.-])[A-Za-z0-9_/+=-]{{{TOKEN_RUN_MIN_LENGTH},}}"),
)


def _normalized(text: str) -> str:
    """`text` con los saltos de línea unificados a LF y sin caracteres de
    control, la misma limpieza que recibe el diagnóstico antes de buscar
    valores en él."""
    return _CONTROL_CHARACTERS.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def _redaction_forms(value: str, min_length: int) -> set[str]:
    """El valor tal cual y en su forma normalizada (`_normalized`), los que
    midan al menos `min_length`. Sin la forma normalizada, un PEM con CRLF o
    un valor con un carácter de control no casaría con el texto ya limpio."""
    return {form for form in (value, _normalized(value)) if len(form) >= min_length}


def _without_environment_values(
    text: str, environ: Mapping[str, str], extra_values: Iterable[str] = ()
) -> str:
    candidates: set[str] = set()
    for name, value in environ.items():
        if name not in STRUCTURAL_ENV_NAMES:
            candidates |= _redaction_forms(value, MIN_REDACTED_VALUE_LENGTH)
    for value in extra_values:
        candidates |= _redaction_forms(value, MIN_EXTRA_REDACTED_VALUE_LENGTH)
    for value in sorted(candidates, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text


def _without_credentials(text: str) -> str:
    for pattern in _CREDENTIAL_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def bounded_tail(text: str, max_bytes: int = MAX_DIAGNOSTICS_BYTES) -> str:
    """El final de `text` en como mucho `max_bytes` bytes UTF-8, sin partir
    un carácter y empezando en el principio de una línea si cabe."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    tail = encoded[-max_bytes:].decode("utf-8", errors="ignore")
    newline = tail.find("\n")
    return tail[newline + 1 :] if 0 <= newline < len(tail) - 1 else tail


def sanitize_diagnostics(
    text: str, environ: Mapping[str, str], extra_values: Iterable[str] = ()
) -> str:
    """`text` sin caracteres de control, sin valores de `environ` ni
    patrones de credencial, acotado a `MAX_DIAGNOSTICS_BYTES`. La
    sustitución va antes del recorte para que un secreto partido por el
    recorte no sobreviva. `extra_values` son valores que se retiran siempre
    que midan al menos `MIN_EXTRA_REDACTED_VALUE_LENGTH` (los que el host
    exportó desde Secrets Manager); por defecto, ninguno."""
    cleaned = _without_credentials(
        _without_environment_values(_normalized(text), environ, extra_values)
    )
    return bounded_tail(cleaned.strip()).strip()
