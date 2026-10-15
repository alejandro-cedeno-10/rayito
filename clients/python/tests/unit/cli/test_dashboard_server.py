"""Seguridad del servidor local de `rayito dashboard`: sin sesión no responde
nada; el token de arranque se canjea una sola vez por una cookie `HttpOnly`;
`Host` y `Origin` ajenos se rechazan (DNS rebinding, POST entre sitios); todo
POST exige JSON y el token CSRF; sólo lectura por defecto; la SPA no usa
nada que la CSP bloquee; y ni el token ni las peticiones se registran."""

from __future__ import annotations

import json
import logging
import re
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from rayito._dashboard._api._contract import API_PREFIX
from rayito._dashboard._theme import PNG_SIGNATURE, Appearance, parse_logo, parse_theme
from rayito._limits import DASHBOARD_REQUEST_MAX_BODY_BYTES, DASHBOARD_START_TOKEN_TTL_SECONDS
from rayito.cli._dashboard_server import (
    CONTENT_SECURITY_POLICY,
    CSRF_HEADER,
    LOOPBACK,
    SessionGuard,
    load_static_files,
)

from ..conftest import FakeClock
from ..dashboard_support import LocalClient, Rig, validate
from ..fake_dashboard import FakePlane, list_item, sandbox_id

pytestmark = pytest.mark.usefixtures("clean_catalog")

STATIC_ROOT = Path(__file__).resolve().parents[3] / "src" / "rayito" / "_dashboard" / "_static"
JSON_HEADERS = {"Content-Type": "application/json"}
FORBIDDEN_JS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "eval(",
    "new Function",
    "document.write",
)


@pytest.fixture
def rig() -> Rig:
    return Rig(FakePlane([list_item(1), list_item(2)]), allow_actions=True)


@pytest.fixture
def local(rig: Rig) -> Iterator[LocalClient]:
    client = LocalClient(rig.api)
    yield client
    client.close()


def writes(rig: Rig) -> list[str]:
    return [
        name
        for name in rig.plane.operations()
        if name.endswith("_microvm") and name != "list_microvms"
    ]


# ------------------------------------------------------------ session / token


def test_the_server_only_listens_on_loopback(local: LocalClient) -> None:
    assert local.server._server.server_address[0] == LOOPBACK


def test_the_start_token_is_swapped_for_a_strict_httponly_cookie(rig: Rig) -> None:
    client = LocalClient(rig.api, sign_in=False)
    try:
        status, headers, _ = client.raw("GET", client.start_target, sign=False)
    finally:
        client.close()
    assert status == 303
    assert headers["Location"] == "/"
    assert headers["Referrer-Policy"] == "no-referrer"
    cookie = headers["Set-Cookie"]
    assert {"HttpOnly", "SameSite=Strict", "Path=/"} <= set(cookie.split("; "))
    assert "Secure" not in cookie


def test_the_start_token_works_once(rig: Rig) -> None:
    client = LocalClient(rig.api)
    try:
        status, _, body = client.raw("GET", client.start_target, sign=False)
    finally:
        client.close()
    assert status == 401
    assert json.loads(body)["error"]["code"] == "unauthorized"


def test_a_wrong_start_token_does_not_burn_the_real_one(rig: Rig) -> None:
    from rayito.cli._dashboard_server import LocalDashboard

    server = LocalDashboard(rig.api)
    try:
        assert server.guard.redeem("not-the-token") is None
        assert server.guard.redeem(server.guard.start_token) is not None
    finally:
        server.shutdown()


def test_an_expired_start_token_is_refused() -> None:
    clock = FakeClock()
    guard = SessionGuard(1234, clock=clock)
    token = guard.start_token
    clock.advance(DASHBOARD_START_TOKEN_TTL_SECONDS + 1)
    assert guard.redeem(token) is None
    assert guard.has_session(f"{guard.cookie_name}=anything") is False


@pytest.mark.parametrize(
    "path", ["/", "/index.html", "/js/app.js", f"{API_PREFIX}/meta", f"{API_PREFIX}/session"]
)
def test_nothing_answers_without_the_session_cookie(local: LocalClient, path: str) -> None:
    status, _, body = local.raw("GET", path, sign=False)
    assert status == 401
    validate(json.loads(body), "error")


def test_a_forged_or_malformed_cookie_is_not_a_session(local: LocalClient) -> None:
    name = local.server.guard.cookie_name
    for cookie in (f"{name}=forged", "garbage", f"{name}", ""):
        status, _, _ = local.raw(
            "GET", f"{API_PREFIX}/meta", headers={"Cookie": cookie}, sign=False
        )
        assert status == 401


# ----------------------------------------------------------------- Host/Origin


def test_the_loopback_names_with_the_port_are_admitted(local: LocalClient) -> None:
    for host in (f"127.0.0.1:{local.port}", f"localhost:{local.port}"):
        status, _, _ = local.raw("GET", f"{API_PREFIX}/meta", headers={"Host": host})
        assert status == 200


@pytest.mark.parametrize(
    "host", ["evil.example", "evil.example:{port}", "127.0.0.1", "127.0.0.1:1", "0.0.0.0:{port}"]
)
def test_a_foreign_host_is_a_misdirected_request_even_with_a_valid_session(
    local: LocalClient, host: str
) -> None:
    status, _, body = local.raw(
        "GET", f"{API_PREFIX}/meta", headers={"Host": host.format(port=local.port)}
    )
    assert status == 421
    assert json.loads(body)["error"]["code"] == "misdirected_request"


@pytest.mark.parametrize(
    "origin", ["http://evil.example", "null", "https://127.0.0.1:{port}", "http://127.0.0.1"]
)
def test_a_foreign_origin_is_forbidden(local: LocalClient, origin: str) -> None:
    status, _, body = local.raw(
        "GET", f"{API_PREFIX}/meta", headers={"Origin": origin.format(port=local.port)}
    )
    assert status == 403
    assert json.loads(body)["error"]["code"] == "forbidden"


def test_the_own_origin_is_admitted(local: LocalClient) -> None:
    status, _, _ = local.raw(
        "GET", f"{API_PREFIX}/meta", headers={"Origin": f"http://127.0.0.1:{local.port}"}
    )
    assert status == 200


# ------------------------------------------------------------- Sec-Fetch-Site


@pytest.mark.parametrize("site", ["same-site", "cross-site", "Same-Site"])
def test_a_same_site_or_cross_site_request_without_origin_is_forbidden(
    local: LocalClient, site: str
) -> None:
    """Un `<img>` de la web que sirve un sandbox en `127.0.0.1:<otro puerto>`:
    comparte cookie, es `same-site` y no manda `Origin`."""
    status, _, body = local.raw("GET", f"{API_PREFIX}/meta", headers={"Sec-Fetch-Site": site})
    assert status == 403
    assert json.loads(body)["error"]["code"] == "forbidden"


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_the_own_spa_and_a_typed_navigation_are_admitted(local: LocalClient, site: str) -> None:
    status, _, _ = local.raw("GET", f"{API_PREFIX}/meta", headers={"Sec-Fetch-Site": site})
    assert status == 200


# --------------------------------------------------------- paid GET and CSRF


@pytest.fixture
def paid() -> Iterator[tuple[LocalClient, Rig]]:
    rig = Rig(FakePlane([list_item(1)]), cost_explorer=True)
    client = LocalClient(rig.api)
    yield client, rig
    client.close()


@pytest.mark.parametrize("query", ["billed=true", "billed=true&refresh=true"])
def test_the_billed_cost_get_needs_the_csrf_token_and_calls_nothing_without_it(
    paid: tuple[LocalClient, Rig], query: str
) -> None:
    client, rig = paid
    status, _, body = client.raw("GET", f"{API_PREFIX}/cost?{query}")
    assert status == 403
    assert json.loads(body)["error"]["code"] == "csrf_failed"
    assert rig.cost_source.queries == []


def test_the_billed_cost_get_with_the_csrf_token_goes_through(
    paid: tuple[LocalClient, Rig],
) -> None:
    client, rig = paid
    status, _, _ = client.raw(
        "GET", f"{API_PREFIX}/cost?billed=true", headers={CSRF_HEADER: client.csrf}
    )
    assert status == 200
    assert len(rig.cost_source.queries) == 1


def test_the_free_cost_estimate_needs_no_csrf_token(paid: tuple[LocalClient, Rig]) -> None:
    client, rig = paid
    status, _, _ = client.raw("GET", f"{API_PREFIX}/cost")
    assert status == 200
    assert rig.cost_source.queries == []


# ----------------------------------------------------------------------- CSRF


def kill_request(client: LocalClient, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    return client.raw(
        "POST",
        f"{API_PREFIX}/sandboxes/{sandbox_id(1)}/kill",
        headers=headers,
        body=b'{"confirm": true}',
    )


def test_a_post_without_the_csrf_token_is_refused_and_calls_nothing(
    local: LocalClient, rig: Rig
) -> None:
    status, _, body = kill_request(local, JSON_HEADERS)
    assert status == 403
    assert json.loads(body)["error"]["code"] == "csrf_failed"
    assert writes(rig) == []


def test_a_post_with_a_wrong_csrf_token_is_refused(local: LocalClient, rig: Rig) -> None:
    status, _, _ = kill_request(local, {**JSON_HEADERS, CSRF_HEADER: "guess"})
    assert status == 403
    assert writes(rig) == []


def test_a_post_that_is_not_json_is_refused_before_the_csrf_check(
    local: LocalClient, rig: Rig
) -> None:
    status, _, body = kill_request(local, {"Content-Type": "text/plain", CSRF_HEADER: local.csrf})
    assert status == 415
    assert json.loads(body)["error"]["code"] == "unsupported_media_type"
    assert writes(rig) == []


def test_a_cross_site_post_with_the_cookie_and_token_still_dies_on_origin(
    local: LocalClient, rig: Rig
) -> None:
    status, _, _ = kill_request(
        local, {**JSON_HEADERS, CSRF_HEADER: local.csrf, "Origin": "http://evil.example"}
    )
    assert status == 403
    assert writes(rig) == []


def test_a_valid_post_goes_through(local: LocalClient, rig: Rig) -> None:
    status, _, _ = kill_request(local, {**JSON_HEADERS, CSRF_HEADER: local.csrf})
    assert status == 202


def test_the_csrf_token_is_only_given_to_a_session(local: LocalClient) -> None:
    status, _, _ = local.raw("GET", f"{API_PREFIX}/session", sign=False)
    assert status == 401


def test_read_only_by_default_over_http() -> None:
    rig = Rig(FakePlane([list_item(1)]), allow_actions=False)
    client = LocalClient(rig.api)
    try:
        status, _, body = kill_request(client, {**JSON_HEADERS, CSRF_HEADER: client.csrf})
    finally:
        client.close()
    assert status == 403
    assert json.loads(body)["error"]["code"] == "actions_disabled"
    assert rig.plane.calls == []


# ------------------------------------------------------------- request limits


def test_an_oversized_body_is_refused_without_reading_it(local: LocalClient) -> None:
    status, _, body = local.raw(
        "POST",
        f"{API_PREFIX}/actions",
        headers={
            **JSON_HEADERS,
            CSRF_HEADER: local.csrf,
            "Content-Length": str(DASHBOARD_REQUEST_MAX_BODY_BYTES + 1),
        },
        body=b"",
    )
    assert status == 413
    assert json.loads(body)["error"]["code"] == "payload_too_large"


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_other_methods_are_not_allowed(local: LocalClient, method: str) -> None:
    status, _, body = local.raw(method, f"{API_PREFIX}/meta")
    assert status == 405
    assert json.loads(body)["error"]["code"] == "method_not_allowed"


def test_a_body_on_a_method_without_one_closes_the_connection_so_it_is_not_a_second_request(
    local: LocalClient,
) -> None:
    smuggled = (
        f"GET {API_PREFIX}/meta HTTP/1.1\r\nHost: {local.host}\r\nCookie: {local.cookie}\r\n\r\n"
    ).encode()
    request = (
        f"PUT {API_PREFIX}/meta HTTP/1.1\r\nHost: {local.host}\r\nCookie: {local.cookie}\r\n"
        f"Content-Length: {len(smuggled)}\r\n\r\n"
    ).encode() + smuggled
    with socket.create_connection((LOOPBACK, local.port), timeout=5) as connection:
        connection.sendall(request)
        received = b""
        while chunk := connection.recv(4096):
            received += chunk
    assert received.count(b"HTTP/1.1 ") == 1
    assert b"405" in received.split(b"\r\n", 1)[0]


# ---------------------------------------------------------------------- SPA


def test_the_spa_is_served_from_the_package_with_a_strict_csp(local: LocalClient) -> None:
    status, headers, body = local.raw("GET", "/")
    assert status == 200
    assert b"<rayito-app>" in body
    assert headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert "unsafe" not in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"


def test_every_static_file_has_a_content_type_and_unknown_paths_are_404(local: LocalClient) -> None:
    files = load_static_files()
    assert {"/", "/index.html", "/js/app.js", "/css/app.css", "/css/tokens.css"} <= set(files)
    for path, (_, content_type) in files.items():
        status, headers, _ = local.raw("GET", path)
        assert status == 200 and headers["Content-Type"] == content_type
    for path in ("/nope.js", "/../../etc/passwd", "/js/", "/%2e%2e/secret", "/_static/index.html"):
        status, _, _ = local.raw("GET", path)
        assert status == 404


def test_the_spa_uses_nothing_the_csp_blocks_or_that_could_inject_html() -> None:
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\ssrc=)", html)
    assert not re.search(r"<style|\sstyle=|\son\w+=", html)
    assert not re.search(r'(?:src|href)="https?://', html)
    for path in STATIC_ROOT.rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_JS:
            assert forbidden not in text, f"{path.name}: {forbidden}"
        assert not re.search(r'fetch\(\s*["\']https?://', text)
        assert "localStorage.setItem" not in text or "THEME_KEY" in text


def test_the_spa_never_reads_or_stores_secrets() -> None:
    for path in STATIC_ROOT.rglob("*.js"):
        text = path.read_text(encoding="utf-8").lower()
        assert "sessionstorage" not in text
        assert "document.cookie" not in text


# ---------------------------------------------------------------------- logs


def test_nothing_is_logged_or_printed(
    rig: Rig, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    client = LocalClient(rig.api)
    token = client.server.guard.csrf_token
    try:
        client.call("GET", "/meta")
        client.call("POST", f"/sandboxes/{sandbox_id(1)}/kill", body={"confirm": True})
        client.raw("GET", "/?token=not-valid", sign=False)
    finally:
        client.close()
    output = capfd.readouterr()
    assert output.out == "" and output.err == ""
    seen = caplog.text
    assert token not in seen and "token=" not in seen
    assert "GET" not in seen


# --------------------------------------------------------------------- tema


def test_theme_css_is_always_served_and_empty_without_a_user_theme(local: LocalClient) -> None:
    status, headers, body = local.raw("GET", "/theme.css")
    assert status == 200 and body == b""
    assert headers["Content-Type"].startswith("text/css")
    assert headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert local.raw("GET", "/theme/logo")[0] == 404
    _, meta = local.call("GET", "/meta")
    assert meta["theme"] == {"name": "rayito", "logo": False}


def test_a_user_theme_and_logo_are_served_same_origin_under_the_same_csp(rig: Rig) -> None:
    look = Appearance(
        theme=parse_theme('{"version": 1, "name": "demo", "dark": {"bg": "#101010"}}', fmt="json"),
        logo=parse_logo(b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'),
    )
    client = LocalClient(rig.api, appearance=look)
    try:
        _, headers, body = client.raw("GET", "/theme.css")
        assert b"--bg: #101010;" in body and headers["Content-Type"].startswith("text/css")
        assert headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
        status, logo_headers, logo = client.raw("GET", "/theme/logo")
        assert status == 200 and logo.startswith(b"<svg")
        assert logo_headers["Content-Type"] == "image/svg+xml"
        assert "sandbox" in logo_headers["Content-Security-Policy"]
        assert logo_headers["X-Content-Type-Options"] == "nosniff"
    finally:
        client.close()


def test_theme_and_logo_need_a_session(rig: Rig) -> None:
    look = Appearance(logo=parse_logo(PNG_SIGNATURE + b"x"))
    client = LocalClient(rig.api, appearance=look, sign_in=False)
    try:
        assert client.raw("GET", "/theme.css", sign=False)[0] == 401
        assert client.raw("GET", "/theme/logo", sign=False)[0] == 401
    finally:
        client.close()


def test_the_csp_has_no_remote_sources_and_the_spa_links_the_theme_sheet() -> None:
    assert "http" not in CONTENT_SECURITY_POLICY and "unsafe" not in CONTENT_SECURITY_POLICY
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    assert html.index("/css/tokens.css") < html.index("/theme.css")
