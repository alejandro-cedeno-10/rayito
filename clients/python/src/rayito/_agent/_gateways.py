"""Pasarelas ya hechas para el modelo del agente (`ai-agent-core` y
`ai-agent-providers`, ADR-025): devuelven un `SecretGateway` (ADR-023) con
el `upstream`, la cabecera y la allowlist justas para un proveedor.
Restringir `allow` a los modelos elegidos es parte del control de coste: el
código del sandbox puede llamar a la pasarela por su cuenta, fuera del
presupuesto de tokens del SDK (SECURITY.md T29), pero sólo a esos modelos y
a ese ritmo.

Sólo Bedrock y Gemini llevan el modelo en la ruta y admiten una allowlist
por modelo. Las APIs al estilo de OpenAI (OpenAI, Azure OpenAI, xAI,
OpenRouter, Groq, Mistral, DeepSeek, LiteLLM) lo llevan en el cuerpo, que
la pasarela reenvía sin leer: con ellas el tope de gasto es el del
proveedor (presupuesto por proyecto, límite de crédito por clave). Los
valores de cada preset están en `testdata/agent/provider-catalogue.json`.

Construir una no llama a AWS: el coste y la activación son los de
`SecretGateway` (una lectura de Secrets Manager por cabecera y TTL al
aplicarla en `Sandbox.create(gateways=...)`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final
from urllib.parse import quote

from rayito._agent._domain import validate_base_path, validate_model_id, validate_region
from rayito._secret_gateway._domain import SecretGateway
from rayito.exceptions import InvalidArgumentException

if TYPE_CHECKING:
    from rayito._secrets import SecretRef

#: `bedrock-runtime.<región>.amazonaws.com`: el endpoint de `Converse` y
#: `ConverseStream` (docs/research/2026-10-agent-spike.md, medido por la
#: pasarela desde el sandbox).
BEDROCK_RUNTIME_UPSTREAM_TEMPLATE: Final = "https://bedrock-runtime.{region}.amazonaws.com"
#: Las dos operaciones que usan OpenCode (`@ai-sdk/amazon-bedrock`) y
#: `ChatBedrockConverse` (botocore). Ambos codifican el id del modelo con
#: `%3A` en vez de `:`, y `rayd` compara la ruta en crudo, así que la regla
#: lleva el id ya codificado.
BEDROCK_OPERATIONS: Final[tuple[str, ...]] = ("converse-stream", "converse")
#: Bedrock lee la clave de API (`ABSK...`) de `authorization: Bearer <clave>`:
#: el secreto guarda el valor entero, `Bearer ` incluido.
BEDROCK_AUTH_HEADER: Final = "authorization"
ANTHROPIC_UPSTREAM: Final = "https://api.anthropic.com"
ANTHROPIC_AUTH_HEADER: Final = "x-api-key"
ANTHROPIC_MESSAGES_PATH: Final = "/v1/messages"
#: Una API compatible con OpenAI lee `authorization: Bearer <clave>`: el
#: secreto guarda el valor entero, `Bearer ` incluido.
OPENAI_AUTH_HEADER: Final = "authorization"
OPENAI_CHAT_COMPLETIONS_PATH: Final = "/chat/completions"
#: La Responses API, que OpenCode usa con `@ai-sdk/openai`, `@ai-sdk/azure`
#: y `@ai-sdk/xai` (`sdk.responses(...)`).
OPENAI_RESPONSES_PATH: Final = "/responses"
#: Las dos operaciones de las APIs de OpenAI y xAI que permiten los presets.
OPENAI_STYLE_OPERATIONS: Final[tuple[str, ...]] = (
    OPENAI_RESPONSES_PATH,
    OPENAI_CHAT_COMPLETIONS_PATH,
)
#: platform.openai.com/docs/api-reference (consultado el 2026-10-07).
OPENAI_UPSTREAM: Final = "https://api.openai.com"
OPENAI_BASE_PATH: Final = "/v1"
#: Gemini API (AI Studio): la clave va en `x-goog-api-key`
#: (ai.google.dev/gemini-api/docs/api-key, consultado el 2026-10-07).
GEMINI_UPSTREAM: Final = "https://generativelanguage.googleapis.com"
GEMINI_AUTH_HEADER: Final = "x-goog-api-key"
GEMINI_MODELS_PATH: Final = "/v1beta/models"
#: Las dos operaciones por modelo de `@ai-sdk/google` y
#: `ChatGoogleGenerativeAI`; `?alt=sse` va en la query, que `rayd` no
#: compara.
GEMINI_OPERATIONS: Final[tuple[str, ...]] = ("streamGenerateContent", "generateContent")
#: Azure OpenAI v1 GA: `https://<recurso>.openai.azure.com/openai/v1/`, sin
#: `api-version` obligatoria y con la clave en `api-key`
#: (learn.microsoft.com/azure/ai-foundry/openai/api-version-lifecycle,
#: consultado el 2026-10-07).
AZURE_OPENAI_UPSTREAM_TEMPLATE: Final = "https://{resource}.openai.azure.com"
AZURE_OPENAI_AUTH_HEADER: Final = "api-key"
AZURE_OPENAI_BASE_PATH: Final = "/openai/v1"
#: El subdominio propio de un recurso de Azure es una etiqueta DNS: 1-63
#: caracteres `[a-z0-9-]` sin guion al principio ni al final.
MAX_DNS_LABEL_LEN: Final = 63
#: openrouter.ai/docs/api/reference/authentication (consultado el
#: 2026-10-07).
OPENROUTER_UPSTREAM: Final = "https://openrouter.ai"
OPENROUTER_BASE_PATH: Final = "/api/v1"
#: console.groq.com/docs/openai (consultado el 2026-10-07).
GROQ_UPSTREAM: Final = "https://api.groq.com"
GROQ_BASE_PATH: Final = "/openai/v1"
#: docs.mistral.ai/api (consultado el 2026-10-07).
MISTRAL_UPSTREAM: Final = "https://api.mistral.ai"
MISTRAL_BASE_PATH: Final = "/v1"
#: api-docs.deepseek.com: la base compatible con OpenAI es la raíz
#: (consultado el 2026-10-07).
DEEPSEEK_UPSTREAM: Final = "https://api.deepseek.com"
DEEPSEEK_BASE_PATH: Final = ""
#: docs.x.ai/docs/api-reference (consultado el 2026-10-07).
XAI_UPSTREAM: Final = "https://api.x.ai"
XAI_BASE_PATH: Final = "/v1"
#: El proxy de LiteLLM sirve la API de OpenAI bajo `/v1` (y también en la
#: raíz) con una clave virtual en `authorization: Bearer`
#: (docs.litellm.ai/docs/proxy/user_keys, consultado el 2026-10-07).
#: `https://host[:puerto]` del proxy propio (LiteLLM): etiquetas DNS o
#: una IPv4; el mismo patrón está en el SDK de TypeScript.
PROXY_UPSTREAM_PATTERN: Final = re.compile(
    r"https://(?P<host>[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)(?::(?P<port>[0-9]{1,5}))?"
)
IPV4_PATTERN: Final = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}")
LOCALHOST: Final = "localhost"
#: `0.0.0.0/8` y `127.0.0.0/8` (loopback).
RESERVED_IPV4_FIRST_OCTETS: Final = (0, 127)
#: `169.254.0.0/16`: enlace local (metadatos de la instancia).
LINK_LOCAL_IPV4_PREFIX: Final = (169, 254)
MAX_IPV4_OCTET: Final = 255
MIN_PORT: Final = 1
MAX_PORT: Final = 65535
LITELLM_DEFAULT_BASE_PATH: Final = "/v1"
_POST: Final = "POST"
_DNS_LABEL_PATTERN: Final = re.compile(
    rf"[a-z0-9](?:[a-z0-9-]{{0,{MAX_DNS_LABEL_LEN - 2}}}[a-z0-9])?"
)


def bedrock_model_path(model_id: str, operation: str) -> str:
    """`/model/<id codificado>/<operación>`, con el id codificado entero
    (`:` → `%3A`), como lo envían los clientes de Bedrock."""
    return f"/model/{quote(model_id, safe='')}/{operation}"


def bedrock_gateway(
    secret: str | SecretRef,
    *,
    region: str,
    models: Sequence[str],
    rate_per_minute: int = 0,
) -> SecretGateway:
    """Pasarela hacia Bedrock (`Converse`/`ConverseStream`) para los
    `models` dados (ids o perfiles de inferencia, como
    `us.anthropic.claude-haiku-4-5-20251001-v1:0`). `secret` nombra un
    secreto con `Bearer <clave de API de Bedrock>`; una clave de corta
    duración se rota con `sbx.gateways.refresh()`."""
    validate_region(region, "bedrock_gateway(region=)")
    model_ids = _model_list(models, "bedrock_gateway(models=)")
    for model_id in model_ids:
        if "/" in model_id:
            raise InvalidArgumentException(
                "bedrock_gateway(models=) no admite ARNs: la pasarela rechaza un '/' "
                "codificado en la ruta; usa el id del modelo o del perfil de inferencia"
            )
    allow = [
        (_POST, bedrock_model_path(model_id, operation))
        for model_id in model_ids
        for operation in BEDROCK_OPERATIONS
    ]
    return SecretGateway(
        upstream=BEDROCK_RUNTIME_UPSTREAM_TEMPLATE.format(region=region),
        headers={BEDROCK_AUTH_HEADER: secret},
        allow=allow,
        rate_per_minute=rate_per_minute,
    )


def anthropic_gateway(secret: str | SecretRef, *, rate_per_minute: int = 0) -> SecretGateway:
    """Pasarela hacia la Messages API de Anthropic. `secret` nombra un
    secreto con la clave de API (`x-api-key`)."""
    return SecretGateway(
        upstream=ANTHROPIC_UPSTREAM,
        headers={ANTHROPIC_AUTH_HEADER: secret},
        allow=[(_POST, ANTHROPIC_MESSAGES_PATH)],
        rate_per_minute=rate_per_minute,
    )


def openai_compatible_gateway(
    secret: str | SecretRef,
    *,
    upstream: str,
    base_path: str = "",
    rate_per_minute: int = 0,
) -> SecretGateway:
    """Pasarela hacia una API compatible con OpenAI (`POST
    <base_path>/chat/completions`). `upstream` es `https://host`;
    `base_path` es el mismo que `AgentModel.base_path` (`"/v1"`). `secret`
    nombra un secreto con `Bearer <clave>`."""
    validate_base_path(base_path, "openai_compatible_gateway(base_path=)")
    return _chat_completions_gateway(secret, upstream, base_path, rate_per_minute)


def openai_gateway(secret: str | SecretRef, *, rate_per_minute: int = 0) -> SecretGateway:
    """Pasarela hacia la API de OpenAI (`POST /v1/responses` y `POST
    /v1/chat/completions`), para `AgentModel(provider="openai")`. `secret`
    nombra un secreto con `Bearer <clave>`. La pasarela no puede limitar el
    modelo (va en el cuerpo): fija el presupuesto y los modelos permitidos
    en el proyecto de OpenAI de la clave (SECURITY.md T29)."""
    return _bearer_gateway(secret, OPENAI_UPSTREAM, OPENAI_BASE_PATH, rate_per_minute)


def gemini_gateway(
    secret: str | SecretRef, *, models: Sequence[str], rate_per_minute: int = 0
) -> SecretGateway:
    """Pasarela hacia la Gemini API (AI Studio) para los `models` dados
    (`gemini-2.5-flash`), para `AgentModel(provider="google")`. El modelo va
    en la ruta, así que la allowlist sí lo limita: incluye también el
    `small_model` del agente. `secret` nombra un secreto con la clave
    (`x-goog-api-key`)."""
    label = "gemini_gateway(models=)"
    model_ids = _model_list(models, label)
    for model_id in model_ids:
        if "/" in model_id:
            raise InvalidArgumentException(
                f"{label} no admite '/': usa el id del modelo ('gemini-2.5-flash'), "
                "sin el prefijo 'models/'"
            )
    allow = [
        (_POST, f"{GEMINI_MODELS_PATH}/{model_id}:{operation}")
        for model_id in model_ids
        for operation in GEMINI_OPERATIONS
    ]
    return SecretGateway(
        upstream=GEMINI_UPSTREAM,
        headers={GEMINI_AUTH_HEADER: secret},
        allow=allow,
        rate_per_minute=rate_per_minute,
    )


def azure_openai_gateway(
    secret: str | SecretRef, *, resource: str, rate_per_minute: int = 0
) -> SecretGateway:
    """Pasarela hacia Azure OpenAI v1 (`POST /openai/v1/responses` y `POST
    /openai/v1/chat/completions`) del recurso `resource` (el subdominio de
    `https://<resource>.openai.azure.com`), para
    `AgentModel(provider="azure")`. `secret` nombra un secreto con la clave
    del recurso (`api-key`, sin `Bearer`). El modelo (el despliegue) va en
    el cuerpo: limita los despliegues y la cuota en el propio recurso."""
    if not isinstance(resource, str) or not _DNS_LABEL_PATTERN.fullmatch(resource):
        raise InvalidArgumentException(
            f"azure_openai_gateway(resource=) debe ser una etiqueta DNS de 1-{MAX_DNS_LABEL_LEN} "
            "caracteres [a-z0-9-], sin guion al principio ni al final"
        )
    return SecretGateway(
        upstream=AZURE_OPENAI_UPSTREAM_TEMPLATE.format(resource=resource),
        headers={AZURE_OPENAI_AUTH_HEADER: secret},
        allow=_openai_style_allow(AZURE_OPENAI_BASE_PATH),
        rate_per_minute=rate_per_minute,
    )


def openrouter_gateway(secret: str | SecretRef, *, rate_per_minute: int = 0) -> SecretGateway:
    """Pasarela hacia OpenRouter (`POST /api/v1/chat/completions`), para
    `AgentModel(provider="openai-compatible", base_path="/api/v1")`.
    `secret` guarda `Bearer <clave>`. Fija un límite de crédito en la clave:
    la pasarela no limita el modelo."""
    return _chat_completions_gateway(
        secret, OPENROUTER_UPSTREAM, OPENROUTER_BASE_PATH, rate_per_minute
    )


def groq_gateway(secret: str | SecretRef, *, rate_per_minute: int = 0) -> SecretGateway:
    """Pasarela hacia Groq (`POST /openai/v1/chat/completions`), para
    `AgentModel(provider="openai-compatible", base_path="/openai/v1")`.
    `secret` guarda `Bearer <clave>`."""
    return _chat_completions_gateway(secret, GROQ_UPSTREAM, GROQ_BASE_PATH, rate_per_minute)


def mistral_gateway(secret: str | SecretRef, *, rate_per_minute: int = 0) -> SecretGateway:
    """Pasarela hacia Mistral (`POST /v1/chat/completions`), para
    `AgentModel(provider="openai-compatible", base_path="/v1")`. `secret`
    guarda `Bearer <clave>`."""
    return _chat_completions_gateway(secret, MISTRAL_UPSTREAM, MISTRAL_BASE_PATH, rate_per_minute)


def deepseek_gateway(secret: str | SecretRef, *, rate_per_minute: int = 0) -> SecretGateway:
    """Pasarela hacia DeepSeek (`POST /chat/completions`), para
    `AgentModel(provider="openai-compatible")` sin `base_path`. `secret`
    guarda `Bearer <clave>`."""
    return _chat_completions_gateway(secret, DEEPSEEK_UPSTREAM, DEEPSEEK_BASE_PATH, rate_per_minute)


def xai_gateway(secret: str | SecretRef, *, rate_per_minute: int = 0) -> SecretGateway:
    """Pasarela hacia la API de xAI (`POST /v1/responses` y `POST
    /v1/chat/completions`), para `AgentModel(provider="openai")`: OpenCode
    y deepagents le hablan como a OpenAI. `secret` guarda `Bearer <clave>`
    de la API de xAI (nunca una sesión de SuperGrok)."""
    return _bearer_gateway(secret, XAI_UPSTREAM, XAI_BASE_PATH, rate_per_minute)


def litellm_gateway(
    secret: str | SecretRef,
    *,
    upstream: str,
    base_path: str = LITELLM_DEFAULT_BASE_PATH,
    rate_per_minute: int = 0,
) -> SecretGateway:
    """Pasarela hacia un proxy de LiteLLM propio (`POST <base_path>/responses`
    y `POST <base_path>/chat/completions`), para
    `AgentModel(provider="openai-compatible", base_path=base_path)`.
    `upstream` es `https://host[:puerto]` alcanzable desde la VPC del
    sandbox (nunca loopback, enlace local ni `0.0.0.0`); `secret` guarda `Bearer <clave virtual>`.
    Los presupuestos y modelos por clave virtual de LiteLLM son el tope de
    gasto: la pasarela no limita el modelo."""
    validate_base_path(base_path, "litellm_gateway(base_path=)")
    _validate_proxy_upstream(upstream, "litellm_gateway(upstream=)")
    return _bearer_gateway(secret, upstream, base_path, rate_per_minute)


def _validate_proxy_upstream(upstream: object, label: str) -> None:
    """`https://host[:puerto]` con un nombre DNS o una IPv4 que no sea de
    loopback, enlace local ni `0.0.0.0`: el sandbox no alcanza el
    `localhost` del llamante y `169.254.0.0/16` es el servicio de
    metadatos de la instancia."""
    match = PROXY_UPSTREAM_PATTERN.fullmatch(upstream) if isinstance(upstream, str) else None
    if match is None:
        raise InvalidArgumentException(
            f"{label} debe ser 'https://host[:puerto]' con un nombre DNS o una IPv4"
        )
    host = match.group("host").lower()
    port = match.group("port")
    if (
        host == LOCALHOST
        or host.endswith(f".{LOCALHOST}")
        or (IPV4_PATTERN.fullmatch(host) is not None and _is_reserved_ipv4(host))
        or (port is not None and not MIN_PORT <= int(port) <= MAX_PORT)
    ):
        raise InvalidArgumentException(
            f"{label} no puede apuntar a loopback, enlace local ni a un puerto fuera de rango"
        )


def _is_reserved_ipv4(host: str) -> bool:
    octets = [int(octet) for octet in host.split(".")]
    if any(octet > MAX_IPV4_OCTET for octet in octets):
        return True
    return octets[0] in RESERVED_IPV4_FIRST_OCTETS or tuple(octets[:2]) == LINK_LOCAL_IPV4_PREFIX


def _openai_style_allow(base_path: str) -> list[tuple[str, str]]:
    return [(_POST, f"{base_path}{operation}") for operation in OPENAI_STYLE_OPERATIONS]


def _bearer_gateway(
    secret: str | SecretRef, upstream: str, base_path: str, rate_per_minute: int
) -> SecretGateway:
    return SecretGateway(
        upstream=upstream,
        headers={OPENAI_AUTH_HEADER: secret},
        allow=_openai_style_allow(base_path),
        rate_per_minute=rate_per_minute,
    )


def _chat_completions_gateway(
    secret: str | SecretRef, upstream: str, base_path: str, rate_per_minute: int
) -> SecretGateway:
    return SecretGateway(
        upstream=upstream,
        headers={OPENAI_AUTH_HEADER: secret},
        allow=[(_POST, f"{base_path}{OPENAI_CHAT_COMPLETIONS_PATH}")],
        rate_per_minute=rate_per_minute,
    )


def _model_list(models: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(models, str) or not isinstance(models, Sequence) or not models:
        raise InvalidArgumentException(f"{label} debe ser una lista no vacía de ids de modelo")
    unique: dict[str, None] = {}
    for model_id in models:
        unique[validate_model_id(model_id, label)] = None
    return tuple(unique)
