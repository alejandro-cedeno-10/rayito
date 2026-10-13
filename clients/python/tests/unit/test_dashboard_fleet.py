"""`Fleet` y `AsyncFleet`: inventario con memoria de imagen, imágenes y
versiones (adaptador con `Stubber`), historial de eventos y acciones masivas
(sólo con `allow_actions=True`, al ritmo de la cuota de TPS, sin que un
fallo detenga la cola y sin repetir el mensaje de AWS)."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime

import pytest
from botocore.stub import Stubber

from rayito._dashboard._aws import LambdaMicrovmsImageCatalog
from rayito._dashboard._domain import BulkProgress
from rayito._dashboard._fleet import Fleet
from rayito._dashboard._fleet_async import AsyncFleet
from rayito._lifecycle_events._domain import EventRecord
from rayito.exceptions import (
    AuthenticationException,
    CapacityException,
    InvalidArgumentException,
    QuotaExceededException,
    SandboxException,
)

from .conftest import IMAGE_ARN, FakeClock, no_retry_client
from .fake_dashboard import (
    OTHER_IMAGE_ARN,
    FakeEvents,
    FakeImageCatalog,
    FakePlane,
    RaisingError,
    list_item,
    sandbox_id,
)

pytestmark = pytest.mark.usefixtures("clean_catalog")


def running_fleet(count: int = 3) -> FakePlane:
    return FakePlane([list_item(n) for n in range(1, count + 1)])


def fleet(plane: FakePlane, clock: FakeClock | None = None, **extra: object) -> Fleet:
    clock = clock or FakeClock()
    return Fleet(control_plane=plane, monotonic=clock, sleep=clock.sleep, **extra)  # type: ignore[arg-type]


def test_sandboxes_carry_the_image_memory_and_never_envs_or_hook_payloads() -> None:
    plane = FakePlane(
        [list_item(1), list_item(2, image_arn=OTHER_IMAGE_ARN)],
        memory={(IMAGE_ARN, "1.0"): 2048, (OTHER_IMAGE_ARN, "1.0"): 8192},
    )

    views = fleet(plane).sandboxes()

    assert [(v.sandbox_id, v.memory_mib, v.template_name) for v in views] == [
        (sandbox_id(1), 2048, "rayito-base-2gb"),
        (sandbox_id(2), 8192, "rayito-base-8gb"),
    ]
    fields = set(vars(views[0]))
    assert fields.isdisjoint({"envs", "run_hook_payload", "token", "jwe", "endpoint"})


def test_sandboxes_filter_by_image_name_and_state() -> None:
    plane = FakePlane(
        [list_item(1), list_item(2, state="SUSPENDED"), list_item(3, image_arn=OTHER_IMAGE_ARN)],
        memory={(IMAGE_ARN, "1.0"): 2048, (OTHER_IMAGE_ARN, "1.0"): 8192},
    )
    views = fleet(plane).sandboxes(states={"SUSPENDED"}, image="rayito-base-2gb")
    assert [v.sandbox_id for v in views] == [sandbox_id(2)]
    assert ("resolve_template_arn", "rayito-base-2gb") in plane.calls


def test_images_and_versions_use_the_catalog_port_and_resolve_names() -> None:
    catalog = FakeImageCatalog()
    service = fleet(running_fleet(), images=catalog)
    assert [i.name for i in service.images(name_filter="rayito")] == ["rayito-base"]
    assert [v.image_version for v in service.image_versions("rayito-base")] == ["2.0"]
    assert catalog.calls == [("list_images", "rayito"), ("list_versions", IMAGE_ARN)]


def test_events_need_a_history_and_pass_the_query_through() -> None:
    with pytest.raises(InvalidArgumentException, match="historial de eventos"):
        fleet(running_fleet()).events()
    record = EventRecord("e1", sandbox_id(1), "created", None, 0, 1, IMAGE_ARN, "1.0")
    history = FakeEvents([record])
    events = fleet(running_fleet(), events=history).events(
        sandbox_id=sandbox_id(1), types=["sandbox.lifecycle.created"], limit=5, order="asc"
    )
    assert events == [record]
    assert history.calls == [
        {
            "sandbox_id": sandbox_id(1),
            "types": ["sandbox.lifecycle.created"],
            "limit": 5,
            "order": "asc",
        }
    ]


def test_actions_are_off_unless_explicitly_allowed() -> None:
    plane = running_fleet()
    service = fleet(plane)
    assert service.allow_actions is False
    with pytest.raises(InvalidArgumentException, match="allow_actions"):
        service.run_bulk("kill", [sandbox_id(1)])
    assert plane.operations() == []


def test_bulk_suspend_runs_at_the_quota_rate_and_reports_progress() -> None:
    plane = running_fleet(6)
    clock = FakeClock()
    progress: list[BulkProgress] = []
    service = fleet(plane, clock, allow_actions=True)

    result = service.run_bulk(
        "suspend", [sandbox_id(n) for n in range(1, 7)], on_progress=progress.append
    )

    assert result.plan.tps == 2
    assert [r.outcome for r in result.items] == ["done"] * 6
    assert result.count("done") == 6
    assert plane.operations() == ["suspend_microvm"] * 6
    assert [p.done for p in progress] == [1, 2, 3, 4, 5, 6]
    assert {p.total for p in progress} == {6}
    assert clock.now - 1_000_000.0 == pytest.approx(2.0)


def test_bulk_uses_a_lower_applied_rate_and_never_exceeds_the_api_tps() -> None:
    service = fleet(running_fleet(), allow_actions=True)
    assert service.plan_bulk("suspend", 10, applied_rate=1).tps == 1
    assert service.plan_bulk("suspend", 10, applied_rate=50).tps == 2


def test_a_failure_does_not_stop_the_queue_and_only_the_code_is_reported() -> None:
    plane = running_fleet(4)
    plane.fail[("terminate_microvm", sandbox_id(2))] = RaisingError("ConflictException")
    plane.fail[("terminate_microvm", sandbox_id(3))] = AuthenticationException(
        "arn:aws:... secreto", aws_code="AccessDeniedException"
    )
    plane.unchanged.add(("terminate_microvm", sandbox_id(4)))

    result = fleet(plane, allow_actions=True).run_bulk("kill", [sandbox_id(n) for n in range(1, 5)])

    assert [(r.outcome, r.code) for r in result.items] == [
        ("done", None),
        ("failed", "ConflictException"),
        ("failed", "AccessDeniedException"),
        ("unchanged", None),
    ]
    assert "secreto" not in repr(result)
    assert "mensaje" not in repr(result)


def test_quota_and_capacity_failures_do_not_stop_the_queue() -> None:
    plane = running_fleet(4)
    plane.fail[("resume_microvm", sandbox_id(2))] = QuotaExceededException(
        "cuota", quota_code="L-1"
    )
    plane.fail[("resume_microvm", sandbox_id(3))] = CapacityException("sin capacidad")

    result = fleet(plane, allow_actions=True).run_bulk(
        "resume", [sandbox_id(n) for n in range(1, 5)]
    )

    assert [(r.outcome, r.code) for r in result.items] == [
        ("done", None),
        ("failed", "ServiceQuotaExceededException"),
        ("failed", "InsufficientCapacityException"),
        ("done", None),
    ]


def test_a_failure_without_an_aws_code_uses_the_language_neutral_code() -> None:
    plane = running_fleet(1)
    plane.fail[("terminate_microvm", sandbox_id(1))] = SandboxException("x")

    result = fleet(plane, allow_actions=True).run_bulk("kill", [sandbox_id(1)])

    assert result.items[0].code == "UnknownError"


def test_bulk_validates_before_any_call_and_dedupes_ids() -> None:
    plane = running_fleet()
    service = fleet(plane, allow_actions=True)
    for bad in ([], [""], ["x" * 257]):
        with pytest.raises(InvalidArgumentException):
            service.run_bulk("kill", bad)
    with pytest.raises(InvalidArgumentException, match="action"):
        service.run_bulk("delete", [sandbox_id(1)])  # type: ignore[arg-type]
    assert plane.operations() == []
    result = service.run_bulk("resume", [sandbox_id(1), sandbox_id(1)])
    assert len(result.items) == 1


def test_async_fleet_matches_the_sync_results_and_signatures() -> None:
    plane = FakePlane([list_item(1), list_item(2)])
    clock = FakeClock()
    asynchronous = AsyncFleet(
        control_plane=plane,
        allow_actions=True,
        monotonic=clock,
        sleep=clock.sleep,
    )
    views = asyncio.run(asynchronous.sandboxes())
    assert [v.sandbox_id for v in views] == [sandbox_id(1), sandbox_id(2)]
    result = asyncio.run(asynchronous.run_bulk("kill", [sandbox_id(1)]))
    assert result.items[0].outcome == "done"
    assert inspect.signature(AsyncFleet.__init__) == inspect.signature(Fleet.__init__)
    for method in ("sandboxes", "images", "image_versions", "events", "plan_bulk", "run_bulk"):
        assert list(inspect.signature(getattr(Fleet, method)).parameters) == list(
            inspect.signature(getattr(AsyncFleet, method)).parameters
        ), method


def version_item(version: str, *, created: datetime, memory: bool = True) -> dict[str, object]:
    item: dict[str, object] = {
        "imageArn": IMAGE_ARN,
        "imageVersion": version,
        "state": "SUCCESSFUL",
        "status": "ACTIVE",
        "createdAt": created,
        "codeArtifact": {"uri": "s3://amzn-s3-demo-bucket/rayito/artifact-abc.zip"},
        "baseImageVersion": "1",
        "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
        "buildRoleArn": "arn:aws:iam::123456789012:role/build",
    }
    if memory:
        item["resources"] = [{"minimumMemoryInMiB": 2048}]
    return item


def test_image_adapter_lists_images_and_sorts_versions_newest_first() -> None:
    client = no_retry_client("lambda-microvms")
    created = datetime(2026, 10, 1, tzinfo=UTC)
    with Stubber(client) as stub:
        stub.add_response(
            "list_microvm_images",
            {
                "items": [
                    {
                        "name": "rayito-base",
                        "imageArn": IMAGE_ARN,
                        "state": "CREATED",
                        "latestActiveImageVersion": "2",
                        "createdAt": created,
                    }
                ]
            },
            {"nameFilter": "rayito"},
        )
        stub.add_response(
            "list_microvm_image_versions",
            {
                "items": [
                    version_item("1", created=created, memory=False),
                    version_item("2", created=created.replace(day=9)),
                ]
            },
            {"imageIdentifier": IMAGE_ARN},
        )
        catalog = LambdaMicrovmsImageCatalog(lambda: client)
        images = catalog.list_images(name_filter="rayito")
        versions = catalog.list_versions(IMAGE_ARN)

    assert [(i.name, i.latest_active_version, i.latest_failed_version) for i in images] == [
        ("rayito-base", "2", None)
    ]
    assert [(v.image_version, v.memory_mib) for v in versions] == [("2", 2048), ("1", None)]
    assert versions[0].artifact == "artifact-abc.zip"
    assert "amzn-s3-demo-bucket" not in repr(versions)


def test_the_real_control_plane_and_lifecycle_events_satisfy_the_ports() -> None:
    from rayito import LifecycleEvents
    from rayito._aws import LambdaMicrovmsControlPlane
    from rayito._dashboard._ports import EventsHistory, ManagementPlane

    plane: ManagementPlane = LambdaMicrovmsControlPlane(no_retry_client("lambda-microvms"))
    history: EventsHistory = LifecycleEvents(stacks=object(), gateway=object())  # type: ignore[arg-type]
    assert plane.region
    assert history is not None
