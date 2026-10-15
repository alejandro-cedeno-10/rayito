"""Adaptadores `boto3` de los puertos del panel de gestión: Service Quotas,
Cost Explorer y el catálogo de imágenes de `lambda-microvms`. Cada uno recibe
un proveedor de cliente (`LazyClient.get`, o un cliente ya construido en los
tests), así que construirlos no llama a AWS. Los parámetros de AWS usados
aquí están en `AWS_API_NOTES.md` §30 y §6. Un error se traduce por **código**
(nunca por texto) y el mensaje sólo lleva ese código: el de AWS puede nombrar
recursos de la cuenta.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any, Final, TypeVar

from botocore.exceptions import BotoCoreError, ClientError

from rayito._aws_sanitize import sanitize_aws_error
from rayito._dashboard._domain import (
    AppliedQuota,
    CostGroup,
    CostPeriod,
    CostQueryResult,
    Granularity,
    ImageSummary,
    ImageVersionSummary,
)
from rayito._limits import COST_EXPLORER_SERVICE_NAME, QUOTA_SERVICE_CODE
from rayito.exceptions import AuthenticationException, DashboardException, RateLimitException

T = TypeVar("T")
ClientProvider = Callable[[], Any]

#: Las cuotas de MicroVMs son las de `lambda` cuyo nombre lo menciona (el
#: mismo criterio que `rayito doctor`).
MICROVM_QUOTA_NAME_MARKER: Final = "microvm"
ACCESS_DENIED_CODE: Final = "AccessDeniedException"
#: Throttling de Service Quotas (`TooManyRequestsException`), de Cost Explorer
#: (`LimitExceededException`) y de lambda-microvms (`ThrottlingException`).
THROTTLING_CODES: Final = frozenset(
    {"TooManyRequestsException", "LimitExceededException", "ThrottlingException"}
)
COST_METRICS: Final = ("UnblendedCost", "UsageQuantity")
COST_GROUP_KEY: Final = "USAGE_TYPE"
SERVICE_DIMENSION: Final = "SERVICE"
REGION_DIMENSION: Final = "REGION"


def dashboard_call(operation: str, invoke: Callable[[], T]) -> T:
    """Lanza fuera del `except` y desde el resumen sin mensaje: ni
    `__cause__` ni `__context__` guardan el `ClientError` original."""
    try:
        return invoke()
    except (ClientError, BotoCoreError) as exc:
        cause = sanitize_aws_error(exc, include_message=False)
    message = f"{operation}: AWS respondió {cause.name}"
    if cause.code == ACCESS_DENIED_CODE:
        raise AuthenticationException(message, aws_code=cause.code) from cause
    if cause.code in THROTTLING_CODES:
        raise RateLimitException(message, aws_code=cause.code) from cause
    raise DashboardException(message, aws_code=cause.code) from cause


def list_microvm_quotas(client: Any) -> list[AppliedQuota]:
    """Las cuotas de MicroVMs de `lambda` de un cliente de Service Quotas ya
    construido, sin traducir errores: `rayito doctor` clasifica el
    `ClientError` por su cuenta."""
    paginator = client.get_paginator("list_service_quotas")
    return [
        AppliedQuota(
            code=str(quota["QuotaCode"]),
            name=str(quota["QuotaName"]),
            value=float(quota["Value"]),
            adjustable=bool(quota.get("Adjustable", False)),
        )
        for page in paginator.paginate(ServiceCode=QUOTA_SERVICE_CODE)
        for quota in page.get("Quotas", [])
        if MICROVM_QUOTA_NAME_MARKER in str(quota["QuotaName"]).lower()
    ]


class ServiceQuotasSource:
    """`QuotaSource` sobre Service Quotas (`servicequotas:ListServiceQuotas`)."""

    def __init__(self, client: ClientProvider) -> None:
        self._client = client

    def list_applied_quotas(self) -> list[AppliedQuota]:
        return dashboard_call(
            "Quotas.list_service_quotas", lambda: list_microvm_quotas(self._client())
        )


class CostExplorerSource:
    """`CostSource` sobre Cost Explorer (`ce:GetCostAndUsage`, $0,01 por
    petición): el servicio `AWS Lambda` agrupado por `USAGE_TYPE`, como el
    comando verificado en AWS_API_NOTES.md §12. Sigue `NextPageToken` y cada
    página cuenta como una petición."""

    def __init__(self, client: ClientProvider) -> None:
        self._client = client

    def get_cost_and_usage(
        self, *, start: date, end: date, granularity: Granularity, region: str | None
    ) -> CostQueryResult:
        return dashboard_call(
            "UsageCost.get_cost_and_usage", lambda: self._query(start, end, granularity, region)
        )

    def _query(
        self, start: date, end: date, granularity: Granularity, region: str | None
    ) -> CostQueryResult:
        client = self._client()
        params: dict[str, Any] = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": granularity,
            "Metrics": list(COST_METRICS),
            "Filter": _cost_filter(region),
            "GroupBy": [{"Type": "DIMENSION", "Key": COST_GROUP_KEY}],
        }
        periods: list[CostPeriod] = []
        requests = 0
        while True:
            response = client.get_cost_and_usage(**params)
            requests += 1
            periods.extend(_cost_period(result) for result in response.get("ResultsByTime", []))
            token = response.get("NextPageToken")
            if not token:
                return CostQueryResult(tuple(periods), requests)
            params["NextPageToken"] = token


def _cost_filter(region: str | None) -> dict[str, Any]:
    service = {"Dimensions": {"Key": SERVICE_DIMENSION, "Values": [COST_EXPLORER_SERVICE_NAME]}}
    if region is None:
        return service
    return {"And": [service, {"Dimensions": {"Key": REGION_DIMENSION, "Values": [region]}}]}


def _cost_period(result: dict[str, Any]) -> CostPeriod:
    window = result["TimePeriod"]
    return CostPeriod(
        start=date.fromisoformat(window["Start"]),
        end=date.fromisoformat(window["End"]),
        estimated=bool(result.get("Estimated", False)),
        groups=tuple(_cost_group(group) for group in result.get("Groups", [])),
    )


def _cost_group(group: dict[str, Any]) -> CostGroup:
    metrics = group["Metrics"]
    return CostGroup(
        usage_type=str(group["Keys"][0]),
        cost_usd=float(metrics["UnblendedCost"]["Amount"]),
        quantity=float(metrics["UsageQuantity"]["Amount"]),
        unit=str(metrics["UsageQuantity"].get("Unit", "")),
    )


def list_image_summaries(client: Any, name_filter: str | None = None) -> list[ImageSummary]:
    """Las imágenes de la cuenta, sin traducir errores (la CLI de imágenes
    clasifica el `ClientError` por su cuenta)."""
    params: dict[str, Any] = {"nameFilter": name_filter} if name_filter else {}
    paginator = client.get_paginator("list_microvm_images")
    return [
        ImageSummary(
            name=str(item["name"]),
            image_arn=str(item["imageArn"]),
            state=str(item["state"]),
            latest_active_version=item.get("latestActiveImageVersion"),
            latest_failed_version=item.get("latestFailedImageVersion"),
            created_at=item["createdAt"],
        )
        for page in paginator.paginate(**params)
        for item in page.get("items", [])
    ]


def list_image_version_summaries(client: Any, image_arn: str) -> list[ImageVersionSummary]:
    """Las versiones de una imagen, la más nueva primero, sin traducir errores."""
    paginator = client.get_paginator("list_microvm_image_versions")
    versions = [
        ImageVersionSummary(
            image_version=str(item["imageVersion"]),
            state=str(item["state"]),
            status=str(item["status"]),
            base_image_version=item.get("baseImageVersion"),
            artifact=artifact_basename(item),
            memory_mib=_minimum_memory_mib(item),
            created_at=item["createdAt"],
        )
        for page in paginator.paginate(imageIdentifier=image_arn)
        for item in page.get("items", [])
    ]
    versions.sort(key=lambda version: version.created_at, reverse=True)
    return versions


class LambdaMicrovmsImageCatalog:
    """`ImageCatalog` sobre `lambda-microvms` (`ListMicrovmImages`,
    `ListMicrovmImageVersions`)."""

    def __init__(self, client: ClientProvider) -> None:
        self._client = client

    def list_images(self, *, name_filter: str | None = None) -> list[ImageSummary]:
        return dashboard_call(
            "Fleet.list_images", lambda: list_image_summaries(self._client(), name_filter)
        )

    def list_versions(self, image_arn: str) -> list[ImageVersionSummary]:
        return dashboard_call(
            "Fleet.list_image_versions",
            lambda: list_image_version_summaries(self._client(), image_arn),
        )


def artifact_basename(item: dict[str, Any]) -> str | None:
    """El nombre del fichero del artefacto de código de una imagen o versión
    (nunca el bucket ni la ruta), o `None` si no lo trae."""
    uri = item.get("codeArtifact", {}).get("uri")
    return None if uri is None else str(uri).rsplit("/", 1)[-1]


def _minimum_memory_mib(item: dict[str, Any]) -> int | None:
    resources = item.get("resources") or []
    if not resources or "minimumMemoryInMiB" not in resources[0]:
        return None
    return int(resources[0]["minimumMemoryInMiB"])
