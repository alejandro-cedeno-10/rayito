"""Soporte de los tests de la API y del servidor local del panel: un
`DashboardApi` armado con fakes y dos clientes con la misma interfaz, uno
que llama al núcleo y otro que habla HTTP con el servidor local (sesión,
cookie y CSRF incluidos). Los tests de contrato corren contra los dos."""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import jsonschema

from rayito._dashboard._api._contract import (
    API_PREFIX,
    ApiRequest,
    LogLine,
    LogPage,
)
from rayito._dashboard._api._router import DashboardApi
from rayito._dashboard._cost import UsageCost
from rayito._dashboard._domain import CostGroup, CostPeriod, CostQueryResult
from rayito._dashboard._fleet import Fleet
from rayito._dashboard._quotas import Quotas
from rayito._dashboard._theme import Appearance
from rayito.cli._dashboard_server import CSRF_HEADER, LocalDashboard

from .conftest import FakeClock
from .fake_dashboard import (
    NOW,
    FakeCostSource,
    FakeEvents,
    FakeImageCatalog,
    FakePlane,
    FakeQuotaSource,
    applied_defaults,
)

SCHEMA_PATH = Path(__file__).resolve().parents[4] / "testdata" / "dashboard" / "api-v1.schema.json"
SCHEMA: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
JOB_TIMEOUT_SECONDS = 5.0
JOB_POLL_SECONDS = 0.01

BILLED_RESULT = CostQueryResult(
    (
        CostPeriod(
            date(2026, 10, 1),
            date(2026, 10, 14),
            True,
            (
                CostGroup("Lambda-MicroVM-vCPU-Second-ARM", 0.5, 100.0, "vCPU-Seconds"),
                CostGroup("Requests", 9.0, 5.0, "Requests"),
            ),
        ),
    ),
    requests=1,
)


class FakeLogs:
    def __init__(
        self, lines: Sequence[LogLine] = (), *, truncated: bool = False, fallback: bool = False
    ) -> None:
        self.lines = tuple(lines)
        self.truncated = truncated
        self.fallback = fallback
        self.reads: list[tuple[str, int]] = []

    def read(self, sandbox_id: str, *, limit: int) -> LogPage:
        self.reads.append((sandbox_id, limit))
        return LogPage(self.lines, self.truncated, self.fallback)


class Rig:
    """Todo lo que un test quiere inspeccionar de un `DashboardApi`."""

    def __init__(
        self,
        plane: FakePlane,
        *,
        allow_actions: bool = False,
        cost_explorer: bool = False,
        events: FakeEvents | None = None,
        logs: FakeLogs | None = None,
        quota_source: FakeQuotaSource | None = None,
    ) -> None:
        self.clock = FakeClock()
        self.plane = plane
        self.images = FakeImageCatalog()
        self.quota_source = quota_source or FakeQuotaSource(applied_defaults(memory_gb=100))
        self.cost_source = FakeCostSource(BILLED_RESULT)
        fleet = Fleet(
            control_plane=plane,
            images=self.images,
            events=events,
            allow_actions=allow_actions,
            monotonic=self.clock,
            sleep=self.clock.sleep,
        )
        self.api = DashboardApi(
            fleet=fleet,
            quotas=Quotas(control_plane=plane, source=self.quota_source),
            cost=UsageCost(
                control_plane=plane,
                source=self.cost_source,
                clock=lambda: NOW,
                monotonic=self.clock,
            ),
            region=plane.region,
            logs=logs,
            events_configured=events is not None,
            cost_explorer=cost_explorer,
            monotonic=self.clock,
        )

    def wait_for_job(self, client: Any, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            _, body = client.call("GET", f"/actions/{job_id}")
            if body["job"]["status"] == "finished":
                job: dict[str, Any] = body["job"]
                return job
            time.sleep(JOB_POLL_SECONDS)
        raise AssertionError("el trabajo no terminó")


def validate(document: Any, definition: str) -> None:
    jsonschema.validate(document, {"$ref": f"#/$defs/{definition}", "$defs": SCHEMA["$defs"]})


class CoreClient:
    """Llama a `DashboardApi.handle` sin transporte."""

    def __init__(self, api: DashboardApi) -> None:
        self._api = api

    def call(
        self,
        method: str,
        path: str,
        query: Mapping[str, str] | None = None,
        body: Any = None,
    ) -> tuple[int, dict[str, Any]]:
        request = ApiRequest(
            method,
            f"{API_PREFIX}{path}",
            {name: [value] for name, value in (query or {}).items()},
            b"" if body is None else json.dumps(body).encode(),
        )
        response = self._api.handle(request)
        return response.status, response.body

    def close(self) -> None:
        self._api.close()


class LocalClient:
    """Habla HTTP con `LocalDashboard` en loopback: canjea el token de
    arranque por la cookie y pide el token CSRF, como la SPA."""

    def __init__(
        self, api: DashboardApi, *, sign_in: bool = True, appearance: Appearance | None = None
    ) -> None:
        self.server = LocalDashboard(api, appearance=appearance)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        self.port = self.server.port
        self.host = f"127.0.0.1:{self.port}"
        self.cookie = ""
        self.csrf = ""
        start = urlsplit(self.server.url)
        self.start_target = f"{start.path}?{start.query}"
        if sign_in:
            self._sign_in()

    def _sign_in(self) -> None:
        status, headers, _ = self.raw("GET", self.start_target, sign=False)
        assert status == 303
        self.cookie = headers["Set-Cookie"].split(";")[0]
        _, _, body = self.raw("GET", f"{API_PREFIX}/session")
        self.csrf = json.loads(body)["csrfToken"]

    def raw(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        sign: bool = True,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        sent = {"Host": self.host}
        if sign and self.cookie:
            sent["Cookie"] = self.cookie
        sent.update(headers or {})
        connection.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        for name, value in sent.items():
            connection.putheader(name, value)
        if body is not None and "Content-Length" not in sent:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    def call(
        self,
        method: str,
        path: str,
        query: Mapping[str, str] | None = None,
        body: Any = None,
    ) -> tuple[int, dict[str, Any]]:
        target = f"{API_PREFIX}{path}" + (f"?{urlencode(query)}" if query else "")
        headers: dict[str, str] = {CSRF_HEADER: self.csrf}
        payload: bytes | None = None
        if method == "POST":
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body if body is not None else {}).encode()
        status, _, data = self.raw(method, target, headers=headers, body=payload)
        return status, json.loads(data)

    def close(self) -> None:
        self.server.shutdown()
