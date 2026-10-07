"""Excepciones con los nombres de `e2b.exceptions` (E2B 2.51).

Las nativas se re-exportan tal cual (son las mismas clases, no copias),
`UnimplementedError` incluida: un `except rayito.UnimplementedError` atrapa
lo que lanza el shim y un `except rayito.e2b.UnimplementedError` lo que
lanza el SDK nativo. El shim la construye con `doc=COMPAT_DOC_PATH`.
`NotEnoughSpaceException` y `ServiceBusyException` son alias de las clases
que Rayito sí lanza (`DiskFullException`, `CapacityException`).
`TemplateException` y `BuildException` también son las nativas: lo que lanza
`Template.build()` (nativo o del shim) lo atrapan por igual
`except rayito.BuildException` y `except rayito.e2b.BuildException`. A
diferencia de E2B, las dos son `SandboxException`. `SecretException` y `SecretNotFoundException`
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
    BuildException,
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
    TemplateException,
    TimeoutException,
    UnimplementedError,
    VolumeException,
    VolumeNotFoundException,
    VolumePathNotFoundException,
)

COMPAT_DOC_PATH = "docs/site/docs/e2b-compat.md"

NotEnoughSpaceException = DiskFullException
ServiceBusyException = CapacityException


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
