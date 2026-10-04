"""Excepciones con los nombres de `e2b.exceptions` (E2B 2.51).

Las nativas se re-exportan tal cual (son las mismas clases, no copias),
`UnimplementedError` incluida: un `except rayito.UnimplementedError` atrapa
lo que lanza el shim y un `except rayito.e2b.UnimplementedError` lo que
lanza el SDK nativo. El shim la construye con `doc=COMPAT_DOC_PATH`.
`NotEnoughSpaceException` y `ServiceBusyException` son alias de las clases
que Rayito sí lanza (`DiskFullException`, `CapacityException`);
`TemplateException` y `BuildException` existen para que los `except` de un
programa E2B sigan compilando. `SecretException` y `SecretNotFoundException`
son las nativas (las lanza el `Secret`/`AsyncSecret` del shim); a diferencia
de E2B, `SecretException` es un `SandboxException` y
`SecretNotFoundException` también es la `NotFoundException` nativa.
`RayitoCompatWarning` vive en `rayito.exceptions` y se re-exporta aquí.

Divergencia documentada: en E2B `FileUploadException` hereda de
`BuildException` porque sólo sale de construir templates; en Rayito hereda
de `TransferException` (un `SandboxException`) porque sólo sale de una
importación de `upload_url`/escritura grande que termina `FAILED`.
"""

from __future__ import annotations

from rayito.exceptions import (
    AuthenticationException,
    CapacityException,
    CommandExitException,
    DiskFullException,
    FileNotFoundException,
    FileUploadException,
    GitAuthException,
    GitUpstreamException,
    InvalidArgumentException,
    NotFoundException,
    RateLimitException,
    RayitoCompatWarning,
    SandboxException,
    SandboxNotFoundException,
    SecretException,
    SecretNotFoundException,
    TimeoutException,
    UnimplementedError,
    VolumeException,
    VolumeNotFoundException,
    VolumePathNotFoundException,
)

COMPAT_DOC_PATH = "docs/site/docs/e2b-compat.md"

NotEnoughSpaceException = DiskFullException
ServiceBusyException = CapacityException


class TemplateException(SandboxException):
    """Nombre de E2B para un error de template. Rayito nunca la lanza: una
    imagen inexistente es `NotFoundException` del plano de control."""


class BuildException(Exception):
    """Nombre de E2B para un error de construcción de template. Rayito nunca
    la lanza: no hay API de construcción de templates (`Template` es
    `UnimplementedError`)."""


__all__ = [
    "AuthenticationException",
    "BuildException",
    "CommandExitException",
    "FileNotFoundException",
    "FileUploadException",
    "GitAuthException",
    "GitUpstreamException",
    "InvalidArgumentException",
    "NotEnoughSpaceException",
    "NotFoundException",
    "RateLimitException",
    "RayitoCompatWarning",
    "SandboxException",
    "SandboxNotFoundException",
    "SecretException",
    "SecretNotFoundException",
    "ServiceBusyException",
    "TemplateException",
    "TimeoutException",
    "UnimplementedError",
    "VolumeException",
    "VolumeNotFoundException",
    "VolumePathNotFoundException",
]
