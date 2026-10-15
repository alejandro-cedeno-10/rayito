"""Contrato de `/api/v1`: los mismos tests corren contra el núcleo
(`DashboardApi.handle`) y contra el servidor local por HTTP, y cada respuesta
valida contra `testdata/dashboard/api-v1.schema.json`. Además: sólo lectura
por defecto (ninguna llamada de escritura a AWS), coste facturado apagado
salvo `cost_explorer`, errores por código sin el mensaje de AWS, y una cola
que respeta el TPS."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from rayito._dashboard._api._contract import DEPLOYMENT_LOCAL, ERROR_MESSAGES, LogLine
from rayito._dashboard._api._router import DashboardApi
from rayito._lifecycle_events._domain import EventRecord
from rayito._limits import (
    API_TPS,
    COST_CACHE_TTL_SECONDS,
    DASHBOARD_BULK_MAX_SANDBOXES,
    DASHBOARD_READ_CACHE_SECONDS,
    MICROVM_ID_MAX_LENGTH,
)
from rayito.exceptions import AuthenticationException, InvalidArgumentException

from .conftest import IMAGE_ARN
from .dashboard_support import (
    CoreClient,
    FakeLogs,
    LocalClient,
    Rig,
    validate,
)
from .fake_dashboard import FakePlane, RaisingError, list_item, sandbox_id

pytestmark = pytest.mark.usefixtures("clean_catalog")

Client = CoreClient | LocalClient
Connect = Callable[[Rig], Client]
WRITES = {"terminate_microvm", "suspend_microvm", "resume_microvm"}
EPSILON = 1e-9
SECRET_FIELDS = ("envs", "runHookPayload", "token", "jwe", "secret")


@pytest.fixture(params=["core", "local"])
def connect(request: pytest.FixtureRequest) -> Iterator[Connect]:
    opened: list[Client] = []

    def open_client(rig: Rig) -> Client:
        client: Client = CoreClient(rig.api) if request.param == "core" else LocalClient(rig.api)
        opened.append(client)
        return client

    yield open_client
    for client in opened:
        client.close()


def three_sandboxes() -> FakePlane:
    return FakePlane([list_item(1), list_item(2), list_item(3, state="SUSPENDED")])


def writes(plane: FakePlane) -> list[str]:
    return [name for name in plane.operations() if name in WRITES]


def event(number: int) -> EventRecord:
    return EventRecord(
        f"evt-{number}",
        sandbox_id(number),
        "terminated",
        "idle",
        1,
        1_760_000_000_000,
        IMAGE_ARN,
        "1.0",
    )


def test_meta_reports_the_mode_and_the_constants_the_spa_needs(connect: Connect) -> None:
    status, body = connect(Rig(three_sandboxes())).call("GET", "/meta")
    assert status == 200
    validate(body, "meta")
    assert body["deployment"] == DEPLOYMENT_LOCAL
    assert body["allowActions"] is False
    assert body["costExplorer"]["enabled"] is False
    assert body["bulkMaxSandboxes"] == DASHBOARD_BULK_MAX_SANDBOXES
    assert body["costExplorer"]["cacheTtlSeconds"] == COST_CACHE_TTL_SECONDS


def test_the_read_cache_keeps_following_the_injected_clock_after_invalidate() -> None:
    rig = Rig(three_sandboxes())

    def listings() -> int:
        return rig.plane.operations().count("list_microvms")

    client = CoreClient(rig.api)
    client.call("GET", "/sandboxes")
    rig.api.invalidate()
    client.call("GET", "/sandboxes")
    client.call("GET", "/sandboxes")
    assert listings() == 2
    rig.clock.advance(DASHBOARD_READ_CACHE_SECONDS)
    client.call("GET", "/sandboxes")
    assert listings() == 3


def test_a_read_that_started_before_an_action_finished_does_not_refill_the_cache() -> None:
    rig = Rig(three_sandboxes())
    original = rig.plane.list_microvms
    invalidated = False

    def listing_while_an_action_finishes(*args: Any, **kwargs: Any) -> Any:
        nonlocal invalidated
        result = original(*args, **kwargs)
        if not invalidated:
            invalidated = True
            rig.api.invalidate()
        return result

    rig.plane.list_microvms = listing_while_an_action_finishes  # type: ignore[method-assign]
    client = CoreClient(rig.api)
    client.call("GET", "/sandboxes")
    client.call("GET", "/sandboxes")
    assert rig.plane.operations().count("list_microvms") == 2


def test_sandboxes_list_with_memory_and_nothing_secret(connect: Connect) -> None:
    status, body = connect(Rig(three_sandboxes())).call("GET", "/sandboxes")
    assert status == 200
    validate(body, "sandboxes")
    assert [item["memoryMib"] for item in body["sandboxes"]] == [2048, 2048, 2048]
    text = str(body).lower()
    assert not any(name.lower() in text for name in SECRET_FIELDS)


def test_sandboxes_filter_by_state(connect: Connect) -> None:
    _, body = connect(Rig(three_sandboxes())).call("GET", "/sandboxes", {"state": "SUSPENDED"})
    assert [item["sandboxId"] for item in body["sandboxes"]] == [sandbox_id(3)]


def test_images_and_versions(connect: Connect) -> None:
    client = connect(Rig(three_sandboxes()))
    status, images = client.call("GET", "/images")
    validate(images, "images")
    assert status == 200 and images["images"][0]["name"] == "rayito-base"
    status, versions = client.call("GET", "/images/versions", {"image": "rayito-base"})
    validate(versions, "imageVersions")
    assert status == 200 and versions["versions"][0]["artifact"] == "artifact.zip"


def test_versions_need_an_image(connect: Connect) -> None:
    status, body = connect(Rig(three_sandboxes())).call("GET", "/images/versions")
    assert status == 400
    validate(body, "error")


def test_quotas_report_memory_warnings_and_the_increase_link(connect: Connect) -> None:
    rig = Rig(three_sandboxes())
    status, body = connect(rig).call("GET", "/quotas")
    assert status == 200
    validate(body, "quotas")
    assert body["memory"]["allocatedGb"] == 6
    assert body["memory"]["level"] == "ok"
    assert all(entry["increaseUrl"].startswith("https://") for entry in body["entries"])


def test_quotas_warn_near_the_limit(connect: Connect) -> None:
    from .fake_dashboard import FakeQuotaSource, applied_defaults

    rig = Rig(three_sandboxes(), quota_source=FakeQuotaSource(applied_defaults(memory_gb=6.2)))
    _, body = connect(rig).call("GET", "/quotas")
    validate(body, "quotas")
    assert body["memory"]["level"] == "critical"
    assert any(w["kind"] == "memory_critical" for w in body["warnings"])


def test_cost_gives_the_estimate_and_never_calls_cost_explorer_by_default(
    connect: Connect,
) -> None:
    rig = Rig(three_sandboxes())
    status, body = connect(rig).call("GET", "/cost")
    assert status == 200
    validate(body, "cost")
    assert body["estimated"]["label"] == "estimated"
    assert body["billed"] is None
    assert rig.cost_source.queries == []


def test_billed_cost_is_refused_without_the_option_and_costs_nothing(connect: Connect) -> None:
    rig = Rig(three_sandboxes())
    status, body = connect(rig).call("GET", "/cost", {"billed": "true"})
    assert status == 403
    validate(body, "error")
    assert body["error"]["code"] == "cost_explorer_disabled"
    assert rig.cost_source.queries == []


def test_billed_cost_is_labelled_and_cached(connect: Connect) -> None:
    rig = Rig(three_sandboxes(), cost_explorer=True)
    client = connect(rig)
    status, body = client.call("GET", "/cost", {"billed": "true"})
    assert status == 200
    validate(body, "cost")
    assert body["billed"]["label"] == "billed"
    assert body["billed"]["partial"] is True
    assert [line["component"] for line in body["billed"]["lines"]] == ["vcpu"]
    client.call("GET", "/cost", {"billed": "true"})
    assert len(rig.cost_source.queries) == 1


def test_events_are_off_until_a_stack_is_configured(connect: Connect) -> None:
    status, body = connect(Rig(three_sandboxes())).call("GET", "/events")
    assert status == 200
    validate(body, "events")
    assert body == {"configured": False, "events": []}


def test_events_come_from_the_history(connect: Connect) -> None:
    from .fake_dashboard import FakeEvents

    events = FakeEvents([event(1)])
    status, body = connect(Rig(three_sandboxes(), events=events)).call(
        "GET", "/events", {"sandboxId": sandbox_id(1), "limit": "5"}
    )
    assert status == 200
    validate(body, "events")
    assert body["events"][0]["type"] == "sandbox.lifecycle.terminated"
    assert events.calls == [
        {"sandbox_id": sandbox_id(1), "types": None, "limit": 5, "order": "desc"}
    ]


def test_events_reject_a_bad_limit(connect: Connect) -> None:
    from .fake_dashboard import FakeEvents

    status, body = connect(Rig(three_sandboxes(), events=FakeEvents())).call(
        "GET", "/events", {"limit": "0"}
    )
    assert status == 400
    validate(body, "error")


def test_logs_are_read_on_demand(connect: Connect) -> None:
    logs = FakeLogs([LogLine("2026-10-13T12:00:00.000Z", "hola", "stream")], truncated=True)
    client = connect(Rig(three_sandboxes(), logs=logs))
    status, body = client.call("GET", "/logs", {"sandboxId": sandbox_id(1)})
    assert status == 200
    validate(body, "logs")
    assert body["truncated"] is True and body["lines"][0]["message"] == "hola"
    assert logs.reads and logs.reads[0][0] == sandbox_id(1)


def test_logs_report_the_name_search_fallback(connect: Connect) -> None:
    logs = FakeLogs([LogLine("2026-10-13T12:00:00.000Z", "hola", "stream")], fallback=True)
    client = connect(Rig(three_sandboxes(), logs=logs))
    status, body = client.call("GET", "/logs", {"sandboxId": sandbox_id(1)})
    assert status == 200
    validate(body, "logs")
    assert body["fallback"] is True


def test_logs_are_absent_without_a_source(connect: Connect) -> None:
    status, body = connect(Rig(three_sandboxes())).call("GET", "/logs", {"sandboxId": "x"})
    assert status == 404
    validate(body, "error")


# ------------------------------------------------------- read-only by default


@pytest.mark.parametrize("action", ["kill", "suspend", "resume"])
def test_actions_are_refused_without_the_flag_and_make_no_aws_call(
    connect: Connect, action: str
) -> None:
    rig = Rig(three_sandboxes())
    client = connect(rig)
    status, body = client.call(
        "POST", f"/sandboxes/{sandbox_id(1)}/{action}", body={"confirm": True}
    )
    assert status == 403
    validate(body, "error")
    assert body["error"]["code"] == "actions_disabled"
    status, body = client.call(
        "POST", "/actions", body={"action": action, "sandboxIds": [sandbox_id(1)], "confirm": True}
    )
    assert status == 403 and body["error"]["code"] == "actions_disabled"
    assert rig.plane.calls == []


def test_actions_need_an_explicit_confirmation(connect: Connect) -> None:
    rig = Rig(three_sandboxes(), allow_actions=True)
    client = connect(rig)
    status, body = client.call("POST", f"/sandboxes/{sandbox_id(1)}/kill", body={})
    assert status == 400 and body["error"]["code"] == "confirmation_required"
    status, body = client.call(
        "POST", "/actions", body={"action": "kill", "sandboxIds": [sandbox_id(1)], "confirm": "yes"}
    )
    assert status == 400 and body["error"]["code"] == "confirmation_required"
    assert writes(rig.plane) == []


# ------------------------------------------------------------------- actions


def test_a_single_kill_runs_as_a_job(connect: Connect) -> None:
    rig = Rig(three_sandboxes(), allow_actions=True)
    client = connect(rig)
    status, body = client.call("POST", f"/sandboxes/{sandbox_id(2)}/kill", body={"confirm": True})
    assert status == 202
    validate(body, "jobDocument")
    job = rig.wait_for_job(client, body["job"]["jobId"])
    validate({"job": job}, "jobDocument")
    assert job["counts"] == {"done": 1, "unchanged": 0, "failed": 0}
    assert writes(rig.plane) == ["terminate_microvm"]


def test_a_bulk_failure_reports_only_the_aws_code_and_the_queue_continues(
    connect: Connect,
) -> None:
    rig = Rig(three_sandboxes(), allow_actions=True)
    rig.plane.fail[("suspend_microvm", sandbox_id(1))] = RaisingError("ThrottlingException")
    rig.plane.unchanged.add(("suspend_microvm", sandbox_id(2)))
    client = connect(rig)
    ids = [sandbox_id(1), sandbox_id(2), sandbox_id(3)]
    status, body = client.call(
        "POST", "/actions", body={"action": "suspend", "sandboxIds": ids, "confirm": True}
    )
    assert status == 202
    job = rig.wait_for_job(client, body["job"]["jobId"])
    assert job["counts"] == {"done": 1, "unchanged": 1, "failed": 1}
    assert [item["code"] for item in job["items"]] == ["ThrottlingException", None, None]
    assert "mensaje que no debe salir" not in str(job)
    status, listing = client.call("GET", "/actions")
    validate(listing, "jobsDocument")
    assert listing["jobs"][0]["jobId"] == job["jobId"]


def test_the_bulk_queue_is_paced_at_the_applied_tps(
    connect: Connect,
) -> None:
    rig = Rig(FakePlane([list_item(n) for n in range(1, 11)]), allow_actions=True)
    starts: list[float] = []
    original = rig.plane.suspend_microvm

    def timed(sandbox: str) -> bool:
        starts.append(rig.clock())
        return original(sandbox)

    rig.plane.suspend_microvm = timed  # type: ignore[method-assign,assignment]
    client = connect(rig)
    ids = [sandbox_id(n) for n in range(1, 11)]
    _, body = client.call(
        "POST", "/actions", body={"action": "suspend", "sandboxIds": ids, "confirm": True}
    )
    rig.wait_for_job(client, body["job"]["jobId"])
    tps = API_TPS["SuspendMicrovm"]
    assert len(starts) == 10
    gaps = [later - earlier for earlier, later in itertools.pairwise(starts[tps - 1 :])]
    assert all(gap >= 1 / tps - EPSILON for gap in gaps)
    assert starts[-1] - starts[0] >= (10 - tps) / tps - EPSILON


def test_the_plan_uses_the_applied_rate(connect: Connect) -> None:
    status, body = connect(Rig(three_sandboxes(), allow_actions=True)).call(
        "GET", "/actions/plan", {"action": "suspend", "count": "10"}
    )
    assert status == 200
    validate(body, "planDocument")
    assert body["plan"]["tps"] == API_TPS["SuspendMicrovm"]
    assert body["plan"]["estimatedSeconds"] == 10 / API_TPS["SuspendMicrovm"]


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "explode", "sandboxIds": ["x"], "confirm": True},
        {"action": "kill", "sandboxIds": "x", "confirm": True},
        {"action": "kill", "sandboxIds": [], "confirm": True},
        {"action": "kill", "sandboxIds": ["x" * (MICROVM_ID_MAX_LENGTH + 1)], "confirm": True},
        {"action": "kill", "sandboxIds": [1], "confirm": True},
    ],
)
def test_invalid_bulk_requests_fail_before_any_aws_call(
    connect: Connect, payload: dict[str, Any]
) -> None:
    rig = Rig(three_sandboxes(), allow_actions=True)
    status, body = connect(rig).call("POST", "/actions", body=payload)
    assert status == 400
    validate(body, "error")
    assert writes(rig.plane) == []


def test_a_bulk_request_over_the_cap_is_refused(connect: Connect) -> None:
    rig = Rig(three_sandboxes(), allow_actions=True)
    ids = [sandbox_id(n) for n in range(DASHBOARD_BULK_MAX_SANDBOXES + 1)]
    status, body = connect(rig).call(
        "POST", "/actions", body={"action": "kill", "sandboxIds": ids, "confirm": True}
    )
    assert status == 400 and body["error"]["code"] == "invalid_argument"
    assert writes(rig.plane) == []


# -------------------------------------------------------------------- errors


def test_unknown_routes_and_methods(connect: Connect) -> None:
    client = connect(Rig(three_sandboxes()))
    status, body = client.call("GET", "/nope")
    assert status == 404
    validate(body, "error")
    status, body = client.call("POST", "/quotas", body={})
    assert status in {403, 405}
    validate(body, "error")


def test_aws_failures_map_to_codes_without_the_aws_message(connect: Connect) -> None:
    plane = three_sandboxes()
    plane.list_microvms = lambda **_: (_ for _ in ()).throw(RaisingError("ServiceException"))  # type: ignore[method-assign]
    status, body = connect(Rig(plane)).call("GET", "/sandboxes")
    assert status == 502
    validate(body, "error")
    assert body["error"]["awsCode"] == "ServiceException"
    assert "mensaje que no debe salir" not in str(body)


def test_missing_permissions_are_a_403_with_a_fixed_message(connect: Connect) -> None:
    plane = three_sandboxes()
    plane.list_microvms = lambda **_: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AuthenticationException("detalle interno", aws_code="AccessDeniedException")
    )
    status, body = connect(Rig(plane)).call("GET", "/sandboxes")
    assert status == 403
    validate(body, "error")
    assert body["error"]["code"] == "aws_access_denied"
    assert "detalle interno" not in str(body)


def test_an_aws_validation_failure_carries_the_code_and_never_the_aws_message(
    connect: Connect,
) -> None:
    plane = three_sandboxes()
    plane.list_microvms = lambda **_: (_ for _ in ()).throw(  # type: ignore[method-assign]
        InvalidArgumentException(
            "arn:aws:lambda:us-east-1:123456789012:image/privada rechazada",
            aws_code="ValidationException",
        )
    )
    status, body = connect(Rig(plane)).call("GET", "/sandboxes")
    assert status == 400
    validate(body, "error")
    assert body["error"]["code"] == "invalid_argument"
    assert body["error"]["awsCode"] == "ValidationException"
    assert body["error"]["message"] == ERROR_MESSAGES["invalid_argument"]
    assert "123456789012" not in str(body)


def test_an_unexpected_failure_is_an_internal_error_with_no_detail(connect: Connect) -> None:
    plane = three_sandboxes()
    plane.list_microvms = lambda **_: (_ for _ in ()).throw(RuntimeError("clave-secreta"))  # type: ignore[method-assign]
    status, body = connect(Rig(plane)).call("GET", "/sandboxes")
    assert status == 500
    validate(body, "error")
    assert "clave-secreta" not in str(body)


def test_the_api_object_is_the_one_both_shells_wrap() -> None:
    assert isinstance(Rig(three_sandboxes()).api, DashboardApi)


def test_there_are_no_additional_accounts_without_the_multi_account_view(
    connect: Connect,
) -> None:
    status, body = connect(Rig(three_sandboxes())).call("GET", "/accounts")
    assert status == 200
    validate(body, "accounts")
    assert body == {"accounts": []}
