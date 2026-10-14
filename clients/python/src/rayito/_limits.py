"""Límites y cuotas de Lambda MicroVMs que el SDK valida en cliente.

GENERADO por `scripts/gen_limits.py` desde `limits.json` (raíz del repo): no
editar a mano, ejecutar `make limits`. Fuente de los valores:
`AWS_API_NOTES.md` §2, §3, §6, §11 y `docs/aws-api/model_summary.md`.
Nada aquí es configurable por el usuario: son propiedades de la API.
"""

from __future__ import annotations

from typing import Final

MAX_DURATION_SECONDS: Final = 28800
MIN_DURATION_SECONDS: Final = 1
IDLE_MAX_IDLE_MIN_SECONDS: Final = 60
IDLE_SUSPENDED_MIN_SECONDS: Final = 0

TOKEN_TTL_MINUTES: Final = 60
TOKEN_TTL_MIN_MINUTES: Final = 1
TOKEN_REFRESH_AFTER_MINUTES: Final = 45
TOKEN_REFRESH_RETRY_SECONDS: Final = 60

RUN_HOOK_PAYLOAD_MAX_CHARS: Final = 4096
CLIENT_TOKEN_MAX: Final = 128
MICROVM_ID_MIN_LENGTH: Final = 1
MICROVM_ID_MAX_LENGTH: Final = 256
LIST_MAX_RESULTS: Final = 50
NETWORK_CONNECTORS_MAX: Final = 10

DEFAULT_PORT: Final = 8080
HOOKS_PORT: Final = 9000
PORT_MIN: Final = 1
PORT_MAX: Final = 65535
ENDPOINT_TLS_PORT: Final = 443
HOOK_PATH_PREFIX: Final = "/aws/lambda-microvms/runtime/v1"

MAX_CONCURRENT_CONNECTIONS_1_VCPU: Final = 8

MICROVM_STATES: Final = (
    "PENDING",
    "RUNNING",
    "SUSPENDING",
    "SUSPENDED",
    "TERMINATING",
    "TERMINATED",
)
TERMINAL_STATES: Final = frozenset({"TERMINATING", "TERMINATED"})
SUSPENDED_STATES: Final = frozenset({"SUSPENDING", "SUSPENDED"})

MANAGED_NETWORK_CONNECTORS: Final = frozenset(
    {"ALL_INGRESS", "NO_INGRESS", "SHELL_INGRESS", "INTERNET_EGRESS"}
)

SUPPORTED_REGIONS: Final = (
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "eu-west-1",
    "ap-northeast-1",
    "ap-south-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "eu-central-1",
    "eu-north-1",
)

API_TPS: Final[dict[str, int]] = {
    "CreateMicrovmAuthToken": 50,
    "CreateMicrovmShellAuthToken": 5,
    "GetMicrovm": 100,
    "ResumeMicrovm": 5,
    "RunMicrovm": 5,
    "SuspendMicrovm": 2,
    "TerminateMicrovm": 10,
}

PERSIST_KEY_PREFIX_MAX_BYTES: Final = 900
PERSIST_EXCLUDE_MAX: Final = 64
S3_BUCKET_NAME_MIN: Final = 3
S3_BUCKET_NAME_MAX: Final = 63
DEFAULT_PERSIST_TIMEOUT_SECONDS: Final = 600

EGRESS_MAX_ENTRIES_PER_LIST: Final = 256
EGRESS_MAX_HOSTNAME_ENTRIES: Final = 64
EGRESS_HOSTNAME_MAX_CHARS: Final = 253
EGRESS_PROXY_CREDENTIAL_MAX_BYTES: Final = 255

LIFECYCLE_CAP_MARGIN_SECONDS: Final = 60
LIFECYCLE_MIN_MAX_LIFETIME_SECONDS: Final = 120
LIFECYCLE_RESUME_GRACE_SECONDS: Final = 30
LIFECYCLE_AUTO_RESUME_MIN_SECONDS: Final = 300
LIFECYCLE_MIN_TIMEOUT_SECONDS: Final = 1
LIFECYCLE_TIMEOUT_EXIT_CODE: Final = 124

TRANSFER_DEFAULT_PREFIX: Final = "rayito-transfer"
TRANSFER_DEFAULT_EXPIRES_IN_SECONDS: Final = 3600
TRANSFER_DEFAULT_MAX_EXPIRES_IN_SECONDS: Final = 86400
TRANSFER_PRESIGN_MAX_SECONDS: Final = 604800
TRANSFER_DEFAULT_THRESHOLD_BYTES: Final = 8388608
TRANSFER_THRESHOLD_MIN_BYTES: Final = 1048576
TRANSFER_DEFAULT_MULTIPART_THRESHOLD_BYTES: Final = 5368709120
TRANSFER_MULTIPART_THRESHOLD_MIN_BYTES: Final = 16777216
TRANSFER_SINGLE_PUT_MAX_BYTES: Final = 5368709120
TRANSFER_PART_SIZE_MIN_BYTES: Final = 8388608
TRANSFER_MAX_PARTS: Final = 1000

TRANSFER_MAX_ACTIVE: Final = 16
TRANSFER_MAX_RUNNING: Final = 2
TRANSFER_RETAINED_MAX: Final = 64
TRANSFER_RETAINED_SECONDS: Final = 1800
TRANSFER_PROBE_BUDGET_MS: Final = 2000
TRANSFER_POLL_FAST_INTERVAL_MS: Final = 1000
TRANSFER_POLL_SLOW_INTERVAL_MS: Final = 5000
TRANSFER_POLL_FAST_WINDOW_SECONDS: Final = 600
TRANSFER_URL_MAX_BYTES: Final = 8192
TRANSFER_INTERNAL_EXPIRES_IN_SECONDS: Final = 900
TRANSFER_CONNECT_TIMEOUT_MS: Final = 5000
TRANSFER_IDLE_TIMEOUT_MS: Final = 30000

METADATA_MAX_BYTES: Final = 4000
METADATA_MAX_KEYS: Final = 64
METADATA_XATTR_PREFIX: Final = "user.rayito."
COMPRESSION_OPT_IN_HEADER: Final = "rayito-compress"

SUPPORTED_MEMORY_MIB: Final = (512, 1024, 2048, 4096, 8192)
GUEST_MEMORY_MULTIPLIER: Final = 4
RESERVED_PORTS: Final = (8080, 9000)

EFS_VOLUMES_MAX_PER_SANDBOX: Final = 4

ACCESS_TOKEN_MIN_BYTES: Final = 16
COMMAND_OUTPUT_MAX_BYTES: Final = 67108864

DEFAULT_AGENT_TIMEOUT_SECONDS: Final = 600
DEFAULT_AGENT_MAX_STEPS: Final = 50
DEFAULT_AGENT_MAX_TOTAL_TOKENS: Final = 1000000
DEFAULT_AGENT_MAX_OUTPUT_BYTES: Final = 16777216
MAX_AGENT_EVENT_LINE_BYTES: Final = 4194304
MAX_TOOL_OUTPUT_PREVIEW_BYTES: Final = 65536
DEFAULT_AGENT_WORKDIR: Final = "/home/user"
AGENT_STATE_DIR: Final = "/home/user/.rayito/agent"
MODEL_CREDENTIAL_PLACEHOLDER: Final = "placeholder-not-a-secret"
OPENCODE_SESSION_TITLE: Final = "rayito"
AGENT_PROTOCOL_VERSION: Final = 1
AGENT_TEMPLATE_MANIFEST_PATH: Final = "/opt/agents/rayito-agent.json"
AGENT_TEMPLATE_MANIFEST_SCHEMA: Final = "rayito.agent-template/1"
AGENT_MIN_MEMORY_MIB: Final = 2048
AGENT_OPENCODE_VERSION: Final = "1.18.34"
AGENT_OPENCODE_SHA256: Final = "bbdb3f00c2c51e42e315525233151309724226a8776da8e9145e3b0fa3d5310f"
AGENT_RIPGREP_VERSION: Final = "15.2.0"
AGENT_RIPGREP_SHA256: Final = "a740b91c82eaf9914cfedd353572f2791cbe0162c84101ee0951058f4dcbc90d"
DEFAULT_WARMUP_STEP_TIMEOUT_SECONDS: Final = 120

LARGE_QUOTA_REGIONS: Final = frozenset({"us-east-1", "us-east-2", "us-west-2", "ap-northeast-1"})
QUOTA_SERVICE_CODE: Final = "lambda"
QUOTA_NAMES: Final[dict[str, str]] = {
    "L-118C44B3": "ResumeMicrovm rate",
    "L-139F9A48": "SuspendMicrovm burst",
    "L-25EEC0A4": "ResumeMicrovm burst",
    "L-2CCA0501": "TerminateMicrovm burst",
    "L-535CA9B6": "RunMicrovm rate",
    "L-72E0D058": "builds concurrentes",
    "L-74787B8A": "TerminateMicrovm rate",
    "L-7712260B": "CreateMicrovmAuthToken rate",
    "L-90045317": "SuspendMicrovm rate",
    "L-91B95582": "RunMicrovm burst",
    "L-942E56BE": "imágenes",
    "L-9A5E43A5": "CreateMicrovmShellAuthToken burst",
    "L-B78D2ECC": "CreateMicrovmShellAuthToken rate",
    "L-C9C2110E": "GetMicrovm burst",
    "L-CD1C0CC4": "memoria ARM_64 (GB)",
    "L-CE98C9E3": "GetMicrovm rate",
    "L-D65D9F16": "CreateMicrovmAuthToken burst",
    "L-F8BECE9C": "versiones por imagen",
}
QUOTA_DEFAULTS: Final[dict[str, int]] = {
    "L-118C44B3": 5,
    "L-139F9A48": 2,
    "L-25EEC0A4": 5,
    "L-2CCA0501": 10,
    "L-535CA9B6": 5,
    "L-72E0D058": 5,
    "L-74787B8A": 10,
    "L-7712260B": 50,
    "L-90045317": 2,
    "L-91B95582": 5,
    "L-942E56BE": 100,
    "L-9A5E43A5": 5,
    "L-B78D2ECC": 5,
    "L-C9C2110E": 100,
    "L-CD1C0CC4": 400,
    "L-CE98C9E3": 100,
    "L-D65D9F16": 50,
    "L-F8BECE9C": 50,
}
QUOTA_LARGE_REGION_DEFAULTS: Final[dict[str, int]] = {"L-72E0D058": 10, "L-CD1C0CC4": 1024}
QUOTA_KINDS: Final[dict[str, str]] = {
    "L-118C44B3": "rate",
    "L-139F9A48": "burst",
    "L-25EEC0A4": "burst",
    "L-2CCA0501": "burst",
    "L-535CA9B6": "rate",
    "L-72E0D058": "count",
    "L-74787B8A": "rate",
    "L-7712260B": "rate",
    "L-90045317": "rate",
    "L-91B95582": "burst",
    "L-942E56BE": "count",
    "L-9A5E43A5": "burst",
    "L-B78D2ECC": "rate",
    "L-C9C2110E": "burst",
    "L-CD1C0CC4": "memory-gb",
    "L-CE98C9E3": "rate",
    "L-D65D9F16": "burst",
    "L-F8BECE9C": "count",
}
QUOTA_OPERATIONS: Final[dict[str, str]] = {
    "L-118C44B3": "ResumeMicrovm",
    "L-139F9A48": "SuspendMicrovm",
    "L-25EEC0A4": "ResumeMicrovm",
    "L-2CCA0501": "TerminateMicrovm",
    "L-535CA9B6": "RunMicrovm",
    "L-74787B8A": "TerminateMicrovm",
    "L-7712260B": "CreateMicrovmAuthToken",
    "L-90045317": "SuspendMicrovm",
    "L-91B95582": "RunMicrovm",
    "L-9A5E43A5": "CreateMicrovmShellAuthToken",
    "L-B78D2ECC": "CreateMicrovmShellAuthToken",
    "L-C9C2110E": "GetMicrovm",
    "L-CE98C9E3": "GetMicrovm",
    "L-D65D9F16": "CreateMicrovmAuthToken",
}
QUOTA_WARNING_FRACTION: Final = 0.8
QUOTA_CRITICAL_FRACTION: Final = 0.95
COST_CACHE_TTL_SECONDS: Final = 86400
COST_CACHE_MAX_ENTRIES: Final = 64
COST_EXPLORER_REGION: Final = "us-east-1"
COST_EXPLORER_SERVICE_NAME: Final = "AWS Lambda"
COST_EXPLORER_REQUEST_PRICE_USD: Final = 0.01
COMPUTE_PRICE_USD_PER_VCPU_SECOND: Final = 0.0000276944
COMPUTE_PRICE_USD_PER_GB_SECOND: Final = 0.0000036667
SNAPSHOT_WRITE_PRICE_USD_PER_GB: Final = 0.0038
SNAPSHOT_READ_PRICE_USD_PER_GB: Final = 0.00155
SNAPSHOT_STORAGE_PRICE_USD_PER_GB_MONTH: Final = 0.08
BASELINE_MIB_PER_VCPU: Final = 2048

DASHBOARD_SESSION_TOKEN_BYTES: Final = 32
DASHBOARD_START_TOKEN_TTL_SECONDS: Final = 300
DASHBOARD_REQUEST_MAX_BODY_BYTES: Final = 65536
DASHBOARD_REQUEST_TIMEOUT_SECONDS: Final = 30
DASHBOARD_READ_CACHE_SECONDS: Final = 5
DASHBOARD_POLL_INTERVAL_SECONDS: Final = 15
DASHBOARD_JOB_POLL_INTERVAL_SECONDS: Final = 1
DASHBOARD_BULK_MAX_SANDBOXES: Final = 100
DASHBOARD_JOBS_RETAINED: Final = 20
DASHBOARD_JOBS_ACTIVE_MAX: Final = 5
DASHBOARD_EVENTS_MAX_LIMIT: Final = 500
DASHBOARD_LOGS_MAX_LINES: Final = 1000
DASHBOARD_LOG_LINE_MAX_CHARS: Final = 2000
DASHBOARD_THEME_MAX_BYTES: Final = 16384
DASHBOARD_LOGO_MAX_BYTES: Final = 262144

AGENT_PREFETCH_RESTORE_JUMP_SECONDS: Final = 30
AGENT_PREFETCH_INTERVAL_SECONDS: Final = 2
AGENT_DEEPAGENTS_REQUIREMENTS_SHA256: Final = (
    "b73cb891e585c59f532eed1bfe5cf2d903ab4585d50249ae6d3e3246a205a8a3"
)

AGENT_KERNEL_WARMUP_MARKER_PATH: Final = "/opt/rayito/sidecar/ipython/startup/warmup_variant"
AGENT_KERNEL_WARMUP_SLIM_VALUE: Final = "slim"

AGENT_PREFETCH_BUILD_MARKER_PATH: Final = "/tmp/rayito-agent-prefetch.ready"
AGENT_PREFETCH_BUILD_TIMEOUT_SECONDS: Final = 300
