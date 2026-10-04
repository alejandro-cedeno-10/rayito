"""Comprobación previa de `EfsVolumes` en una VPC existente
(`rayito._volumes._network` + el adaptador `_vpc.Ec2NetworkInspector`):
validación sin I/O, cada hallazgo y que sólo se llaman `Describe*` de EC2."""

from __future__ import annotations

import pytest

from rayito._stacks.components.efs_volumes import COMPONENT
from rayito._volumes._network import (
    MAX_SUBNETS,
    REQUIRED_FREE_IPS_PER_SUBNET,
    RouteTableFacts,
    inspect_network,
    validate_access_point_arns,
    validate_subnet_ids,
    validate_vpc_id,
)
from rayito._volumes._vpc import Ec2NetworkInspector, default_route_target
from rayito.exceptions import InvalidArgumentException

from .fake_ec2 import OTHER_VPC_ID, SUBNET_A, SUBNET_B, SUBNET_C, VPC_ID, FakeEc2Api, subnet

READ_ONLY_PREFIXES = ("describe_",)


def check(api: FakeEc2Api, subnets: object, vpc_id: str = VPC_ID):  # type: ignore[no-untyped-def]
    return inspect_network(Ec2NetworkInspector(lambda: api), vpc_id, subnets, cost=COMPONENT.cost)


def codes(report) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {finding.code: finding.level for finding in report.findings}


def test_a_healthy_vpc_with_three_azs_passes_and_reports_what_it_would_create() -> None:
    api = FakeEc2Api()
    report = check(api, [SUBNET_A, SUBNET_B, SUBNET_C])
    assert report.ok
    assert report.status == "OK"
    assert report.availability_zones == ("us-east-1a", "us-east-1b", "us-east-1c")
    assert report.cost is COMPONENT.cost
    assert "AWS::Lambda::NetworkConnector" in report.cost.creates
    assert codes(report) == {"internet-egress": "OK"}
    assert all(call.startswith(READ_ONLY_PREFIXES) for call in api.calls)


def test_the_cli_comma_form_is_accepted() -> None:
    report = check(FakeEc2Api(), f"{SUBNET_A}, {SUBNET_B}")
    assert report.subnet_ids == (SUBNET_A, SUBNET_B)


@pytest.mark.parametrize(
    "subnets",
    [
        [],
        "",
        [SUBNET_A, SUBNET_A],
        ["subnet-xyz"],
        [SUBNET_A, SUBNET_B, SUBNET_C, "subnet-dddddddddddd"],
        42,
    ],
)
def test_malformed_subnet_ids_are_rejected_before_any_call(subnets: object) -> None:
    api = FakeEc2Api()
    with pytest.raises(InvalidArgumentException):
        check(api, subnets)
    assert api.calls == []


def test_a_malformed_vpc_id_is_rejected_without_echoing_it() -> None:
    with pytest.raises(InvalidArgumentException) as excinfo:
        validate_vpc_id("not-a-vpc-secret")
    assert "not-a-vpc-secret" not in str(excinfo.value)


def test_max_subnets_matches_the_template_slots() -> None:
    assert MAX_SUBNETS == 3
    assert validate_subnet_ids([SUBNET_A, SUBNET_B, SUBNET_C]) == (SUBNET_A, SUBNET_B, SUBNET_C)


def test_a_missing_vpc_fails_and_reads_nothing_else() -> None:
    api = FakeEc2Api(vpcs={})
    report = check(api, [SUBNET_A])
    assert not report.ok
    assert codes(report) == {"vpc-missing": "FAIL"}
    assert api.calls == ["describe_vpcs"]


def test_subnets_must_exist_belong_to_the_vpc_and_sit_in_distinct_azs() -> None:
    api = FakeEc2Api(
        subnets=[
            subnet(SUBNET_A, "us-east-1a"),
            subnet(SUBNET_B, "us-east-1a"),
            subnet(SUBNET_C, "us-east-1c", vpc_id=OTHER_VPC_ID),
        ]
    )
    report = check(api, [SUBNET_A, SUBNET_B, SUBNET_C])
    found = codes(report)
    assert found["subnet-same-az"] == "FAIL"
    assert found["subnet-other-vpc"] == "FAIL"
    assert not report.ok

    missing = check(FakeEc2Api(subnets=[subnet(SUBNET_A, "us-east-1a")]), [SUBNET_A, SUBNET_B])
    assert codes(missing)["subnet-missing"] == "FAIL"


def test_a_subnet_without_free_ips_fails() -> None:
    api = FakeEc2Api(
        subnets=[subnet(SUBNET_A, "us-east-1a", free=REQUIRED_FREE_IPS_PER_SUBNET - 1)]
    )
    report = check(api, [SUBNET_A])
    assert codes(report)["subnet-free-ips"] == "FAIL"


def test_one_az_and_no_dns_are_warnings_not_failures() -> None:
    vpcs = {VPC_ID: {"VpcId": VPC_ID, "State": "available", "dns": False, "hostnames": False}}
    report = check(FakeEc2Api(vpcs=vpcs), [SUBNET_A])
    assert report.ok
    assert report.status == "WARN"
    assert codes(report) == {
        "vpc-dns-support": "WARN",
        "vpc-dns-hostnames": "WARN",
        "single-az": "WARN",
        "internet-egress": "OK",
    }


def test_internet_egress_counts_subnets_routed_to_a_nat() -> None:
    api = FakeEc2Api(
        route_tables=[
            {"Associations": [{"Main": True}], "Routes": []},
            {
                "Associations": [{"SubnetId": SUBNET_B}],
                "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-0abc"}],
            },
        ]
    )
    report = check(api, [SUBNET_A, SUBNET_B])
    egress = next(f for f in report.findings if f.code == "internet-egress")
    assert "1 de 2" in egress.message
    assert "NAT" in egress.message


def test_internet_egress_also_counts_other_default_routes() -> None:
    # Medido 2026-10-04: subredes privadas con salida por un transit gateway.
    api = FakeEc2Api(
        route_tables=[
            {
                "Associations": [{"Main": True}],
                "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "TransitGatewayId": "tgw-0abc"}],
            },
        ]
    )
    report = check(api, [SUBNET_A, SUBNET_B])
    egress = next(f for f in report.findings if f.code == "internet-egress")
    assert "0 de 2 a un NAT, 2 a otra puerta" in egress.message


@pytest.mark.parametrize(
    ("routes", "expected"),
    [
        ([{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1"}], "nat"),
        ([{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1"}], "internet-gateway"),
        ([{"DestinationCidrBlock": "0.0.0.0/0", "TransitGatewayId": "tgw-1"}], "other"),
        ([{"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local"}], None),
    ],
)
def test_default_route_target(routes: list[dict[str, str]], expected: str | None) -> None:
    assert default_route_target(routes) == expected


def test_route_tables_are_read_with_their_associations() -> None:
    tables = Ec2NetworkInspector(lambda: FakeEc2Api()).route_tables(VPC_ID)
    assert tables == [RouteTableFacts(subnet_ids=(), main=True, default_route="nat")]


def test_access_point_arns_are_validated() -> None:
    good = "arn:aws:elasticfilesystem:us-east-1:123456789012:access-point/fsap-0123456789abcdef0"
    assert validate_access_point_arns([good]) == (good,)
    with pytest.raises(InvalidArgumentException):
        validate_access_point_arns(["arn:aws:s3:::bucket"])
