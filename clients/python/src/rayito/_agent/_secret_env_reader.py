"""La lectura de los secretos de `secret_env` en MicroVMs (ADR-033 D4): el
SDK los lee con las credenciales de quien despliega, con una `SecretCache`
sobre `SecretStore(prefix="")` (nombres completos, como `--readable-secret`),
y el host los recibe como variables de su proceso. Construirla no llama a AWS:
el cliente de Secrets Manager nace en la primera lectura, y sólo si el
proyecto usa `secret_env`.
"""

from __future__ import annotations

from typing import Any, Final

from rayito._agent._secret_env_domain import AsyncSecretReader, SecretReader
from rayito._secrets import SecretCache, SecretRef

#: Los nombres de `secret_env` son completos: sin el prefijo `rayito/` de
#: `SecretStore`.
FULL_NAME_PREFIX: Final = ""


def _cache(region: str | None, session: Any) -> SecretCache:
    return SecretCache(region=region, session=session, prefix=FULL_NAME_PREFIX)


def secret_reader_for(region: str | None, session: Any = None) -> SecretReader:
    """Lee un secreto (nombre completo o ARN, y etiqueta de versión) una vez
    por despliegue y región."""
    cache = _cache(region, session)

    def read(secret: str, version_stage: str | None) -> str:
        return cache.get(SecretRef(secret, version_stage=version_stage))

    return read


def async_secret_reader_for(region: str | None, session: Any = None) -> AsyncSecretReader:
    """`secret_reader_for` para asyncio."""
    cache = _cache(region, session)

    async def read(secret: str, version_stage: str | None) -> str:
        return await cache.aget(SecretRef(secret, version_stage=version_stage))

    return read
