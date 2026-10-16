"""La matriz de `testdata/dashboard/guard-vectors.json` contra `LocalApp.respond`
(sin sockets): sesión, `Host`, `Origin`, `Sec-Fetch-Site`, CSRF, tipo de
contenido, métodos y rutas. El mismo fichero lo ejecuta TypeScript
(`dashboard-guard-vectors.test.ts`), así las dos cáscaras locales dan la misma
respuesta a cada petición."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import pytest

from rayito.cli._dashboard_server import LocalDashboard

from ..dashboard_support import Rig
from ..dashboard_vectors import build_rig, load_vectors
from ..test_dashboard_api_cases import WRITE_OPERATIONS

pytestmark = pytest.mark.usefixtures("clean_catalog")

CASES = load_vectors("guard-vectors.json")["cases"]


class Scenario:
    def __init__(self, case: dict[str, Any]) -> None:
        self.rig, _, _ = build_rig(case["world"])
        self.server = LocalDashboard(self.rig.api)
        guard = self.server.guard
        start = urlsplit(self.server.url)
        self.values = {
            "port": str(self.server.port),
            "host": f"127.0.0.1:{self.server.port}",
            "cookieName": guard.cookie_name,
            "csrf": guard.csrf_token,
            "startTarget": f"{start.path}?{start.query}",
        }
        self.cookie = ""
        if case["session"] == "signed":
            self.cookie = f"{guard.cookie_name}={guard.redeem(guard.start_token)}"
        elif case.get("redeemFirst"):
            guard.redeem(guard.start_token)

    def fill(self, text: str) -> str:
        for name, value in self.values.items():
            text = text.replace("{" + name + "}", value)
        return text

    def headers(self, declared: dict[str, str | None]) -> dict[str, str]:
        headers = {"host": self.values["host"]}
        if self.cookie:
            headers["cookie"] = self.cookie
        for name, value in declared.items():
            if value is None:
                headers.pop(name.lower(), None)
            else:
                headers[name.lower()] = self.fill(value)
        return headers


@pytest.fixture
def scenario(request: pytest.FixtureRequest) -> Iterator[Scenario]:
    built = Scenario(request.param)
    yield built
    built.server.shutdown()


def writes(rig: Rig) -> list[str]:
    return [WRITE_OPERATIONS[name] for name in rig.plane.operations() if name in WRITE_OPERATIONS]


@pytest.mark.parametrize("scenario", CASES, ids=[case["id"] for case in CASES], indirect=True)
def test_the_local_shell_answers_the_shared_guard_vectors(
    scenario: Scenario, request: pytest.FixtureRequest
) -> None:
    case = request.node.callspec.params["scenario"]
    spec = case["request"]
    body = spec.get("body")
    reply = scenario.server.app.respond(
        spec["method"],
        scenario.fill(spec["target"]),
        scenario.headers(spec["headers"]),
        None if body is None else body.encode(),
    )
    expect = case["expect"]
    assert reply.status == expect["status"]
    if "errorCode" in expect:
        assert json.loads(reply.body)["error"]["code"] == expect["errorCode"]
    for name, value in expect.get("headers", {}).items():
        if value is None:
            assert name not in reply.headers
        else:
            assert reply.headers[name] == value
    attributes = {part.strip() for part in reply.headers.get("Set-Cookie", "").split(";")}
    assert set(expect.get("setCookieHas", [])) <= attributes
    assert not set(expect.get("setCookieLacks", [])) & attributes
    for key in expect.get("bodyHas", []):
        assert key in json.loads(reply.body)
    if "bodyText" in expect:
        assert reply.body.decode() == expect["bodyText"]
    if "bodyContains" in expect:
        assert expect["bodyContains"] in reply.body.decode()
    if "contentTypePrefix" in expect:
        assert reply.content_type.startswith(expect["contentTypePrefix"])
    if "writes" in expect:
        assert writes(scenario.rig) == expect["writes"]
    if expect.get("noPlaneCalls"):
        assert scenario.rig.plane.calls == []
    if "costQueries" in expect:
        assert len(scenario.rig.cost_source.queries) == expect["costQueries"]
