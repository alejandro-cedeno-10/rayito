"""Render the SDK limits modules from ``limits.json`` (single source).

``limits.json`` at the repository root holds every Lambda MicroVMs limit the
SDKs validate client-side (``AWS_API_NOTES.md`` §2, §3, §6, §11). This script
renders it into ``clients/python/src/rayito/_limits.py`` and
``clients/typescript/src/limits.ts`` deterministically, formatted the way
``ruff format`` and ``biome format`` expect (100 columns, trailing commas),
so neither SDK can drift from the other. Standard library only.

Usage::

    python scripts/gen_limits.py           # rewrite both modules
    python scripts/gen_limits.py --check   # exit 1 with a diff on drift
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIMITS_JSON = REPO_ROOT / "limits.json"
PYTHON_TARGET = REPO_ROOT / "clients" / "python" / "src" / "rayito" / "_limits.py"
TYPESCRIPT_TARGET = REPO_ROOT / "clients" / "typescript" / "src" / "limits.ts"
LINE_WIDTH = 100
INDENT = "    "
TS_INDENT = "  "

SET_KEYS = frozenset(
    {
        "terminalStates",
        "suspendedStates",
        "managedNetworkConnectors",
        "largeQuotaRegions",
    }
)
GROUPS: tuple[tuple[str, ...], ...] = (
    (
        "maxDurationSeconds",
        "minDurationSeconds",
        "idleMaxIdleMinSeconds",
        "idleSuspendedMinSeconds",
    ),
    (
        "tokenTtlMinutes",
        "tokenTtlMinMinutes",
        "tokenRefreshAfterMinutes",
        "tokenRefreshRetrySeconds",
    ),
    (
        "runHookPayloadMaxChars",
        "clientTokenMax",
        "microvmIdMinLength",
        "microvmIdMaxLength",
        "listMaxResults",
        "networkConnectorsMax",
    ),
    (
        "defaultPort",
        "hooksPort",
        "portMin",
        "portMax",
        "endpointTlsPort",
        "hookPathPrefix",
    ),
    ("maxConcurrentConnections1Vcpu",),
    (
        # `rayito sandbox proxy` (ADR-027), the same on both CLIs: the head
        # cap is the 64 KiB default of asyncio's StreamReader, and the three
        # timeouts keep an idle client or a hung handshake from holding a
        # connection forever. Not AWS properties.
        "proxyHeadMaxBytes",
        "proxyHeadReadTimeoutSeconds",
        "proxyUpstreamConnectTimeoutSeconds",
        "proxyUpstreamResponseHeadTimeoutSeconds",
    ),
    ("microvmStates", "terminalStates", "suspendedStates"),
    ("managedNetworkConnectors",),
    ("supportedRegions",),
    ("apiTps",),
    (
        "persistKeyPrefixMaxBytes",
        "persistExcludeMax",
        "s3BucketNameMin",
        "s3BucketNameMax",
        "defaultPersistTimeoutSeconds",
    ),
    (
        "egressMaxEntriesPerList",
        "egressMaxHostnameEntries",
        "egressHostnameMaxChars",
        "egressProxyCredentialMaxBytes",
    ),
    (
        "lifecycleCapMarginSeconds",
        "lifecycleMinMaxLifetimeSeconds",
        "lifecycleResumeGraceSeconds",
        "lifecycleAutoResumeMinSeconds",
        "lifecycleMinTimeoutSeconds",
        "lifecycleTimeoutExitCode",
    ),
    (
        "transferDefaultPrefix",
        "transferDefaultExpiresInSeconds",
        "transferDefaultMaxExpiresInSeconds",
        "transferPresignMaxSeconds",
        "transferDefaultThresholdBytes",
        "transferThresholdMinBytes",
        "transferDefaultMultipartThresholdBytes",
        "transferMultipartThresholdMinBytes",
        "transferSinglePutMaxBytes",
        "transferPartSizeMinBytes",
        "transferMaxParts",
    ),
    (
        "transferMaxActive",
        "transferMaxRunning",
        "transferRetainedMax",
        "transferRetainedSeconds",
        "transferProbeBudgetMs",
        "transferPollFastIntervalMs",
        "transferPollSlowIntervalMs",
        "transferPollFastWindowSeconds",
        "transferUrlMaxBytes",
        "transferInternalExpiresInSeconds",
        "transferConnectTimeoutMs",
        "transferIdleTimeoutMs",
    ),
    (
        "metadataMaxBytes",
        "metadataMaxKeys",
        "metadataXattrPrefix",
        "compressionOptInHeader",
    ),
    (
        # M15 (Rayito 0.6, foundations): seed for sizes-catalog's
        # `_sizing.py`/`sizing.ts` (`supportedMemoryMiB`,
        # `guestMemoryMultiplier`, Q87/Q88), the closed catalogue of official
        # image variants `_role_policy.py`/`role-policy.ts` recognise by name
        # (`officialImageVariants`) and custom-domain's reserved
        # ports (`reservedPorts`, denied as exposed ports by the SDK, the
        # CloudFront Function and the refresher).
        "supportedMemoryMiB",
        "officialImageVariants",
        "guestMemoryMultiplier",
        "reservedPorts",
    ),
    (
        # m15-efs-volumes: the cap `rayd_core::volume::spec::
        # MAX_VOLUMES_PER_SANDBOX` enforces (research doc §5), checked by
        # both SDKs before `run-microvm`.
        "efsVolumesMaxPerSandbox",
    ),
    (
        # Client-side hardening, not AWS properties: the shortest access
        # token the SDKs accept from a caller (128 bits; generated ones are
        # 32 bytes) and the most command output each stream keeps in memory
        # (the tail; older output is dropped and reported as truncated).
        "accessTokenMinBytes",
        "commandOutputMaxBytes",
    ),
    (
        # ai-agent-core (ADR-025): the AI agent's run defaults and caps
        # (cost bounds, design.md D4), the in-VM paths and protocol both
        # SDKs and the agent template agree on, and the runtime pins the
        # template bakes (OpenCode and ripgrep release assets, sha256 from
        # docs/research/2026-10-agent-spike.md) and the default timeout of a
        # single `WarmupStep` (`agent.prepare()`, pool warmup). Not AWS
        # properties.
        "defaultAgentTimeoutSeconds",
        "defaultAgentMaxSteps",
        "defaultAgentMaxTotalTokens",
        "defaultAgentMaxOutputBytes",
        "maxAgentEventLineBytes",
        "maxToolOutputPreviewBytes",
        "defaultAgentWorkdir",
        "agentStateDir",
        "modelCredentialPlaceholder",
        "opencodeSessionTitle",
        "agentProtocolVersion",
        "agentTemplateManifestPath",
        "agentTemplateManifestSchema",
        "agentMinMemoryMib",
        "agentOpencodeVersion",
        "agentOpencodeSha256",
        "agentRipgrepVersion",
        "agentRipgrepSha256",
        "defaultWarmupStepTimeoutSeconds",
    ),
    (
        # dashboard (ADR-026): Service Quotas catalog of the Lambda MicroVMs
        # quotas the panel shows (code -> name, default, kind, operation;
        # AWS_API_NOTES.md §11, defaults per region class), the warning
        # fractions of allocated memory against the quota, the Cost Explorer
        # cache TTL and the documented compute prices that back the
        # per-sandbox estimate (AWS_API_NOTES.md §12, us-east-1 ARM), and the
        # price of one paid Cost Explorer request (AWS Cost Explorer pricing
        # page, checked 2026-10-13, AWS_API_NOTES.md §12). Not AWS properties
        # except the catalog and the prices.
        "largeQuotaRegions",
        "quotaServiceCode",
        "quotaNames",
        "quotaDefaults",
        "quotaLargeRegionDefaults",
        "quotaKinds",
        "quotaOperations",
        "quotaWarningFraction",
        "quotaCriticalFraction",
        "costCacheTtlSeconds",
        "costCacheMaxEntries",
        "costExplorerRegion",
        "costExplorerServiceName",
        "costExplorerRequestPriceUsd",
        "computePriceUsdPerVcpuSecond",
        "computePriceUsdPerGbSecond",
        "snapshotWritePriceUsdPerGb",
        "snapshotReadPriceUsdPerGb",
        "snapshotStoragePriceUsdPerGbMonth",
        "baselineMibPerVcpu",
    ),
    (
        # dashboard, local shell (ADR-026, phase 1b): entropy and lifetime of
        # the one-time start token, request body and socket timeouts of the
        # loopback server, the read cache that keeps several open tabs from
        # multiplying `ListMicrovms` calls, the polling interval the SPA
        # receives from `/api/v1/meta`, the bulk-action and log bounds, and
        # the size caps of a user theme file and logo. Not AWS properties.
        "dashboardSessionTokenBytes",
        "dashboardStartTokenTtlSeconds",
        "dashboardRequestMaxBodyBytes",
        "dashboardRequestTimeoutSeconds",
        "dashboardReadCacheSeconds",
        "dashboardPollIntervalSeconds",
        "dashboardJobPollIntervalSeconds",
        "dashboardBulkMaxSandboxes",
        "dashboardJobsRetained",
        "dashboardJobsActiveMax",
        "dashboardEventsMaxLimit",
        "dashboardLogsMaxLines",
        "dashboardLogLineMaxChars",
        "dashboardThemeMaxBytes",
        "dashboardLogoMaxBytes",
    ),
    (
        # dashboard, in-account stack (ADR-026, phase 2): the Cognito group
        # that may run actions (the stack template and the handler read the
        # same name), how long the handler keeps the JWKS and the user pool
        # client id (and how soon it may refetch after an unknown `kid`),
        # the bounds on that fetch, the clock leeway and the size cap of a
        # bearer token, the STS session of the actions role, how often the handler
        # re-reads the theme from SSM and the
        # function's timeout and memory (pinned equal to the template
        # defaults by `scripts/tests/test_dashboard_template.py`). Not AWS
        # properties.
        "dashboardStackActionsGroup",
        "dashboardStackAuthCacheSeconds",
        "dashboardStackJwksRefreshMinSeconds",
        "dashboardStackJwksMaxBytes",
        "dashboardStackHttpTimeoutSeconds",
        "dashboardStackJwtLeewaySeconds",
        "dashboardStackJwtMaxBytes",
        "dashboardStackSessionSeconds",
        "dashboardStackThemeRefreshSeconds",
        "dashboardStackLambdaTimeoutSeconds",
        "dashboardStackLambdaTimeoutMarginSeconds",
        "dashboardStackLambdaMemoryMib",
        # The page function of `AuthMode=aws-iam` only serves static files.
        "dashboardStackShellLambdaTimeoutSeconds",
        "dashboardStackShellLambdaMemoryMib",
        # How long a browser may cache the CORS preflight of the IAM API.
        "dashboardStackApiCorsMaxAgeSeconds",
        # AWS properties the template pins: the CloudFormation parameter
        # value cap (ThemeJson `MaxLength`), the SSM Advanced parameter cap
        # (checked against the docs on 2026-10-13) and the Cognito access
        # token validity. The cost lookback is the Cost Explorer history the
        # handler accepts and the misses per UTC day are the paid
        # `ce:GetCostAndUsage` queries it allows in total (each costs
        # `costExplorerRequestPriceUsd`), so the worst day is bounded.
        "dashboardStackThemeParameterMaxBytes",
        "dashboardStackSsmAdvancedParameterMaxBytes",
        "dashboardStackAccessTokenHours",
        "dashboardStackCostLookbackMonths",
        "dashboardStackCostMissesPerDay",
        # The worked cost example of the stack (one tab open
        # `dashboardStackCostExampleOpenHoursPerDay` hours a day for
        # `dashboardStackCostExampleDaysPerMonth` days) and the public prices
        # it quotes, checked on 2026-10-13: Lambda $0.20 per million requests
        # with a monthly free tier of 1,000,000 requests and 400,000 GB-seconds
        # (https://aws.amazon.com/lambda/pricing/) and SSM Parameter Store
        # Advanced parameters at $0.05 per parameter-month plus $0.05 per
        # 10,000 API interactions (https://aws.amazon.com/systems-manager/pricing/).
        "dashboardStackCostExampleOpenHoursPerDay",
        "dashboardStackCostExampleDaysPerMonth",
        "lambdaRequestPriceUsdPerMillion",
        "lambdaFreeTierRequestsPerMonth",
        "lambdaFreeTierGbSecondsPerMonth",
        "ssmAdvancedParameterPriceUsdPerMonth",
        "ssmApiInteractionPriceUsdPerTenThousand",
    ),
    (
        # dashboard, phase 3 (ADR-026): the most accounts the local panel
        # accepts through `--account` and the STS session it asks for (the
        # default of `AssumeRole`, 1 hour; the roles' own maximum caps it),
        # the session name CloudTrail shows in the target account, the
        # custom metric the quota alerts publish and the CloudWatch dashboard
        # plots (namespace and name, shared by the Lambda and the generator),
        # and the schedule, timeout and memory of the alerts Lambda (pinned
        # equal to the template defaults by
        # `scripts/tests/test_quota_alerts_template.py`). Not AWS properties.
        "dashboardAccountsMax",
        "dashboardAccountLabelMaxChars",
        "dashboardAssumeRoleSessionSeconds",
        "dashboardAssumeRoleSessionName",
        "quotaMetricNamespace",
        "quotaMetricName",
        "quotaAlertsScheduleExpression",
        "quotaAlertsLambdaTimeoutSeconds",
        "quotaAlertsLambdaMemoryMib",
        # The CloudWatch prices the optional dashboard and alert stacks quote
        # (https://aws.amazon.com/cloudwatch/pricing/, us-east-1, checked
        # 2026-10-13): three free dashboards of up to 50 metrics each, then a
        # monthly price per dashboard, Logs Insights per GB scanned and a
        # custom metric per month. Not AWS properties of the API.
        "cloudwatchFreeDashboards",
        "cloudwatchFreeDashboardMetrics",
        "cloudwatchDashboardPriceUsdPerMonth",
        "cloudwatchLogsInsightsPriceUsdPerGb",
        "cloudwatchCustomMetricPriceUsdPerMonth",
    ),
    (
        # ai-agent-fast-start (design.md §6, option A): the prefetch daemon
        # of the agent template wakes every `agentPrefetchIntervalSeconds`
        # and treats a wall-clock jump above
        # `agentPrefetchRestoreJumpSeconds` as a snapshot restore (launch
        # or resume, AWS_API_NOTES §15). The requirements sha256 pins the
        # hash-locked deepagents venv file the template copies (package
        # data `rayito/_agent/_assets/requirements-deepagents.txt`). Not AWS
        # properties.
        "agentPrefetchRestoreJumpSeconds",
        "agentPrefetchIntervalSeconds",
        "agentDeepagentsRequirementsSha256",
    ),
    (
        # ai-agent-first-start: the agent template turns the kernel
        # warm-up off by writing `agentKernelWarmupSlimValue` to the marker
        # the sidecar's startup reads (`kernel-sidecar/ipython/startup/
        # 0004_warmup.py`, copied to /opt/rayito/sidecar/ by image/Dockerfile;
        # the same marker `image_zip.py --variant slim` writes). Measured on
        # AWS (AWS_API_NOTES Q155): `create()` p50 -4.4 s and memory
        # snapshot -248 MB. Not AWS properties.
        "agentKernelWarmupMarkerPath",
        "agentKernelWarmupSlimValue",
    ),
    (
        # ai-agent-first-start: before the build snapshot the prefetch
        # daemon (its fourth argument) reads what it prefetches after a
        # restore and then creates `agentPrefetchBuildMarkerPath`; the
        # template's `ready_cmd` waits for that file for at most
        # `agentPrefetchBuildTimeoutSeconds` (under the 600 s
        # `readyTimeoutInSeconds` of the image hooks), so the memory
        # snapshot already holds those pages (AWS_API_NOTES Q156). Not AWS
        # properties.
        "agentPrefetchBuildMarkerPath",
        "agentPrefetchBuildTimeoutSeconds",
    ),
    (
        # agent-runtimes-byo (ADR-028): the contract of the HTTP service an
        # agent exposes (the AgentCore runtime contract on `/ping` and
        # `/invocations`, port 8088 inside a MicroVM because `rayd` owns 8080
        # and 9000, 8080 in an exported container), its request bound and
        # concurrency, the ready marker the host writes under
        # `agentStateDir` and the bound of the redacted stderr tail it leaves
        # in the failed marker, the default lifetime of a deployment and the
        # margin `AgentProject.run()` adds to the sandbox lifetime, the
        # pins of the framework runtimes (PyPI and npm, checked 2026-10-16;
        # Hermes by tag and full commit; Node by the sha256 of the linux
        # arm64 tarball in nodejs.org SHASUMS256), the base image of exported
        # containers (name and digest, Docker Hub, checked 2026-10-16) and
        # the names of the project file and service schemas. Not AWS
        # properties.
        "agentServicePort",
        "agentServiceExportPort",
        "agentServiceHealthPath",
        "agentServiceInvokePath",
        "agentServiceSessionHeader",
        "agentServiceMaxConcurrency",
        "agentServiceMaxRequestBytes",
        "agentServiceReadyTimeoutSeconds",
        "defaultAgentDeployTimeoutSeconds",
        "agentRunSandboxMarginSeconds",
        "agentServiceReadyMarkerPath",
        "agentStartupDiagnosticsMaxBytes",
        "agentServiceSchema",
        "agentHermesApiPort",
        "agentStrandsVersion",
        "agentStrandsHarnessVersion",
        "agentStrandsTsVersion",
        "agentStrandsTsHarnessVersion",
        "agentHermesVersion",
        "agentHermesCommit",
        "agentExportBaseImage",
        "agentExportBaseImageDigest",
        "agentProjectSchema",
        "agentProjectFileNames",
    ),
    (
        # typescript-cli-tools (ADR-027), the same on both CLIs: the MCP
        # server cuts every text output to `mcpMaxOutputChars` characters
        # (the `read_file` description quotes it) and `image publish --sizes`
        # builds in waves of at most `maxConcurrentImageBuilds` (AWS_API_NOTES
        # Q83: at least 10 parallel builds do not degrade the service).
        # Not AWS properties.
        "mcpMaxOutputChars",
        "maxConcurrentImageBuilds",
    ),
    (
        # agent-runtimes-byo (ADR-028), frameworks lane: the sha256 of the
        # hashed requirements files of the Strands, Strands harness and
        # Hermes runtimes (package data under `_agent/_assets/`), the pinned
        # schema name of the host configuration file. The CPython 3.14 build
        # Hermes v0.22.2 needs is the `3.14` entry of `agentPythonBuild*`
        # below. Not AWS properties.
        "agentStrandsRequirementsSha256",
        "agentStrandsHarnessRequirementsSha256",
        "agentHermesRequirementsSha256",
        "agentHostSchema",
    ),
    (
        # 0.14 host and build adapters (design D3, D4, D6): how an HTTP app
        # the host supervises is restarted (at most `agentAppRestartMax`
        # restarts inside `agentAppRestartWindowSeconds`, exponential backoff
        # from `agentAppRestartBackoffSeconds` capped at
        # `agentAppRestartBackoffMaxSeconds`), the port a detected app is told
        # to listen on and the variable that carries it, the bounded FIFO the
        # service keeps in front of `max_concurrency` (default 0 = no queue,
        # the 0.14 behaviour of answering 429) with its wait bound, the
        # pinned python-build-standalone CPython builds and Node.js builds a
        # project may pick (key = minor or major; linux arm64 tarballs,
        # sha256 published with the asset, checked 2026-10-16) and the
        # pinned package managers the install plan can bring. Not AWS
        # properties.
        "agentAppRestartMax",
        "agentAppRestartWindowSeconds",
        "agentAppRestartBackoffSeconds",
        "agentAppRestartBackoffMaxSeconds",
        "agentAppInternalPort",
        "agentAppPortEnv",
        "agentServiceQueueMaxDefault",
        "agentServiceQueueMax",
        "agentServiceQueueTimeoutSecondsDefault",
        "agentServiceQueueTimeoutSecondsMax",
        "agentPythonBuildTag",
        "agentPythonBuildVersions",
        "agentPythonBuildSha256s",
        "agentDefaultPythonMinor",
        "agentNodeVersions",
        "agentNodeSha256s",
        "agentDefaultNodeMajor",
        "agentUvVersion",
        "agentUvSha256",
        "agentPnpmVersion",
        "agentYarnVersion",
    ),
)

PYTHON_HEADER = '''"""Límites y cuotas de Lambda MicroVMs que el SDK valida en cliente.

GENERADO por `scripts/gen_limits.py` desde `limits.json` (raíz del repo): no
editar a mano, ejecutar `make limits`. Fuente de los valores:
`AWS_API_NOTES.md` §2, §3, §6, §11 y `docs/aws-api/model_summary.md`.
Nada aquí es configurable por el usuario: son propiedades de la API.
"""

from __future__ import annotations

from typing import Final
'''

TYPESCRIPT_HEADER = """// GENERATED by scripts/gen_limits.py from limits.json (repository root).
// Do not edit by hand: run `make limits`. Values come from AWS_API_NOTES.md
// §2, §3, §6, §11. Nothing here is user-configurable: these are API facts.
"""


CONSTANT_NAME_OVERRIDES = {
    "s3BucketNameMin": "S3_BUCKET_NAME_MIN",
    "s3BucketNameMax": "S3_BUCKET_NAME_MAX",
    "supportedMemoryMiB": "SUPPORTED_MEMORY_MIB",
    "agentOpencodeSha256": "AGENT_OPENCODE_SHA256",
    "agentRipgrepSha256": "AGENT_RIPGREP_SHA256",
    "agentDeepagentsRequirementsSha256": "AGENT_DEEPAGENTS_REQUIREMENTS_SHA256",
    "agentStrandsRequirementsSha256": "AGENT_STRANDS_REQUIREMENTS_SHA256",
    "agentStrandsHarnessRequirementsSha256": "AGENT_STRANDS_HARNESS_REQUIREMENTS_SHA256",
    "agentHermesRequirementsSha256": "AGENT_HERMES_REQUIREMENTS_SHA256",
    "agentPythonBuildSha256s": "AGENT_PYTHON_BUILD_SHA256S",
    "agentNodeSha256s": "AGENT_NODE_SHA256S",
    "agentUvSha256": "AGENT_UV_SHA256",
}


def constant_name(key: str) -> str:
    """``maxConcurrentConnections1Vcpu`` -> ``MAX_CONCURRENT_CONNECTIONS_1_VCPU``;
    a service name that carries its own digit (``s3``) keeps it attached."""
    if key in CONSTANT_NAME_OVERRIDES:
        return CONSTANT_NAME_OVERRIDES[key]
    with_digits = re.sub(r"([a-zA-Z])(\d)", r"\1_\2", key)
    with_words = re.sub(r"(\d)([a-zA-Z])", r"\1_\2", with_digits)
    snake = re.sub(r"([a-z])([A-Z])", r"\1_\2", with_words)
    return snake.upper()


def load_limits() -> dict[str, object]:
    with LIMITS_JSON.open(encoding="utf-8") as handle:
        limits = json.load(handle)
    listed = {key for group in GROUPS for key in group}
    unknown = sorted(set(limits) - listed)
    missing = sorted(listed - set(limits))
    if unknown or missing:
        raise SystemExit(
            f"limits.json: claves desconocidas {unknown}, ausentes {missing}"
        )
    return limits


def quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def float_literal(value: float) -> str:
    """Plain decimal notation (``0.0000276944``), never ``2.76944e-05``."""
    text = format(Decimal(repr(value)), "f")
    return text if "." in text else f"{text}.0"


def scalar_kind(value: object) -> str:
    if isinstance(value, str):
        return "str"
    return "float" if isinstance(value, float) else "int"


def dict_value_kind(values: Iterable[object]) -> str:
    """``int``, ``float`` or ``str``: the one scalar type a dict holds."""
    kinds = {scalar_kind(value) for value in values}
    if len(kinds) != 1:
        raise SystemExit(f"limits.json: un dict mezcla tipos de valor {sorted(kinds)}")
    return kinds.pop()


# --------------------------------------------------------------------- python


def python_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        return float_literal(value)
    if isinstance(value, int):
        return repr(value)
    if isinstance(value, str):
        return quoted(value)
    raise TypeError(f"valor no soportado en limits.json: {value!r}")


def python_collection(
    prefix: str, items: Iterable[str], opening: str, closing: str
) -> str:
    """Ruff style: one line if it fits, otherwise one element per line with a
    trailing comma (the magic trailing comma keeps it exploded)."""
    elements = list(items)
    single = f"{prefix}{opening}{', '.join(elements)}{closing}"
    if len(single) <= LINE_WIDTH:
        return single
    body = "".join(f"{INDENT}{element},\n" for element in elements)
    return f"{prefix}{opening}\n{body}{closing}"


def python_frozenset(name: str, values: list[str]) -> str:
    elements = [quoted(value) for value in values]
    prefix = f"{name}: Final = frozenset("
    single = f"{prefix}{{{', '.join(elements)}}})"
    if len(single) <= LINE_WIDTH:
        return single
    inner = f"{INDENT}{{{', '.join(elements)}}}"
    if len(inner) <= LINE_WIDTH:
        return f"{prefix}\n{inner}\n)"
    body = "".join(f"{INDENT}{INDENT}{element},\n" for element in elements)
    return f"{prefix}\n{INDENT}{{\n{body}{INDENT}}}\n)"


def python_line(key: str, value: object) -> str:
    name = constant_name(key)
    if isinstance(value, dict):
        items = [
            f"{quoted(item)}: {python_scalar(value[item])}" for item in sorted(value)
        ]
        kind = dict_value_kind(value.values())
        return python_collection(
            f"{name}: Final[dict[str, {kind}]] = ", items, "{", "}"
        )
    if isinstance(value, list):
        if key in SET_KEYS:
            return python_frozenset(name, [str(item) for item in value])
        elements = [python_scalar(item) for item in value]
        if len(elements) == 1:
            return f"{name}: Final = ({elements[0]},)"
        return python_collection(f"{name}: Final = ", elements, "(", ")")
    line = f"{name}: Final = {python_scalar(value)}"
    if len(line) <= LINE_WIDTH:
        return line
    return f"{name}: Final = (\n{INDENT}{python_scalar(value)}\n)"


def render_python(limits: dict[str, object]) -> str:
    blocks = [
        "\n".join(python_line(key, limits[key]) for key in group) for group in GROUPS
    ]
    return PYTHON_HEADER + "\n" + "\n\n".join(blocks) + "\n"


# ----------------------------------------------------------------- typescript


def ts_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return float_literal(value)
    if isinstance(value, int):
        return repr(value)
    if isinstance(value, str):
        return quoted(value)
    raise TypeError(f"valor no soportado en limits.json: {value!r}")


def ts_collection(
    prefix: str, items: list[str], opening: str, closing: str, suffix: str
) -> str:
    """Biome style: one line if it fits, otherwise one element per line."""
    single = f"{prefix}{opening}{', '.join(items)}{closing}{suffix}"
    if len(single) <= LINE_WIDTH:
        return single
    body = "".join(f"{TS_INDENT}{item},\n" for item in items)
    return f"{prefix}{opening}\n{body}{closing}{suffix}"


def ts_key(key: str) -> str:
    """Biome's ``useQuoteProps``: quote only the keys that are not identifiers."""
    return key if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", key) else quoted(key)


def ts_line(key: str, value: object) -> str:
    name = constant_name(key)
    if isinstance(value, dict):
        items = [f"{ts_key(item)}: {ts_scalar(value[item])}" for item in sorted(value)]
        ts_type = "string" if dict_value_kind(value.values()) == "str" else "number"
        prefix = f"export const {name}: Readonly<Record<string, {ts_type}>> = "
        return ts_collection(prefix, items, "{", "}", ";")
    if isinstance(value, list):
        if key in SET_KEYS:
            elements = [quoted(str(item)) for item in value]
            prefix = f"export const {name}: ReadonlySet<string> = new Set("
            return ts_collection(prefix, elements, "[", "]", ");")
        elements = [ts_scalar(item) for item in value]
        return ts_collection(
            f"export const {name} = ", elements, "[", "]", " as const;"
        )
    single = f"export const {name} = {ts_scalar(value)} as const;"
    if len(single) <= LINE_WIDTH:
        return single
    return f"export const {name} =\n{TS_INDENT}{ts_scalar(value)} as const;"


def render_typescript(limits: dict[str, object]) -> str:
    blocks = ["\n".join(ts_line(key, limits[key]) for key in group) for group in GROUPS]
    return TYPESCRIPT_HEADER + "\n" + "\n\n".join(blocks) + "\n"


# ----------------------------------------------------------------------- main


def current_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def check(renders: dict[Path, str]) -> int:
    drifted = 0
    for path, expected in renders.items():
        actual = current_text(path)
        if actual == expected:
            continue
        drifted += 1
        relative = path.relative_to(REPO_ROOT).as_posix()
        diff = difflib.unified_diff(
            actual.splitlines(keepends=True),
            expected.splitlines(keepends=True),
            fromfile=f"{relative} (on disk)",
            tofile=f"{relative} (from limits.json)",
        )
        sys.stdout.write(f"gen_limits: {relative} drifts from limits.json\n")
        sys.stdout.writelines(diff)
    if drifted:
        sys.stdout.write(
            "gen_limits: run `python scripts/gen_limits.py` to regenerate\n"
        )
    return 1 if drifted else 0


def main(argv: list[str]) -> int:
    limits = load_limits()
    renders = {
        PYTHON_TARGET: render_python(limits),
        TYPESCRIPT_TARGET: render_typescript(limits),
    }
    if "--check" in argv:
        return check(renders)
    for path, text in renders.items():
        write_text(path, text)
        sys.stdout.write(
            f"gen_limits: wrote {path.relative_to(REPO_ROOT).as_posix()}\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
