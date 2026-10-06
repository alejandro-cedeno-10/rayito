"""Pasarelas ya hechas para el modelo del agente (`ai-agent-core`,
ADR-025): devuelven un `SecretGateway` (ADR-023) con el `upstream`, la
cabecera y la allowlist justas para un proveedor. Restringir `allow` a los
modelos elegidos es parte del control de coste: el código del sandbox puede
llamar a la pasarela por su cuenta, fuera del presupuesto de tokens del
SDK (SECURITY.md T29), pero sólo a esos modelos y a ese ritmo.

Construir una no llama a AWS: el coste y la activación son los de
`SecretGateway` (una lectura de Secrets Manager por cabecera y TTL al
aplicarla en `Sandbox.create(gateways=...)`).
"""

from __future__ import annotations

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
_POST: Final = "POST"


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
