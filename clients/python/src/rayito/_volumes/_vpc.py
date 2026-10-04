"""Adaptador boto3 de `NetworkInspector` (`_network.py`): sólo lectura de
EC2 con las credenciales del llamante. Las únicas operaciones y parámetros
son los de AWS_API_NOTES.md §22 ("Comprobación previa de la VPC"):
`DescribeVpcs(VpcIds)`, `DescribeVpcAttribute(VpcId, Attribute)`,
`DescribeSubnets(Filters=[subnet-id])` y `DescribeRouteTables(Filters=[vpc-id])`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

import boto3

from rayito._aws import LazyClient, aws_code
from rayito._volumes._network import (
    DefaultRouteTarget,
    RouteTableFacts,
    SubnetFacts,
    VpcFacts,
)

#: `DescribeVpcs` con un `VpcIds` que no existe no devuelve una lista vacía:
#: falla con este código (modelo `ec2`).
VPC_NOT_FOUND: Final = "InvalidVpcID.NotFound"
#: La ruta por defecto de IPv4 (modelo `ec2`, `Route.DestinationCidrBlock`).
DEFAULT_ROUTE_CIDR: Final = "0.0.0.0/0"
#: Prefijo de un internet gateway en `Route.GatewayId` (los demás valores
#: de ese campo son `local` o gateways de endpoint de VPC).
INTERNET_GATEWAY_PREFIX: Final = "igw-"


def default_route_target(routes: Sequence[Mapping[str, Any]]) -> DefaultRouteTarget | None:
    for route in routes:
        if route.get("DestinationCidrBlock") != DEFAULT_ROUTE_CIDR:
            continue
        if route.get("NatGatewayId"):
            return "nat"
        if str(route.get("GatewayId", "")).startswith(INTERNET_GATEWAY_PREFIX):
            return "internet-gateway"
        return "other"
    return None


class Ec2NetworkInspector:
    """`client` devuelve el cliente boto3 `ec2` (perezoso: construir esto no
    llama a AWS). `rayito doctor` le pasa el suyo; `EfsVolumes`, uno de
    `LazyClient`."""

    def __init__(self, client: Callable[[], Any]) -> None:
        self._client = client

    @classmethod
    def lazy(
        cls, *, region: str | None, session: boto3.session.Session | None
    ) -> Ec2NetworkInspector:
        return cls(LazyClient("ec2", region=region, session=session).get)

    def vpc(self, vpc_id: str) -> VpcFacts | None:
        ec2 = self._client()
        try:
            vpcs = ec2.describe_vpcs(VpcIds=[vpc_id]).get("Vpcs", [])
        except Exception as exc:
            if aws_code(exc) == VPC_NOT_FOUND:
                return None
            raise
        if not vpcs:
            return None

        def attribute(name: str, key: str) -> bool:
            response = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute=name)
            return bool(response.get(key, {}).get("Value", False))

        return VpcFacts(
            vpc_id=vpc_id,
            state=str(vpcs[0].get("State", "")),
            dns_support=attribute("enableDnsSupport", "EnableDnsSupport"),
            dns_hostnames=attribute("enableDnsHostnames", "EnableDnsHostnames"),
        )

    def subnets(self, subnet_ids: Sequence[str]) -> list[SubnetFacts]:
        # `Filters` y no `SubnetIds`: con un id que no existe, `SubnetIds`
        # falla entera; el filtro devuelve las que sí existen y la ausente
        # queda como hallazgo `subnet-missing`.
        response = self._client().describe_subnets(
            Filters=[{"Name": "subnet-id", "Values": list(subnet_ids)}]
        )
        return [
            SubnetFacts(
                subnet_id=str(subnet["SubnetId"]),
                vpc_id=str(subnet.get("VpcId", "")),
                availability_zone=str(subnet.get("AvailabilityZone", "")),
                available_ips=int(subnet.get("AvailableIpAddressCount", 0)),
                state=str(subnet.get("State", "")),
            )
            for subnet in response.get("Subnets", [])
        ]

    def route_tables(self, vpc_id: str) -> list[RouteTableFacts]:
        paginator = self._client().get_paginator("describe_route_tables")
        tables: list[RouteTableFacts] = []
        for page in paginator.paginate(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]):
            for table in page.get("RouteTables", []):
                associations = table.get("Associations", [])
                tables.append(
                    RouteTableFacts(
                        subnet_ids=tuple(
                            str(a["SubnetId"]) for a in associations if a.get("SubnetId")
                        ),
                        main=any(bool(a.get("Main")) for a in associations),
                        default_route=default_route_target(table.get("Routes", [])),
                    )
                )
        return tables
