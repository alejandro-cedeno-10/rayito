"""Dominio puro de la comprobación previa de `EfsVolumes` (`m15-efs-volumes`,
experimental): qué VPC y subredes **ya existentes** acepta la pila
`efs-volumes` y qué hay que avisar antes de desplegarla. Sin `boto3`: los
hechos (`VpcFacts`, `SubnetFacts`, `RouteTableFacts`) los trae el puerto
`NetworkInspector` (`_vpc.py`, sólo llamadas `Describe*` de EC2) y esta
función sólo los evalúa.

Nada de aquí crea ni cambia nada: la pila sólo añade recursos nuevos dentro
de la VPC (`infra/efs-volumes.yaml`) y nunca toca la VPC, sus subredes,
tablas de rutas, NACLs ni grupos de seguridad existentes.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from rayito._stacks._model import CostStatement
from rayito.exceptions import InvalidArgumentException

#: Formatos de EC2 (`vpc-`/`subnet-` y 8 o 17 hex; se acepta el rango para
#: no depender de la longitud exacta): AWS_API_NOTES.md §22.
VPC_ID_PATTERN: Final = re.compile(r"^vpc-[0-9a-f]{8,17}$")
SUBNET_ID_PATTERN: Final = re.compile(r"^subnet-[0-9a-f]{8,17}$")
#: `arn:<partición>:elasticfilesystem:<región>:<cuenta>:access-point/fsap-…`
#: (formato de ARN de la referencia de autorización de EFS; el id sigue
#: `ACCESS_POINT_ID_PATTERN` de `_domain.py`).
ACCESS_POINT_ARN_PATTERN: Final = re.compile(
    r"^arn:aws[a-z-]*:elasticfilesystem:[a-z0-9-]+:\d{12}:access-point/fsap-[0-9a-f]{8,40}$"
)

#: `infra/efs-volumes.yaml` declara un mount target por subred en tres
#: ranuras (`MountTarget1..3`); EFS admite como mucho uno por AZ.
MIN_SUBNETS: Final = 1
MAX_SUBNETS: Final = 3
#: IPs libres que necesita cada subred: una para el mount target (EFS le da
#: una IP de la subred) y al menos una para una ENI del conector (Lambda crea
#: una ENI por combinación de subred y grupo de seguridad). Cuántas más usa
#: el conector bajo carga es EFS-20, aún sin medir.
REQUIRED_FREE_IPS_PER_SUBNET: Final = 2
#: `State` de EC2 para una subred o VPC utilizable (modelo `ec2`).
AVAILABLE_STATE: Final = "available"
#: Destino de una ruta por defecto (`0.0.0.0/0`) según el adaptador.
DefaultRouteTarget = Literal["nat", "internet-gateway", "other"]

FindingLevel = Literal["OK", "WARN", "FAIL"]
#: El orden de gravedad, de menor a mayor (`EfsNetworkReport.status`).
LEVEL_ORDER: Final[tuple[FindingLevel, ...]] = ("OK", "WARN", "FAIL")


@dataclass(frozen=True)
class VpcFacts:
    vpc_id: str
    state: str
    dns_support: bool
    dns_hostnames: bool


@dataclass(frozen=True)
class SubnetFacts:
    subnet_id: str
    vpc_id: str
    availability_zone: str
    available_ips: int
    state: str


@dataclass(frozen=True)
class RouteTableFacts:
    """Una tabla de rutas de la VPC: las subredes asociadas explícitamente,
    si es la principal (la de toda subred sin asociación propia) y a dónde
    va su ruta por defecto, si la tiene."""

    subnet_ids: tuple[str, ...]
    main: bool
    default_route: DefaultRouteTarget | None


class NetworkInspector(Protocol):
    """Lo único que la comprobación previa lee de EC2 (AWS_API_NOTES.md §22):
    `DescribeVpcs`, `DescribeVpcAttribute`, `DescribeSubnets` y
    `DescribeRouteTables`. Nunca escribe."""

    def vpc(self, vpc_id: str) -> VpcFacts | None: ...
    def subnets(self, subnet_ids: Sequence[str]) -> list[SubnetFacts]: ...
    def route_tables(self, vpc_id: str) -> list[RouteTableFacts]: ...


@dataclass(frozen=True)
class NetworkFinding:
    """Un resultado de la comprobación: `code` es un conjunto cerrado
    (`vpc-missing`, `vpc-unavailable`, `vpc-dns-support`,
    `vpc-dns-hostnames`, `subnet-missing`, `subnet-other-vpc`,
    `subnet-unavailable`, `subnet-same-az`, `subnet-free-ips`, `single-az`,
    `internet-egress`)."""

    level: FindingLevel
    code: str
    message: str


@dataclass(frozen=True)
class EfsNetworkReport:
    """Lo que `EfsVolumes.check()` devuelve sin crear nada: los hallazgos,
    las AZs que cubrirían los mount targets y lo que `deploy()` crearía
    con su coste (el `CostStatement` del componente `efs-volumes`, la misma
    fuente que `rayito stack list`)."""

    vpc_id: str
    subnet_ids: tuple[str, ...]
    availability_zones: tuple[str, ...]
    findings: tuple[NetworkFinding, ...]
    cost: CostStatement

    @property
    def ok(self) -> bool:
        """`True` si ningún hallazgo es `FAIL`: `deploy()` se niega si no."""
        return all(finding.level != "FAIL" for finding in self.findings)

    @property
    def status(self) -> FindingLevel:
        return max(
            (finding.level for finding in self.findings),
            key=LEVEL_ORDER.index,
            default="OK",
        )

    @property
    def failures(self) -> tuple[NetworkFinding, ...]:
        return tuple(finding for finding in self.findings if finding.level == "FAIL")


def validate_vpc_id(value: object) -> str:
    if not isinstance(value, str) or not VPC_ID_PATTERN.match(value):
        raise InvalidArgumentException("vpc_id inválido: se esperaba 'vpc-' y 8-17 hex")
    return value


def validate_subnet_ids(values: object) -> tuple[str, ...]:
    """Una cadena `subnet-a,subnet-b` (la forma del `--param` de la CLI) o
    una secuencia; de `MIN_SUBNETS` a `MAX_SUBNETS`, sin repetir."""
    items = values.split(",") if isinstance(values, str) else values
    if not isinstance(items, Sequence):
        raise InvalidArgumentException("subnet_ids espera una lista de ids de subred")
    subnet_ids = tuple(str(item).strip() for item in items if str(item).strip())
    if not MIN_SUBNETS <= len(subnet_ids) <= MAX_SUBNETS:
        raise InvalidArgumentException(
            f"subnet_ids admite de {MIN_SUBNETS} a {MAX_SUBNETS} subredes "
            "(una por AZ: un mount target por subred)"
        )
    for subnet_id in subnet_ids:
        if not SUBNET_ID_PATTERN.match(subnet_id):
            raise InvalidArgumentException("subnet_ids: cada id es 'subnet-' y 8-17 hex")
    if len(set(subnet_ids)) != len(subnet_ids):
        raise InvalidArgumentException("subnet_ids no admite subredes repetidas")
    return subnet_ids


def validate_access_point_arns(values: Sequence[str]) -> tuple[str, ...]:
    arns = tuple(values)
    for arn in arns:
        if not isinstance(arn, str) or not ACCESS_POINT_ARN_PATTERN.match(arn):
            raise InvalidArgumentException(
                "access_point_arns: cada valor es el ARN de un access point de EFS "
                "(arn:aws:elasticfilesystem:<región>:<cuenta>:access-point/fsap-…)"
            )
    return arns


def _vpc_findings(vpc: VpcFacts | None) -> list[NetworkFinding]:
    if vpc is None:
        return [NetworkFinding("FAIL", "vpc-missing", "la VPC no existe en esta cuenta y región")]
    findings: list[NetworkFinding] = []
    if vpc.state != AVAILABLE_STATE:
        findings.append(
            NetworkFinding("FAIL", "vpc-unavailable", f"la VPC está en estado {vpc.state!r}")
        )
    if not vpc.dns_support:
        findings.append(
            NetworkFinding(
                "WARN",
                "vpc-dns-support",
                "la VPC no tiene enableDnsSupport: el nombre DNS del sistema de ficheros "
                "no resuelve (montar por la IP del mount target sí funciona)",
            )
        )
    if not vpc.dns_hostnames:
        findings.append(
            NetworkFinding(
                "WARN",
                "vpc-dns-hostnames",
                "la VPC no tiene enableDnsHostnames: EFS lo pide para montar por nombre DNS",
            )
        )
    return findings


def _subnet_findings(
    vpc_id: str, subnet_ids: Sequence[str], subnets: Sequence[SubnetFacts]
) -> list[NetworkFinding]:
    known = {subnet.subnet_id: subnet for subnet in subnets}
    findings: list[NetworkFinding] = []
    seen_azs: dict[str, str] = {}
    for subnet_id in subnet_ids:
        subnet = known.get(subnet_id)
        if subnet is None:
            findings.append(
                NetworkFinding("FAIL", "subnet-missing", f"{subnet_id}: la subred no existe")
            )
            continue
        if subnet.vpc_id != vpc_id:
            findings.append(
                NetworkFinding("FAIL", "subnet-other-vpc", f"{subnet_id}: la subred es de otra VPC")
            )
        if subnet.state != AVAILABLE_STATE:
            findings.append(
                NetworkFinding(
                    "FAIL",
                    "subnet-unavailable",
                    f"{subnet_id}: la subred está en estado {subnet.state!r}",
                )
            )
        if subnet.availability_zone in seen_azs:
            findings.append(
                NetworkFinding(
                    "FAIL",
                    "subnet-same-az",
                    f"{subnet_id} y {seen_azs[subnet.availability_zone]} están en la misma AZ "
                    f"({subnet.availability_zone}): EFS admite un mount target por AZ",
                )
            )
        else:
            seen_azs[subnet.availability_zone] = subnet_id
        if subnet.available_ips < REQUIRED_FREE_IPS_PER_SUBNET:
            findings.append(
                NetworkFinding(
                    "FAIL",
                    "subnet-free-ips",
                    f"{subnet_id}: {subnet.available_ips} IPs libres, hacen falta al menos "
                    f"{REQUIRED_FREE_IPS_PER_SUBNET} (mount target + ENI del conector)",
                )
            )
    return findings


def _route_table_of(subnet_id: str, tables: Sequence[RouteTableFacts]) -> RouteTableFacts | None:
    """La tabla asociada explícitamente a la subred o, si no hay, la
    principal de la VPC (la regla de EC2)."""
    explicit = next((table for table in tables if subnet_id in table.subnet_ids), None)
    return explicit or next((table for table in tables if table.main), None)


def _egress_finding(subnet_ids: Sequence[str], tables: Sequence[RouteTableFacts]) -> NetworkFinding:
    with_nat = sum(
        1
        for subnet_id in subnet_ids
        if (table := _route_table_of(subnet_id, tables)) is not None
        and table.default_route == "nat"
    )
    return NetworkFinding(
        "OK",
        "internet-egress",
        "el conector de esta pila sólo deja salir NFS (2049) hacia los mount targets; la "
        "salida a internet de un sandbox que use la VPC depende del NAT de tu VPC "
        f"({with_nat} de {len(subnet_ids)} subredes con ruta por defecto a un NAT) y de "
        "un conector que la permita (ninguno de esta pila)",
    )


def assess_network(
    vpc_id: str,
    subnet_ids: Sequence[str],
    *,
    vpc: VpcFacts | None,
    subnets: Sequence[SubnetFacts],
    route_tables: Sequence[RouteTableFacts],
    cost: CostStatement,
) -> EfsNetworkReport:
    """Evalúa los hechos de EC2 para `vpc_id`/`subnet_ids` ya validados."""
    findings = _vpc_findings(vpc)
    if vpc is not None:
        findings.extend(_subnet_findings(vpc_id, subnet_ids, subnets))
    known = {subnet.subnet_id: subnet for subnet in subnets}
    zones = tuple(
        dict.fromkeys(
            known[subnet_id].availability_zone for subnet_id in subnet_ids if subnet_id in known
        )
    )
    if vpc is not None and len(zones) == 1:
        findings.append(
            NetworkFinding(
                "WARN",
                "single-az",
                "una sola AZ: sin redundancia de mount target, y un MicroVM que salga por "
                "otra AZ cruza zonas (transferencia entre AZs facturada)",
            )
        )
    if vpc is not None:
        findings.append(_egress_finding(subnet_ids, route_tables))
    return EfsNetworkReport(
        vpc_id=vpc_id,
        subnet_ids=tuple(subnet_ids),
        availability_zones=zones,
        findings=tuple(findings),
        cost=cost,
    )


def inspect_network(
    inspector: NetworkInspector,
    vpc_id: str,
    subnet_ids: object,
    *,
    cost: CostStatement,
) -> EfsNetworkReport:
    """Valida la petición (sin I/O) y, si es correcta, lee los hechos de EC2
    y los evalúa. Lo comparten `EfsVolumes.check()` y `rayito doctor`."""
    validated_vpc = validate_vpc_id(vpc_id)
    validated_subnets = validate_subnet_ids(subnet_ids)
    vpc = inspector.vpc(validated_vpc)
    subnets = inspector.subnets(validated_subnets) if vpc is not None else []
    tables = inspector.route_tables(validated_vpc) if vpc is not None else []
    return assess_network(
        validated_vpc,
        validated_subnets,
        vpc=vpc,
        subnets=subnets,
        route_tables=tables,
        cost=cost,
    )
