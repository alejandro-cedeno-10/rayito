"""`Quotas` y `AsyncQuotas`: la memoria asignada sale de `ListMicrovms` por la
memoria de cada imagen, las cuotas de Service Quotas (adaptador con
`Stubber`), los avisos y la paridad entre las superficies síncrona y asíncrona."""

from __future__ import annotations

import asyncio
import inspect

import pytest
from botocore.stub import Stubber

from rayito._dashboard._aws import ServiceQuotasSource
from rayito._dashboard._quotas import Quotas
from rayito._dashboard._quotas_async import AsyncQuotas
from rayito._limits import QUOTA_NAMES
from rayito.exceptions import AuthenticationException, DashboardException, RateLimitException

from .conftest import IMAGE_ARN, REGION, no_retry_client
from .fake_dashboard import (
    OTHER_IMAGE_ARN,
    FakePlane,
    FakeQuotaSource,
    applied_defaults,
    list_item,
)

pytestmark = pytest.mark.usefixtures("clean_catalog")


def fleet_plane() -> FakePlane:
    return FakePlane(
        [
            list_item(1, state="RUNNING"),
            list_item(2, state="SUSPENDED"),
            list_item(3, state="SUSPENDING", image_arn=OTHER_IMAGE_ARN),
            list_item(4, state="PENDING"),
            list_item(5, state="TERMINATED"),
        ],
        memory={(IMAGE_ARN, "1.0"): 2048, (OTHER_IMAGE_ARN, "1.0"): 8192},
    )


def test_report_counts_running_and_suspended_memory_against_the_quota() -> None:
    source = FakeQuotaSource(applied_defaults(memory_gb=20))
    quotas = Quotas(control_plane=fleet_plane(), source=source)

    report = quotas.report()

    assert report.region == REGION
    assert report.memory.sandboxes == 3
    assert report.memory.allocated_gb == 12
    assert report.memory.quota_gb == 20
    assert report.memory.level == "ok"
    assert source.calls == 1


def test_report_warns_and_links_the_increase_near_the_limit() -> None:
    plane = FakePlane([list_item(n) for n in range(1, 9)], memory={(IMAGE_ARN, "1.0"): 2048})
    quotas = Quotas(control_plane=plane, source=FakeQuotaSource(applied_defaults(memory_gb=20)))

    report = quotas.report()

    assert report.memory.fraction == 0.8
    assert report.memory.level == "warning"
    warning = next(w for w in report.warnings if w.kind == "memory_high")
    assert warning.increase_url.endswith("/services/lambda/quotas/L-CD1C0CC4")


def test_report_asks_the_inventory_only_for_states_that_hold_memory() -> None:
    plane = fleet_plane()
    Quotas(control_plane=plane, source=FakeQuotaSource(applied_defaults())).report()
    assert ("list_microvms", ["RUNNING", "SUSPENDED", "SUSPENDING"]) in plane.calls


def test_an_unreadable_image_version_is_counted_apart_not_fatal() -> None:
    plane = FakePlane(
        [list_item(1), list_item(2, version="9.9")], memory={(IMAGE_ARN, "1.0"): 1024}
    )
    report = Quotas(control_plane=plane, source=FakeQuotaSource(applied_defaults())).report()
    assert report.memory.sandboxes == 2
    assert report.memory.unresolved == 1
    assert report.memory.allocated_gb == 1


def test_each_image_version_is_resolved_once_per_report() -> None:
    plane = FakePlane([list_item(n) for n in range(1, 6)])
    Quotas(control_plane=plane, source=FakeQuotaSource(applied_defaults())).report()
    assert plane.operations().count("get_microvm_image_version") == 1


def test_async_quotas_return_the_same_report() -> None:
    sync = Quotas(control_plane=fleet_plane(), source=FakeQuotaSource(applied_defaults())).report()
    asynchronous = asyncio.run(
        AsyncQuotas(
            control_plane=fleet_plane(), source=FakeQuotaSource(applied_defaults())
        ).report()
    )
    assert asynchronous == sync


def test_the_sync_and_async_surfaces_have_the_same_signatures() -> None:
    assert inspect.signature(AsyncQuotas.__init__) == inspect.signature(Quotas.__init__)
    sync_report = inspect.signature(Quotas.report)
    async_report = inspect.signature(AsyncQuotas.report)
    assert list(sync_report.parameters) == list(async_report.parameters)


def test_service_quotas_adapter_keeps_only_microvm_quotas() -> None:
    client = no_retry_client("service-quotas")
    with Stubber(client) as stub:
        stub.add_response(
            "list_service_quotas",
            {
                "Quotas": [
                    {
                        "QuotaCode": "L-CD1C0CC4",
                        "QuotaName": "Total memory for MicroVMs",
                        "Value": 1024.0,
                        "Adjustable": True,
                    },
                    {
                        "QuotaCode": "L-B99A9384",
                        "QuotaName": "Concurrent executions",
                        "Value": 1000.0,
                    },
                ]
            },
            {"ServiceCode": "lambda"},
        )
        applied = ServiceQuotasSource(lambda: client).list_applied_quotas()
    assert [(q.code, q.value, q.adjustable) for q in applied] == [("L-CD1C0CC4", 1024.0, True)]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("AccessDeniedException", AuthenticationException),
        ("TooManyRequestsException", RateLimitException),
        ("ServiceException", DashboardException),
    ],
)
def test_service_quotas_errors_map_by_code_and_never_repeat_the_aws_message(
    code: str, expected: type[Exception]
) -> None:
    client = no_retry_client("service-quotas")
    with Stubber(client) as stub:
        stub.add_client_error(
            "list_service_quotas", service_error_code=code, service_message="arn:aws:... secreto"
        )
        with pytest.raises(expected) as raised:
            ServiceQuotasSource(lambda: client).list_applied_quotas()
    assert code in str(raised.value)
    assert "secreto" not in str(raised.value)
    assert getattr(raised.value, "aws_code", None) == code


def test_the_catalog_names_are_spanish_labels_without_account_data() -> None:
    for name in QUOTA_NAMES.values():
        assert "arn:" not in name
