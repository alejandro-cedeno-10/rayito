"""Los casos de `testdata/dashboard/api-v1-cases.json` contra el núcleo de
`/api/v1`. El mismo fichero lo ejecuta TypeScript (`dashboard-api-cases.test.ts`):
cada respuesta valida contra `api-v1.schema.json` y coincide con el subconjunto
del cuerpo que fija el vector."""

from __future__ import annotations

import json
from typing import Any

import pytest

from rayito._dashboard._api._contract import API_PREFIX

from .dashboard_support import CoreClient, FakeLogs, Rig, validate
from .dashboard_vectors import build_rig, expand, load_vectors
from .fake_dashboard import FakeEvents

pytestmark = pytest.mark.usefixtures("clean_catalog")

CASES = load_vectors("api-v1-cases.json")["cases"]
WRITE_OPERATIONS = {
    "terminate_microvm": "terminateMicrovm",
    "suspend_microvm": "suspendMicrovm",
    "resume_microvm": "resumeMicrovm",
}


def assert_subset(actual: Any, expected: Any, where: str = "body") -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict), where
        for key, value in expected.items():
            assert key in actual, f"{where}.{key}"
            assert_subset(actual[key], value, f"{where}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), where
        for index, value in enumerate(expected):
            assert_subset(actual[index], value, f"{where}[{index}]")
    else:
        assert actual == expected and isinstance(actual, bool) == isinstance(expected, bool), where


def writes(rig: Rig) -> list[str]:
    return [WRITE_OPERATIONS[name] for name in rig.plane.operations() if name in WRITE_OPERATIONS]


def check_world(
    rig: Rig, logs: FakeLogs | None, events: FakeEvents | None, expect: dict[str, Any]
) -> None:
    if "writes" in expect:
        assert writes(rig) == expect["writes"]
    if expect.get("noPlaneCalls"):
        assert rig.plane.calls == []
    if "costQueries" in expect:
        assert len(rig.cost_source.queries) == expect["costQueries"]
    if "listings" in expect:
        assert rig.plane.operations().count("list_microvms") == expect["listings"]
    if "eventsQuery" in expect:
        assert events is not None
        last = events.calls[-1]
        assert {
            "sandboxId": last["sandbox_id"],
            "types": None if last["types"] is None else list(last["types"]),
            "limit": last["limit"],
            "order": last["order"],
        } == expect["eventsQuery"]
    if "logRead" in expect:
        assert logs is not None
        sandbox, limit = logs.reads[-1]
        assert {"sandboxId": sandbox, "limit": limit} == expect["logRead"]


def check_response(status: int, body: dict[str, Any], expect: dict[str, Any]) -> None:
    assert status == expect["status"], body
    if "schema" in expect:
        validate(body, expect["schema"])
    if "body" in expect:
        assert_subset(body, expand(expect["body"]))
    text = json.dumps(body)
    for forbidden in expect.get("absentText", []):
        assert forbidden not in text


def send(client: CoreClient, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    query = {name: values for name, values in request.get("query", {}).items()}
    if "rawBody" in request:
        from rayito._dashboard._api._contract import ApiRequest

        response = client._api.handle(
            ApiRequest(
                request["method"],
                f"{API_PREFIX}{request['path']}",
                {},
                request["rawBody"].encode(),
            )
        )
        return response.status, response.body
    return client.call_with_lists(
        request["method"], request["path"], query, expand(request.get("body"))
    )


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_the_core_answers_the_shared_cases(case: dict[str, Any]) -> None:
    rig, logs, events = build_rig(case["world"])
    client = CoreClient(rig.api)
    last_body: dict[str, Any] = {}
    try:
        for step in case["steps"]:
            if step.get("waitForJob"):
                job_id = last_body["job"]["jobId"]
                rig.wait_for_job(client, job_id)
                status, last_body = client.call("GET", f"/actions/{job_id}")
            else:
                status, last_body = send(client, step["request"])
            check_response(status, last_body, step["expect"])
            check_world(rig, logs, events, step["expect"])
    finally:
        client.close()
