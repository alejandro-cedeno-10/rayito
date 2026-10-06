/**
 * Pasarelas ya hechas para el modelo del agente (`ai-agent-core`, ADR-025).
 * Espejo de `rayito._agent._gateways`: devuelven un `SecretGateway`
 * (ADR-023) con el `upstream`, la cabecera y la allowlist justas para un
 * proveedor. Restringir `allow` a los modelos elegidos es parte del control
 * de coste (SECURITY.md T29). Construir una no llama a AWS: el coste y la
 * activación son los de `SecretGateway`.
 */

import { InvalidArgumentError } from "../errors.js";
import { SecretGateway } from "../secret-gateway/domain.js";
import type { SecretLike } from "../secrets/names.js";
import { validateBasePath, validateModelId, validateRegion } from "./domain.js";

/** `Converse` y `ConverseStream` (docs/research/2026-10-agent-spike.md). */
export const BEDROCK_RUNTIME_UPSTREAM_PREFIX = "https://bedrock-runtime.";
export const BEDROCK_RUNTIME_UPSTREAM_SUFFIX = ".amazonaws.com";
/** OpenCode y botocore codifican el id con `%3A` y `rayd` compara la ruta
 * en crudo: la regla lleva el id ya codificado. */
export const BEDROCK_OPERATIONS = ["converse-stream", "converse"] as const;
/** El secreto guarda `Bearer <clave de API de Bedrock>` entero. */
export const BEDROCK_AUTH_HEADER = "authorization";
export const ANTHROPIC_UPSTREAM = "https://api.anthropic.com";
export const ANTHROPIC_AUTH_HEADER = "x-api-key";
export const ANTHROPIC_MESSAGES_PATH = "/v1/messages";
/** El secreto guarda `Bearer <clave>` entero. */
export const OPENAI_AUTH_HEADER = "authorization";
export const OPENAI_CHAT_COMPLETIONS_PATH = "/chat/completions";
const POST = "POST";

/** `/model/<id codificado>/<operación>`, como lo envían los clientes de
 * Bedrock (`:` → `%3A`). */
export function bedrockModelPath(modelId: string, operation: string): string {
  return `/model/${encodeURIComponent(modelId)}/${operation}`;
}

export interface BedrockGatewayOptions {
  readonly region: string;
  readonly models: readonly string[];
  readonly ratePerMinute?: number | undefined;
}

/**
 * Pasarela hacia Bedrock (`Converse`/`ConverseStream`) para los `models`
 * dados. `secret` nombra un secreto con `Bearer <clave de API de Bedrock>`;
 * una clave de corta duración se rota con `sbx.gateways.refresh()`.
 */
export function bedrockGateway(secret: SecretLike, options: BedrockGatewayOptions): SecretGateway {
  validateRegion(options.region, "bedrockGateway({ region })");
  const modelIds = modelList(options.models, "bedrockGateway({ models })");
  for (const modelId of modelIds) {
    if (modelId.includes("/")) {
      throw new InvalidArgumentError(
        "bedrockGateway({ models }) no admite ARNs: la pasarela rechaza un '/' codificado en " +
          "la ruta; usa el id del modelo o del perfil de inferencia",
      );
    }
  }
  const allow = modelIds.flatMap((modelId) =>
    BEDROCK_OPERATIONS.map((operation) => [POST, bedrockModelPath(modelId, operation)] as const),
  );
  return new SecretGateway({
    upstream: `${BEDROCK_RUNTIME_UPSTREAM_PREFIX}${options.region}${BEDROCK_RUNTIME_UPSTREAM_SUFFIX}`,
    headers: { [BEDROCK_AUTH_HEADER]: secret },
    allow,
    ratePerMinute: options.ratePerMinute,
  });
}

export interface AnthropicGatewayOptions {
  readonly ratePerMinute?: number | undefined;
}

/** Pasarela hacia la Messages API de Anthropic (`x-api-key`). */
export function anthropicGateway(
  secret: SecretLike,
  options: AnthropicGatewayOptions = {},
): SecretGateway {
  return new SecretGateway({
    upstream: ANTHROPIC_UPSTREAM,
    headers: { [ANTHROPIC_AUTH_HEADER]: secret },
    allow: [[POST, ANTHROPIC_MESSAGES_PATH]],
    ratePerMinute: options.ratePerMinute,
  });
}

export interface OpenAiCompatibleGatewayOptions {
  readonly upstream: string;
  readonly basePath?: string | undefined;
  readonly ratePerMinute?: number | undefined;
}

/** Pasarela hacia una API compatible con OpenAI (`POST
 * <basePath>/chat/completions`); `secret` guarda `Bearer <clave>`. */
export function openaiCompatibleGateway(
  secret: SecretLike,
  options: OpenAiCompatibleGatewayOptions,
): SecretGateway {
  const basePath = validateBasePath(
    options.basePath ?? "",
    "openaiCompatibleGateway({ basePath })",
  );
  return new SecretGateway({
    upstream: options.upstream,
    headers: { [OPENAI_AUTH_HEADER]: secret },
    allow: [[POST, `${basePath}${OPENAI_CHAT_COMPLETIONS_PATH}`]],
    ratePerMinute: options.ratePerMinute,
  });
}

function modelList(models: readonly string[], label: string): readonly string[] {
  if (!Array.isArray(models) || models.length === 0) {
    throw new InvalidArgumentError(`${label} debe ser una lista no vacía de ids de modelo`);
  }
  return [...new Set(models.map((modelId) => validateModelId(modelId, label)))];
}
