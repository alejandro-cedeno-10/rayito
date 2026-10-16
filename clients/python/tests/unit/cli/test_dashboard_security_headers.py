"""Las cabeceras de seguridad de `testdata/dashboard/security-headers.json`,
exactas: las que construye `_headers.py` y las que el servidor local manda en
cada respuesta (la SPA, la API, el tema y el logo). TypeScript ejecuta el mismo
fichero (`dashboard-security-headers.test.ts`)."""

from __future__ import annotations

import pytest

from rayito._dashboard._api._headers import LOGO_HEADERS, content_security_policy, security_headers
from rayito._dashboard._theme import Appearance, parse_logo

from ..dashboard_support import LocalClient, Rig
from ..dashboard_vectors import load_vectors
from ..fake_dashboard import FakePlane, list_item

pytestmark = pytest.mark.usefixtures("clean_catalog")

VECTORS = load_vectors("security-headers.json")
LOGO_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'


def test_the_builders_give_the_shared_headers() -> None:
    assert security_headers() == VECTORS["headers"]
    assert VECTORS["logoHeaders"] == LOGO_HEADERS
    extra = VECTORS["withConnectSrc"]
    assert content_security_policy(extra["connectSrc"]) == extra["contentSecurityPolicy"]


def test_every_local_response_carries_the_shared_headers() -> None:
    rig = Rig(FakePlane([list_item(1)]))
    client = LocalClient(rig.api, appearance=Appearance(logo=parse_logo(LOGO_SVG)))
    try:
        for target in ("/", "/js/app.js", "/theme.css", "/api/v1/meta", "/api/v1/nope", "/nope"):
            _, headers, _ = client.raw("GET", target)
            for name, value in VECTORS["headers"].items():
                assert headers[name] == value, (target, name)
        _, logo_headers, _ = client.raw("GET", "/theme/logo")
        for name, value in VECTORS["logoHeaders"].items():
            assert logo_headers[name] == value
        _, unauthorized, _ = client.raw("GET", "/api/v1/meta", sign=False)
        assert (
            unauthorized["Content-Security-Policy"] == VECTORS["headers"]["Content-Security-Policy"]
        )
    finally:
        client.close()
