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
