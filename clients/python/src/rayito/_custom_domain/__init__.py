"""`m15-custom-domain` (ADR-024): dominio propio sobre CloudFront.

`CustomDomain`/`AsyncCustomDomain` son la API pública (reexportadas desde
`rayito`); el resto de este paquete (`_domain`, `_kvs`) es implementación.
"""

from __future__ import annotations

from rayito._custom_domain._service import CustomDomain, CustomDomainRoute
from rayito._custom_domain._service_async import AsyncCustomDomain

__all__ = ["AsyncCustomDomain", "CustomDomain", "CustomDomainRoute"]
