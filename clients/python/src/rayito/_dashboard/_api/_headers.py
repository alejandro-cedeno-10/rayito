"""Las cabeceras de seguridad de toda respuesta del panel, la misma en las
dos cáscaras (la local y la Lambda de la pila `dashboard`): CSP sin
`unsafe-inline` ni `unsafe-eval` (la SPA son módulos y hojas de estilo del
propio origen), `nosniff`, sin caché, sin marcos y sin `Referer`. La cáscara
de la pila añade a `connect-src` el dominio del Hosted UI de Cognito, la
única conexión externa que hace la SPA.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

SELF: Final = "'self'"
NONE: Final = "'none'"


#: El logo se sirve con su propia CSP, aún más cerrada: aunque alguien abra
#: `/theme/logo` como documento, un SVG no ejecuta nada ni carga nada.
LOGO_HEADERS: Final[dict[str, str]] = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
}


def content_security_policy(connect_src: Iterable[str] = ()) -> str:
    connect = " ".join((SELF, *connect_src))
    return (
        f"default-src {NONE}; script-src {SELF}; style-src {SELF}; connect-src {connect}; "
        f"img-src {SELF}; base-uri {NONE}; form-action {NONE}; frame-ancestors {NONE}"
    )


def security_headers(connect_src: Iterable[str] = ()) -> dict[str, str]:
    return {
        "Content-Security-Policy": content_security_policy(connect_src),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
        "X-Frame-Options": "DENY",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Cross-Origin-Opener-Policy": "same-origin",
    }
