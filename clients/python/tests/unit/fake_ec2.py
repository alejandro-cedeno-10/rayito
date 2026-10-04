"""Cliente `ec2` falso de sólo lectura para la comprobación previa de
`EfsVolumes` (`m15-efs-volumes`): una VPC, sus subredes y sus tablas de
rutas en memoria, con la forma de respuesta de `DescribeVpcs`,
`DescribeVpcAttribute`, `DescribeSubnets` y `DescribeRouteTables`. Apunta
cada operación en `calls` para probar que nada escribe."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .fake_efs import client_error

VPC_ID = "vpc-0123456789abcdef0"
SUBNET_A = "subnet-aaaaaaaaaaaa"
SUBNET_B = "subnet-bbbbbbbbbbbb"
SUBNET_C = "subnet-cccccccccccc"
OTHER_VPC_ID = "vpc-ffffffffffff"


def subnet(subnet_id: str, az: str, *, vpc_id: str = VPC_ID, free: int = 250) -> dict[str, Any]:
    return {
        "SubnetId": subnet_id,
        "VpcId": vpc_id,
        "AvailabilityZone": az,
        "AvailableIpAddressCount": free,
        "State": "available",
    }


@dataclass(eq=False)
class FakeEc2Api:
    vpcs: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            VPC_ID: {"VpcId": VPC_ID, "State": "available", "dns": True, "hostnames": True}
        }
    )
    subnets: list[dict[str, Any]] = field(
        default_factory=lambda: [
            subnet(SUBNET_A, "us-east-1a"),
            subnet(SUBNET_B, "us-east-1b"),
            subnet(SUBNET_C, "us-east-1c"),
        ]
    )
    route_tables: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "Associations": [{"Main": True}],
                "Routes": [
                    {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local"},
                    {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-0123456789abcdef0"},
                ],
            }
        ]
    )
    calls: list[str] = field(default_factory=list)

    def describe_vpcs(self, **params: Any) -> dict[str, Any]:
        self.calls.append("describe_vpcs")
        (vpc_id,) = params["VpcIds"]
        if vpc_id not in self.vpcs:
            raise client_error("InvalidVpcID.NotFound", "no existe", "DescribeVpcs")
        vpc = self.vpcs[vpc_id]
        return {"Vpcs": [{"VpcId": vpc_id, "State": vpc["State"]}]}

    def describe_vpc_attribute(self, **params: Any) -> dict[str, Any]:
        self.calls.append(f"describe_vpc_attribute:{params['Attribute']}")
        vpc = self.vpcs[params["VpcId"]]
        if params["Attribute"] == "enableDnsSupport":
            return {"EnableDnsSupport": {"Value": vpc["dns"]}}
        return {"EnableDnsHostnames": {"Value": vpc["hostnames"]}}

    def describe_subnets(self, **params: Any) -> dict[str, Any]:
        self.calls.append("describe_subnets")
        assert "SubnetIds" not in params
        (wanted_filter,) = params["Filters"]
        assert wanted_filter["Name"] == "subnet-id"
        wanted = set(wanted_filter["Values"])
        return {"Subnets": [s for s in self.subnets if s["SubnetId"] in wanted]}

    def get_paginator(self, operation: str) -> Any:
        assert operation == "describe_route_tables"
        fake = self

        class Paginator:
            def paginate(self, **params: Any) -> Iterator[dict[str, Any]]:
                fake.calls.append("describe_route_tables")
                assert params["Filters"][0]["Name"] == "vpc-id"
                yield {"RouteTables": fake.route_tables}

        return Paginator()
