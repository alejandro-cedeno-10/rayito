"""Las diez comprobaciones de `rayito doctor` y su modelo de resultado.

Cada comprobación es una función `(clients, DoctorContext) -> CheckResult`;
`run_check` la envuelve para que una excepción sea `FAIL` (o `SKIP` cuando
un `AccessDeniedException` sólo impide un diagnóstico, no un prerequisito)
y nunca aborte las siguientes. Ninguna comprobación guarda en `details` un
JWE, un access token ni un `runHookPayload`.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from botocore.exceptions import ClientError

from rayito._aws import ControlPlane, PortSpec
from rayito._limits import DEFAULT_PORT, SUPPORTED_REGIONS
from rayito._models import SandboxHealth, SandboxInfo, SandboxListItem
from rayito._sandbox_base import METADATA_PROBE_TIMEOUT_SECONDS, health_from_proto
from rayito._transport import TransportSettings
from rayito._version import __version__
from rayito.cli._compat import COMPATIBILITY, Assessment, assess
from rayito.cli._console import client_error_code, client_error_message, iso_utc
from rayito.cli._publish import (
    MANAGED_BASE_IMAGE_NAME,
    S3_KEY_PREFIX,
    base_image_arn,
    latest_build,
    read_gate,
)
from rayito.cli._session import Clients
from rayito.exceptions import AuthenticationException, SandboxNotFoundException
from rayito.sandbox_sync.main import Sandbox, probe_health

CheckStatus = Literal["OK", "WARN", "FAIL", "SKIP"]

CHECK_NAMES = (
    "credentials",
    "managed-images",
    "quotas",
    "iam-simulation",
    "bucket",
    "image-gate",
    "sandboxes",
    "token",
    "agent",
    "compatibility",
)
DEFAULT_TEMPLATE = "rayito-base"
LAUNCH_TIMEOUT_SECONDS = 300
LAUNCH_METADATA = {"rayito": "doctor"}
MAX_RUNNING_BEFORE_FAIL = 10
MAX_LISTED_IDS = 20
ACCESS_DENIED = "AccessDeniedException"
NOT_FOUND = "ResourceNotFoundException"
LARGE_REGIONS = frozenset({"us-east-1", "us-east-2", "us-west-2", "ap-northeast-1"})
ADVISORY_SUMMARY = "simulación orientativa: las comprobaciones 2, 5, 6 y 8 son las que cuentan"
BUCKET_PERMISSIONS_HINT = (
    "publish necesita s3:ListBucket sobre el bucket y s3:GetObject/s3:PutObject "
    f"sobre {S3_KEY_PREFIX}/* (CallerPolicy de infra/iam.yaml)"
)
CAPS_IMAGE_SUFFIX = "-caps"

QUOTA_DEFAULTS: dict[str, tuple[str, float]] = {
    "L-CD1C0CC4": ("memoria ARM_64 (GB)", 400.0),
    "L-535CA9B6": ("RunMicrovm rate", 5.0),
    "L-91B95582": ("RunMicrovm burst", 5.0),
    "L-118C44B3": ("ResumeMicrovm rate", 5.0),
    "L-25EEC0A4": ("ResumeMicrovm burst", 5.0),
    "L-90045317": ("SuspendMicrovm rate", 2.0),
    "L-139F9A48": ("SuspendMicrovm burst", 2.0),
    "L-74787B8A": ("TerminateMicrovm rate", 10.0),
    "L-2CCA0501": ("TerminateMicrovm burst", 10.0),
    "L-CE98C9E3": ("GetMicrovm rate", 100.0),
    "L-C9C2110E": ("GetMicrovm burst", 100.0),
    "L-7712260B": ("CreateMicrovmAuthToken rate", 50.0),
    "L-D65D9F16": ("CreateMicrovmAuthToken burst", 50.0),
    "L-B78D2ECC": ("CreateMicrovmShellAuthToken rate", 5.0),
    "L-9A5E43A5": ("CreateMicrovmShellAuthToken burst", 5.0),
    "L-72E0D058": ("builds concurrentes", 5.0),
    "L-942E56BE": ("imágenes", 100.0),
    "L-F8BECE9C": ("versiones por imagen", 50.0),
}
LARGE_REGION_QUOTA_DEFAULTS: dict[str, float] = {"L-CD1C0CC4": 1024.0, "L-72E0D058": 10.0}

IMAGE_ACTIONS = (
    "lambda:RunMicrovm",
    "lambda:GetMicrovm",
    "lambda:SuspendMicrovm",
    "lambda:ResumeMicrovm",
    "lambda:TerminateMicrovm",
    "lambda:CreateMicrovmAuthToken",
    "lambda:GetMicrovmImage",
    "lambda:UpdateMicrovmImage",
    "lambda:DeleteMicrovmImageVersion",
    "lambda:GetMicrovmImageVersion",
    "lambda:ListMicrovmImageVersions",
    "lambda:GetMicrovmImageBuild",
    "lambda:ListMicrovmImageBuilds",
)
WILDCARD_ACTIONS = (
    "lambda:ListMicrovms",
    "lambda:ListMicrovmImages",
    "lambda:ListManagedMicrovmImages",
    "lambda:ListManagedMicrovmImageVersions",
    "lambda:CreateMicrovmImage",
    "logs:DescribeLogStreams",
    "logs:GetLogEvents",
    "servicequotas:ListServiceQuotas",
)
CONNECTOR_ACTIONS = ("lambda:PassNetworkConnector",)
BUCKET_OBJECT_ACTIONS = ("s3:PutObject", "s3:GetObject")
BUCKET_ACTIONS = ("s3:ListBucket",)
SIMULATED_ACTIONS: tuple[tuple[str, str], ...] = (
    *((action, "image") for action in IMAGE_ACTIONS),
    *((action, "*") for action in WILDCARD_ACTIONS),
    *((action, "connector") for action in CONNECTOR_ACTIONS),
    *((action, "bucket-objects") for action in BUCKET_OBJECT_ACTIONS),
    *((action, "bucket") for action in BUCKET_ACTIONS),
)


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorContext:
    """Lo que las comprobaciones se pasan unas a otras. `minted_token` sólo
    alimenta la sonda de `Health`: nunca se imprime ni entra en `details`."""

    template: str = DEFAULT_TEMPLATE
    template_version: str | None = None
    bucket: str | None = None
    launch: bool = False
    account: str | None = None
    principal_arn: str | None = None
    principal_kind: str | None = None
    template_arn: str | None = None
    resolved_version: str | None = None
    launched: Sandbox | None = None
    target_sandbox: SandboxInfo | None = None
    minted_token: str | None = None
    health: SandboxHealth | None = None
    transport: TransportSettings = field(default_factory=TransportSettings)
    probe_timeout: float = METADATA_PROBE_TIMEOUT_SECONDS


CheckFunction = Callable[[Clients, DoctorContext], CheckResult]


@dataclass(frozen=True)
class CheckSpec:
    name: str
    run: CheckFunction
    denied_action: str
    denied_status: CheckStatus = "FAIL"


def principal_kind(arn: str) -> str:
    resource = arn.split(":", 5)[-1] if arn.count(":") >= 5 else arn
    kind = resource.split("/", 1)[0]
    return kind if kind in {"user", "assumed-role", "root", "federated-user"} else "other"


def policy_source_arn(clients: Clients, arn: str) -> str | None:
    """El simulador quiere el ARN IAM del principal: un usuario tal cual; un
    assumed-role a través de `iam:GetRole` (el ARN de sesión pierde el path
    del rol, p. ej. `/aws-reserved/sso.amazonaws.com/`); root y el resto no
    se simulan."""
    kind = principal_kind(arn)
    if kind == "user":
        return arn
    if kind == "assumed-role":
        role_name = arn.split("/")[1]
        return str(clients.iam.get_role(RoleName=role_name)["Role"]["Arn"])
    return None


def quota_default(code: str, region: str) -> float:
    if region in LARGE_REGIONS and code in LARGE_REGION_QUOTA_DEFAULTS:
        return LARGE_REGION_QUOTA_DEFAULTS[code]
    return QUOTA_DEFAULTS[code][1]


def check_credentials(clients: Clients, ctx: DoctorContext) -> CheckResult:
    identity = clients.sts.get_caller_identity()
    ctx.account = str(identity["Account"])
    ctx.principal_arn = str(identity["Arn"])
    ctx.principal_kind = principal_kind(ctx.principal_arn)
    details: dict[str, Any] = {
        "account": ctx.account,
        "arn": ctx.principal_arn,
        "principal_kind": ctx.principal_kind,
        "region": clients.region,
    }
    summary = f"cuenta {ctx.account}, {clients.region}, {ctx.principal_kind}"
    if clients.region not in SUPPORTED_REGIONS:
        details["supported_regions"] = list(SUPPORTED_REGIONS)
        return CheckResult(
            "credentials",
            "WARN",
            f"{summary}; región fuera de las {len(SUPPORTED_REGIONS)} con Lambda MicroVMs",
            details,
        )
    return CheckResult("credentials", "OK", summary, details)


def newest_managed_version(clients: Clients, arn: str) -> dict[str, Any] | None:
    paginator = clients.microvms.get_paginator("list_managed_microvm_image_versions")
    versions = [
        item for page in paginator.paginate(imageIdentifier=arn) for item in page.get("items", [])
    ]
    if not versions:
        return None
    newest = max(versions, key=lambda item: item["createdAt"])
    return {"imageVersion": str(newest["imageVersion"]), "createdAt": iso_utc(newest["createdAt"])}


def check_managed_images(clients: Clients, ctx: DoctorContext) -> CheckResult:
    paginator = clients.microvms.get_paginator("list_managed_microvm_images")
    managed = [
        str(item["imageArn"]) for page in paginator.paginate() for item in page.get("items", [])
    ]
    expected = base_image_arn(clients.region)
    details: dict[str, Any] = {"managed": managed, "expected": expected}
    if expected not in managed:
        return CheckResult(
            "managed-images",
            "WARN",
            f"list-managed-microvm-images responde pero {MANAGED_BASE_IMAGE_NAME} no está",
            details,
        )
    details["newest_version"] = newest_managed_version(clients, expected)
    newest = details["newest_version"]
    version = newest["imageVersion"] if newest else "?"
    return CheckResult(
        "managed-images",
        "OK",
        f"{MANAGED_BASE_IMAGE_NAME} disponible, versión más nueva {version}",
        details,
    )


def microvm_quotas(clients: Clients) -> list[dict[str, Any]]:
    paginator = clients.quotas.get_paginator("list_service_quotas")
    return [
        {
            "code": str(quota["QuotaCode"]),
            "name": str(quota["QuotaName"]),
            "value": float(quota["Value"]),
            "adjustable": bool(quota.get("Adjustable", False)),
        }
        for page in paginator.paginate(ServiceCode="lambda")
        for quota in page.get("Quotas", [])
        if "microvm" in str(quota["QuotaName"]).lower()
    ]


def check_quotas(clients: Clients, ctx: DoctorContext) -> CheckResult:
    listed = microvm_quotas(clients)
    by_code = {quota["code"]: quota for quota in listed}
    below: list[dict[str, Any]] = []
    missing: list[str] = []
    for code, (short_name, _) in QUOTA_DEFAULTS.items():
        expected = quota_default(code, clients.region)
        quota = by_code.get(code)
        if quota is None:
            missing.append(f"{code} ({short_name})")
        elif quota["value"] < expected:
            below.append({**quota, "short_name": short_name, "default": expected})
    details: dict[str, Any] = {"quotas": listed, "below_default": below, "missing": missing}
    if below or missing:
        named = ", ".join(f"{q['short_name']} {q['value']:g} < {q['default']:g}" for q in below)
        if missing:
            named = ", ".join(filter(None, [named, f"sin listar: {', '.join(missing)}"]))
        return CheckResult(
            "quotas", "WARN", f"cuenta con cuotas reducidas: pide aumento ({named})", details
        )
    return CheckResult(
        "quotas", "OK", f"{len(listed)} cuotas de MicroVM, ninguna por debajo del default", details
    )


def simulation_groups(clients: Clients, ctx: DoctorContext) -> list[tuple[list[str], list[str]]]:
    image_arn = ctx.template_arn or (
        f"arn:aws:lambda:{clients.region}:{ctx.account}:microvm-image:{ctx.template}"
    )
    connector = f"arn:aws:lambda:{clients.region}:aws:network-connector:aws-network-connector:*"
    groups: list[tuple[list[str], list[str]]] = [
        (list(IMAGE_ACTIONS), [image_arn]),
        (list(WILDCARD_ACTIONS), []),
        (list(CONNECTOR_ACTIONS), [connector]),
    ]
    if ctx.bucket:
        groups.append(
            (list(BUCKET_OBJECT_ACTIONS), [f"arn:aws:s3:::{ctx.bucket}/{S3_KEY_PREFIX}/*"])
        )
        groups.append((list(BUCKET_ACTIONS), [f"arn:aws:s3:::{ctx.bucket}"]))
    return groups


def simulate(
    clients: Clients, source_arn: str, actions: list[str], resources: list[str]
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"PolicySourceArn": source_arn, "ActionNames": actions}
    if resources:
        params["ResourceArns"] = resources
    paginator = clients.iam.get_paginator("simulate_principal_policy")
    return [
        {
            "action": str(result["EvalActionName"]),
            "resource": str(result.get("EvalResourceName", "*")),
            "decision": str(result["EvalDecision"]),
            "allowed_by_organizations": result.get("OrganizationsDecisionDetail", {}).get(
                "AllowedByOrganizations"
            ),
        }
        for page in paginator.paginate(**params)
        for result in page.get("EvaluationResults", [])
    ]


def check_iam_simulation(clients: Clients, ctx: DoctorContext) -> CheckResult:
    """Cada grupo de acciones se simula contra su recurso (una llamada por
    recurso): el simulador cruza acciones y recursos, así que mezclar el ARN
    de la imagen con el del bucket produciría denegaciones espurias."""
    if ctx.principal_arn is None:
        return CheckResult("iam-simulation", "SKIP", "sin identidad (comprobación 1)", {})
    source_arn = policy_source_arn(clients, ctx.principal_arn)
    if source_arn is None:
        return CheckResult(
            "iam-simulation",
            "SKIP",
            f"principal {ctx.principal_kind}: nada que simular",
            {"principal_kind": ctx.principal_kind},
        )
    results: list[dict[str, Any]] = []
    for actions, resources in simulation_groups(clients, ctx):
        results.extend(simulate(clients, source_arn, actions, resources))
    explicit = [r["action"] for r in results if r["decision"] == "explicitDeny"]
    by_scp = [r["action"] for r in results if r["allowed_by_organizations"] is False]
    implicit = [r["action"] for r in results if r["decision"] == "implicitDeny"]
    details = {
        "policy_source_arn": source_arn,
        "results": results,
        "explicit_deny": explicit,
        "denied_by_organizations": by_scp,
        "implicit_deny": implicit,
    }
    if explicit or by_scp:
        named = ", ".join(sorted(set(explicit + by_scp)))
        return CheckResult(
            "iam-simulation",
            "FAIL",
            f"denegación explícita o por SCP: {named}; {ADVISORY_SUMMARY}",
            details,
        )
    if implicit:
        return CheckResult(
            "iam-simulation",
            "WARN",
            f"sin permiso explícito para {', '.join(implicit)}; {ADVISORY_SUMMARY}",
            details,
        )
    return CheckResult(
        "iam-simulation", "OK", f"{len(results)} acciones permitidas; {ADVISORY_SUMMARY}", details
    )


def check_bucket(clients: Clients, ctx: DoctorContext) -> CheckResult:
    """Una sola `head_bucket`: exige `s3:ListBucket`, el mismo permiso con el
    que `publish` recibe un 404 (y no un 403) del `head_object` de un
    artefacto nuevo; la región viene en su cabecera `x-amz-bucket-region`."""
    if not ctx.bucket:
        return CheckResult("bucket", "SKIP", "sin --bucket ni RAYITO_BUCKET", {})
    try:
        response = clients.s3.head_bucket(Bucket=ctx.bucket)
    except ClientError as exc:
        code = client_error_code(exc)
        return CheckResult(
            "bucket",
            "FAIL",
            f"s3://{ctx.bucket} no accesible ({exc.operation_name} {code}: "
            f"{client_error_message(exc)}); {BUCKET_PERMISSIONS_HINT}",
            {"bucket": ctx.bucket, "operation": exc.operation_name, "error": code},
        )
    region = str(response.get("BucketRegion") or "")
    details = {"bucket": ctx.bucket, "bucket_region": region, "region": clients.region}
    if not region:
        return CheckResult(
            "bucket",
            "WARN",
            f"s3://{ctx.bucket} accesible pero head-bucket no trajo x-amz-bucket-region",
            details,
        )
    if region != clients.region:
        return CheckResult(
            "bucket",
            "WARN",
            f"s3://{ctx.bucket} está en {region}, no en {clients.region}: "
            "el build fallará con S3_CROSS_REGION_ACCESS_DENIED",
            details,
        )
    return CheckResult("bucket", "OK", f"s3://{ctx.bucket} accesible en {region}", details)


def check_image_gate(clients: Clients, ctx: DoctorContext) -> CheckResult:
    ctx.template_arn = clients.control_plane.resolve_template_arn(ctx.template)
    try:
        image = clients.microvms.get_microvm_image(imageIdentifier=ctx.template_arn)
    except ClientError as exc:
        if client_error_code(exc) != NOT_FOUND:
            raise
        return CheckResult(
            "image-gate",
            "FAIL",
            f"la imagen {ctx.template} no existe: publica con `rayito image publish`",
            {"template_arn": ctx.template_arn, "error": NOT_FOUND},
        )
    version = ctx.template_version or image.get("latestActiveImageVersion")
    details: dict[str, Any] = {
        "template_arn": ctx.template_arn,
        "image_state": str(image["state"]),
        "latest_active_version": image.get("latestActiveImageVersion"),
        "latest_failed_version": image.get("latestFailedImageVersion"),
    }
    if not version:
        return CheckResult(
            "image-gate",
            "FAIL",
            f"{ctx.template} sin versión activa: publica con `rayito image publish`",
            details,
        )
    ctx.resolved_version = str(version)
    gate = read_gate(clients, ctx.template_arn, ctx.resolved_version)
    build = latest_build(clients, ctx.template_arn, ctx.resolved_version)
    details.update(
        {
            "version": ctx.resolved_version,
            "gate": gate.as_dict(),
            "chipset_generation": build.get("chipsetGeneration"),
            "snapshot_build": build.get("snapshotBuild", {}),
        }
    )
    if not gate.launchable:
        return CheckResult(
            "image-gate",
            "FAIL",
            f"{ctx.template} {ctx.resolved_version} no lanzable: imagen {gate.image_state}, "
            f"versión {gate.version_state}/{gate.version_status} ({gate.state_reason!r})",
            details,
        )
    if image.get("latestFailedImageVersion"):
        return CheckResult(
            "image-gate",
            "WARN",
            f"{ctx.template} {ctx.resolved_version} lanzable; un build fallido "
            f"({image['latestFailedImageVersion']}) sigue listado: `rayito image prune`",
            details,
        )
    return CheckResult(
        "image-gate", "OK", f"{ctx.template} {ctx.resolved_version} lanzable", details
    )


def running_items(items: Sequence[SandboxListItem]) -> list[SandboxListItem]:
    return sorted(
        (item for item in items if item.state == "RUNNING"), key=lambda item: item.started_at
    )


def check_sandboxes(clients: Clients, ctx: DoctorContext) -> CheckResult:
    plane = clients.control_plane
    arn = ctx.template_arn or plane.resolve_template_arn(ctx.template)
    ctx.template_arn = arn
    of_template = list(plane.list_microvms(image_arn=arn))
    counts: dict[str, int] = {}
    for item in of_template:
        counts[item.state] = counts.get(item.state, 0) + 1
    running = running_items(of_template)
    details: dict[str, Any] = {
        "template_arn": arn,
        "counts": counts,
        "running": len(running),
        "oldest_running_started_at": iso_utc(running[0].started_at) if running else None,
        "running_ids": [item.sandbox_id for item in running[:MAX_LISTED_IDS]],
    }
    if running:
        ctx.target_sandbox = plane.get_microvm(running[-1].sandbox_id)
    if len(running) > MAX_RUNNING_BEFORE_FAIL:
        return CheckResult(
            "sandboxes",
            "FAIL",
            f"{len(running)} MicroVMs RUNNING de {ctx.template} (más de "
            f"{MAX_RUNNING_BEFORE_FAIL}): `rayito sandbox kill --all --template {ctx.template}`",
            details,
        )
    if running:
        return CheckResult(
            "sandboxes",
            "WARN",
            f"{len(running)} MicroVMs vivos facturando: `rayito sandbox list`, "
            "`rayito sandbox kill --all`",
            details,
        )
    return CheckResult("sandboxes", "OK", f"ningún MicroVM RUNNING de {ctx.template}", details)


def launch_sandbox(clients: Clients, ctx: DoctorContext) -> Sandbox:
    return clients.sandbox_factory(
        ctx.template,
        template_version=ctx.template_version,
        timeout=LAUNCH_TIMEOUT_SECONDS,
        idle=None,
        logging="disabled",
        metadata=dict(LAUNCH_METADATA),
        control_plane=clients.control_plane,
    )


def token_details(sandbox_id: str, source: str) -> dict[str, Any]:
    return {"sandbox_id": sandbox_id, "port": DEFAULT_PORT, "ttl_minutes": 60, "source": source}


def check_token(clients: Clients, ctx: DoctorContext) -> CheckResult:
    if ctx.launch:
        ctx.launched = launch_sandbox(clients, ctx)
        ctx.target_sandbox = ctx.launched.info
        return CheckResult(
            "token",
            "OK",
            f"token acuñado por create() para {ctx.launched.sandbox_id}",
            token_details(ctx.launched.sandbox_id, "create()"),
        )
    if ctx.target_sandbox is None:
        return CheckResult(
            "token", "SKIP", "sin sandbox RUNNING: usa --launch para probar uno efímero", {}
        )
    sandbox_id = ctx.target_sandbox.sandbox_id
    ctx.minted_token = clients.control_plane.create_auth_token(
        sandbox_id, (PortSpec.single(DEFAULT_PORT),)
    )
    return CheckResult(
        "token",
        "OK",
        f"token del proxy acuñado para {sandbox_id} (puerto {DEFAULT_PORT})",
        token_details(sandbox_id, "create-microvm-auth-token"),
    )


class PremintedControlPlane:
    """El plano de control de la sonda de `Health`: la primera acuñación
    devuelve el JWE de la comprobación `token` (sin llamada nueva) y las
    siguientes (reintento tras un 403) van al plano real."""

    def __init__(self, inner: ControlPlane, token: str) -> None:
        self._inner = inner
        self._token: str | None = token

    def create_auth_token(self, sandbox_id: str, ports: Sequence[PortSpec]) -> str:
        if self._token is not None:
            token, self._token = self._token, None
            return token
        return self._inner.create_auth_token(sandbox_id, ports)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def health_details(health: SandboxHealth) -> dict[str, Any]:
    return {
        "agent_version": health.agent_version,
        "agent_ready": health.agent_ready,
        "kernel_ready": health.kernel_ready,
        "imds_blocked": health.imds_blocked,
        "hook_anomalies": health.hook_anomalies,
        "resume_generation": health.resume_generation,
        "uptime_ms": health.uptime_ms,
    }


def read_health(clients: Clients, ctx: DoctorContext) -> SandboxHealth | None:
    if ctx.launched is not None:
        return ctx.launched.get_health()
    if ctx.target_sandbox is None or ctx.minted_token is None:
        return None
    plane = PremintedControlPlane(clients.control_plane, ctx.minted_token)
    response = probe_health(plane, ctx.target_sandbox, ctx.transport, ctx.probe_timeout)
    return health_from_proto(response) if response is not None else None


def caps_image(ctx: DoctorContext) -> bool:
    return ctx.template.split(":")[-1].endswith(CAPS_IMAGE_SUFFIX)


def check_agent(clients: Clients, ctx: DoctorContext) -> CheckResult:
    health = read_health(clients, ctx)
    if health is None:
        return CheckResult("agent", "SKIP", "sin token (comprobación 8)", {})
    ctx.health = health
    details = health_details(health)
    if not health.agent_ready:
        return CheckResult("agent", "FAIL", "rayd responde pero no está agent_ready", details)
    warnings: list[str] = []
    if not health.kernel_ready:
        warnings.append("kernel_ready=false (el sidecar aún rota el kernel)")
    if health.hook_anomalies > 0:
        warnings.append(f"hook_anomalies={health.hook_anomalies} (alguien posee un token allPorts)")
    if caps_image(ctx) and not health.imds_blocked:
        warnings.append("imds_blocked=false en una imagen -caps")
    if warnings:
        return CheckResult(
            "agent", "WARN", f"rayd {health.agent_version}: {'; '.join(warnings)}", details
        )
    return CheckResult(
        "agent",
        "OK",
        f"rayd {health.agent_version} agent_ready, kernel_ready={health.kernel_ready}, "
        f"imds_blocked={health.imds_blocked}",
        details,
    )


def compatibility_rows() -> list[dict[str, str]]:
    return [dataclasses.asdict(row) for row in COMPATIBILITY]


def image_version_for_compatibility(ctx: DoctorContext) -> str | None:
    if ctx.target_sandbox is not None:
        return ctx.target_sandbox.template_version
    return ctx.resolved_version


def with_image_version(reason: str, image_version: str | None) -> str:
    if image_version is None:
        return reason
    return f"{reason}; imagen {image_version} (contador de builds, informativo)"


def check_compatibility(clients: Clients, ctx: DoctorContext) -> CheckResult:
    """La versión de imagen sólo se informa: es el contador de builds de esa
    imagen en esta cuenta, no un criterio de compatibilidad."""
    image_version = image_version_for_compatibility(ctx)
    if ctx.health is None:
        return CheckResult(
            "compatibility",
            "SKIP",
            f"sin agent_version (comprobaciones 8-9): SDK {__version__} no comprobado",
            {"sdk_version": __version__, "image_version": image_version},
        )
    assessment: Assessment = assess(__version__, ctx.health.agent_version)
    details = {
        "sdk_version": __version__,
        "agent_version": ctx.health.agent_version,
        "image_version": image_version,
        "row": None if assessment.row is None else dataclasses.asdict(assessment.row),
    }
    return CheckResult(
        "compatibility",
        assessment.status,
        with_image_version(assessment.reason, image_version),
        details,
    )


CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec("credentials", check_credentials, "sts:GetCallerIdentity"),
    CheckSpec("managed-images", check_managed_images, "lambda:ListManagedMicrovmImages"),
    CheckSpec("quotas", check_quotas, "servicequotas:ListServiceQuotas", "SKIP"),
    CheckSpec("iam-simulation", check_iam_simulation, "iam:SimulatePrincipalPolicy", "SKIP"),
    CheckSpec("bucket", check_bucket, "s3:ListBucket"),
    CheckSpec("image-gate", check_image_gate, "lambda:GetMicrovmImage"),
    CheckSpec("sandboxes", check_sandboxes, "lambda:ListMicrovms"),
    CheckSpec("token", check_token, "lambda:CreateMicrovmAuthToken"),
    CheckSpec("agent", check_agent, "lambda:CreateMicrovmAuthToken"),
    CheckSpec("compatibility", check_compatibility, "-"),
)
PRE_LAUNCH_CHECKS = CHECKS[:7]
LAUNCH_CHECKS = CHECKS[7:]


def denied(spec: CheckSpec, message: str, operation: str | None = None) -> CheckResult:
    """`operation` es la llamada que AWS rechazó (`ClientError.operation_name`),
    que puede no ser la de `denied_action` cuando la comprobación hace varias;
    `action` sigue siendo el permiso IAM que la comprobación necesita."""
    named = spec.denied_action if operation is None else operation
    return CheckResult(
        spec.name,
        spec.denied_status,
        f"{named} denegado: {message}",
        {"action": spec.denied_action, "operation": named, "error": ACCESS_DENIED},
    )


def run_check(spec: CheckSpec, clients: Clients, ctx: DoctorContext) -> CheckResult:
    """Nunca relanza: `AccessDeniedException` es `FAIL` (o `SKIP`) nombrando
    la operación rechazada; cualquier otra excepción, `FAIL` con su clase y
    mensaje."""
    started = time.monotonic()
    try:
        result = spec.run(clients, ctx)
    except ClientError as exc:
        code = client_error_code(exc)
        message = client_error_message(exc)
        if code == ACCESS_DENIED:
            result = denied(spec, message, exc.operation_name)
        else:
            result = CheckResult(
                spec.name,
                "FAIL",
                f"AWS error {code} en {exc.operation_name}: {message}",
                {"error": code, "operation": exc.operation_name},
            )
    except AuthenticationException as exc:
        result = denied(spec, str(exc))
    except SandboxNotFoundException as exc:
        result = CheckResult(spec.name, "FAIL", str(exc), {"error": type(exc).__name__})
    except Exception as exc:
        result = CheckResult(
            spec.name, "FAIL", f"{type(exc).__name__}: {exc}", {"error": type(exc).__name__}
        )
    result.details["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return result
