/**
 * Pasarelas ya hechas para el modelo del agente (`ai-agent-core` y
 * `ai-agent-providers`, ADR-025). Espejo de `rayito._agent._gateways`:
 * devuelven un `SecretGateway` (ADR-023) con el `upstream`, la cabecera y la
 * allowlist justas para un proveedor. Restringir `allow` a los modelos
 * elegidos es parte del control de coste (SECURITY.md T29). Sólo Bedrock y
 * Gemini llevan el modelo en la ruta; las APIs al estilo de OpenAI lo llevan
 * en el cuerpo y su tope de gasto es el del proveedor. Construir una no
 * llama a AWS: el coste y la activación son los de `SecretGateway`. Los
 * valores están en `testdata/agent/provider-catalogue.json`.
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
/** La Responses API, que OpenCode usa con `@ai-sdk/openai`, `@ai-sdk/azure`
 * y `@ai-sdk/xai`. */
export const OPENAI_RESPONSES_PATH = "/responses";
/** Las dos operaciones de las APIs de OpenAI y xAI que permiten los presets. */
export const OPENAI_STYLE_OPERATIONS = [
  OPENAI_RESPONSES_PATH,
  OPENAI_CHAT_COMPLETIONS_PATH,
] as const;
/** platform.openai.com/docs/api-reference (consultado el 2026-10-07). */
export const OPENAI_UPSTREAM = "https://api.openai.com";
export const OPENAI_BASE_PATH = "/v1";
/** Gemini API (AI Studio), clave en `x-goog-api-key`
 * (ai.google.dev/gemini-api/docs/api-key, consultado el 2026-10-07). */
export const GEMINI_UPSTREAM = "https://generativelanguage.googleapis.com";
export const GEMINI_AUTH_HEADER = "x-goog-api-key";
export const GEMINI_MODELS_PATH = "/v1beta/models";
/** Las dos operaciones por modelo; `?alt=sse` va en la query, que `rayd`
 * no compara. */
export const GEMINI_OPERATIONS = ["streamGenerateContent", "generateContent"] as const;
/** Azure OpenAI v1 GA, clave en `api-key`
 * (learn.microsoft.com/azure/ai-foundry/openai/api-version-lifecycle,
 * consultado el 2026-10-07). */
export const AZURE_OPENAI_UPSTREAM_PREFIX = "https://";
export const AZURE_OPENAI_UPSTREAM_SUFFIX = ".openai.azure.com";
export const AZURE_OPENAI_AUTH_HEADER = "api-key";
export const AZURE_OPENAI_BASE_PATH = "/openai/v1";
/** El subdominio de un recurso de Azure es una etiqueta DNS. */
export const MAX_DNS_LABEL_LEN = 63;
/** openrouter.ai/docs/api/reference/authentication (consultado el 2026-10-07). */
export const OPENROUTER_UPSTREAM = "https://openrouter.ai";
export const OPENROUTER_BASE_PATH = "/api/v1";
/** console.groq.com/docs/openai (consultado el 2026-10-07). */
export const GROQ_UPSTREAM = "https://api.groq.com";
export const GROQ_BASE_PATH = "/openai/v1";
/** docs.mistral.ai/api (consultado el 2026-10-07). */
export const MISTRAL_UPSTREAM = "https://api.mistral.ai";
export const MISTRAL_BASE_PATH = "/v1";
/** api-docs.deepseek.com: la base compatible con OpenAI es la raíz
 * (consultado el 2026-10-07). */
export const DEEPSEEK_UPSTREAM = "https://api.deepseek.com";
export const DEEPSEEK_BASE_PATH = "";
/** docs.x.ai/docs/api-reference (consultado el 2026-10-07). */
export const XAI_UPSTREAM = "https://api.x.ai";
export const XAI_BASE_PATH = "/v1";
/** `https://host[:puerto]` del proxy propio (LiteLLM); el mismo patrón está en Python. */
const PROXY_UPSTREAM_PATTERN =
  /^https:\/\/(?<host>[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)(?::(?<port>[0-9]{1,5}))?$/;
const IPV4_PATTERN = /^[0-9]{1,3}(?:\.[0-9]{1,3}){3}$/;
const LOCALHOST = "localhost";
/** `0.0.0.0/8` y `127.0.0.0/8` (loopback). */
const RESERVED_IPV4_FIRST_OCTETS = [0, 127] as const;
/** `169.254.0.0/16`: enlace local (metadatos de la instancia). */
const LINK_LOCAL_IPV4_PREFIX = [169, 254] as const;
const MAX_IPV4_OCTET = 255;
const MIN_PORT = 1;
const MAX_PORT = 65535;
/** El proxy de LiteLLM sirve la API de OpenAI bajo `/v1` con una clave
 * virtual en `authorization: Bearer` (docs.litellm.ai/docs/proxy/user_keys,
 * consultado el 2026-10-07). */
export const LITELLM_DEFAULT_BASE_PATH = "/v1";
const POST = "POST";
const DNS_LABEL_PATTERN = new RegExp(
  `^[a-z0-9](?:[a-z0-9-]{0,${MAX_DNS_LABEL_LEN - 2}}[a-z0-9])?$`,
);

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
  return chatCompletionsGateway(secret, options.upstream, basePath, options.ratePerMinute);
}

/** Opciones comunes de los presets con upstream fijo. */
export interface ProviderGatewayOptions {
  readonly ratePerMinute?: number | undefined;
}

/**
 * Pasarela hacia la API de OpenAI (`POST /v1/responses` y `POST
 * /v1/chat/completions`), para `AgentModel({ provider: "openai" })`.
 * `secret` guarda `Bearer <clave>`. La pasarela no puede limitar el modelo
 * (va en el cuerpo): fija presupuesto y modelos en el proyecto de OpenAI.
 */
export function openaiGateway(
  secret: SecretLike,
  options: ProviderGatewayOptions = {},
): SecretGateway {
  return bearerGateway(secret, OPENAI_UPSTREAM, OPENAI_BASE_PATH, options.ratePerMinute);
}

export interface GeminiGatewayOptions {
  readonly models: readonly string[];
  readonly ratePerMinute?: number | undefined;
}

/**
 * Pasarela hacia la Gemini API (AI Studio) para los `models` dados, para
 * `AgentModel({ provider: "google" })`. El modelo va en la ruta, así que la
 * allowlist sí lo limita: incluye también el `smallModel` del agente.
 * `secret` guarda la clave (`x-goog-api-key`).
 */
export function geminiGateway(secret: SecretLike, options: GeminiGatewayOptions): SecretGateway {
  const label = "geminiGateway({ models })";
  const modelIds = modelList(options.models, label);
  for (const modelId of modelIds) {
    if (modelId.includes("/")) {
      throw new InvalidArgumentError(
        `${label} no admite '/': usa el id del modelo ('gemini-2.5-flash'), sin el prefijo 'models/'`,
      );
    }
  }
  const allow = modelIds.flatMap((modelId) =>
    GEMINI_OPERATIONS.map(
      (operation) => [POST, `${GEMINI_MODELS_PATH}/${modelId}:${operation}`] as const,
    ),
  );
  return new SecretGateway({
    upstream: GEMINI_UPSTREAM,
    headers: { [GEMINI_AUTH_HEADER]: secret },
    allow,
    ratePerMinute: options.ratePerMinute,
  });
}

export interface AzureOpenaiGatewayOptions {
  readonly resource: string;
  readonly ratePerMinute?: number | undefined;
}

/**
 * Pasarela hacia Azure OpenAI v1 (`POST /openai/v1/responses` y `POST
 * /openai/v1/chat/completions`) del recurso `resource`
 * (`https://<resource>.openai.azure.com`), para `AgentModel({ provider:
 * "azure" })`. `secret` guarda la clave del recurso (`api-key`, sin
 * `Bearer`). Limita despliegues y cuota en el propio recurso.
 */
export function azureOpenaiGateway(
  secret: SecretLike,
  options: AzureOpenaiGatewayOptions,
): SecretGateway {
  const resource: unknown = options.resource;
  if (typeof resource !== "string" || !DNS_LABEL_PATTERN.test(resource)) {
    throw new InvalidArgumentError(
      `azureOpenaiGateway({ resource }) debe ser una etiqueta DNS de 1-${MAX_DNS_LABEL_LEN} ` +
        "caracteres [a-z0-9-], sin guion al principio ni al final",
    );
  }
  return new SecretGateway({
    upstream: `${AZURE_OPENAI_UPSTREAM_PREFIX}${resource}${AZURE_OPENAI_UPSTREAM_SUFFIX}`,
    headers: { [AZURE_OPENAI_AUTH_HEADER]: secret },
    allow: openaiStyleAllow(AZURE_OPENAI_BASE_PATH),
    ratePerMinute: options.ratePerMinute,
  });
}

/** Pasarela hacia OpenRouter (`POST /api/v1/chat/completions`), para
 * `AgentModel({ provider: "openai-compatible", basePath: "/api/v1" })`.
 * Fija un límite de crédito en la clave. */
export function openrouterGateway(
  secret: SecretLike,
  options: ProviderGatewayOptions = {},
): SecretGateway {
  return chatCompletionsGateway(
    secret,
    OPENROUTER_UPSTREAM,
    OPENROUTER_BASE_PATH,
    options.ratePerMinute,
  );
}

/** Pasarela hacia Groq (`POST /openai/v1/chat/completions`), para
 * `AgentModel({ provider: "openai-compatible", basePath: "/openai/v1" })`. */
export function groqGateway(
  secret: SecretLike,
  options: ProviderGatewayOptions = {},
): SecretGateway {
  return chatCompletionsGateway(secret, GROQ_UPSTREAM, GROQ_BASE_PATH, options.ratePerMinute);
}

/** Pasarela hacia Mistral (`POST /v1/chat/completions`), para
 * `AgentModel({ provider: "openai-compatible", basePath: "/v1" })`. */
export function mistralGateway(
  secret: SecretLike,
  options: ProviderGatewayOptions = {},
): SecretGateway {
  return chatCompletionsGateway(secret, MISTRAL_UPSTREAM, MISTRAL_BASE_PATH, options.ratePerMinute);
}

/** Pasarela hacia DeepSeek (`POST /chat/completions`), para
 * `AgentModel({ provider: "openai-compatible" })` sin `basePath`. */
export function deepseekGateway(
  secret: SecretLike,
  options: ProviderGatewayOptions = {},
): SecretGateway {
  return chatCompletionsGateway(
    secret,
    DEEPSEEK_UPSTREAM,
    DEEPSEEK_BASE_PATH,
    options.ratePerMinute,
  );
}

/** Pasarela hacia la API de xAI (`POST /v1/responses` y `POST
 * /v1/chat/completions`), para `AgentModel({ provider: "openai" })`.
 * `secret` guarda `Bearer <clave de API de xAI>`, nunca una sesión de
 * SuperGrok. */
export function xaiGateway(
  secret: SecretLike,
  options: ProviderGatewayOptions = {},
): SecretGateway {
  return bearerGateway(secret, XAI_UPSTREAM, XAI_BASE_PATH, options.ratePerMinute);
}

export interface LitellmGatewayOptions {
  readonly upstream: string;
  readonly basePath?: string | undefined;
  readonly ratePerMinute?: number | undefined;
}

/**
 * Pasarela hacia un proxy de LiteLLM propio (`POST <basePath>/responses`
 * y `POST <basePath>/chat/completions`), para
 * `AgentModel({ provider: "openai-compatible", basePath })`. `upstream` es
 * `https://host[:puerto]` alcanzable desde la VPC del sandbox (nunca
 * loopback, enlace local ni `0.0.0.0`); `secret` guarda
 * `Bearer <clave virtual>`. Los presupuestos de la clave virtual son el
 * tope de gasto: la pasarela no limita el modelo.
 */
export function litellmGateway(secret: SecretLike, options: LitellmGatewayOptions): SecretGateway {
  const basePath = validateBasePath(
    options.basePath ?? LITELLM_DEFAULT_BASE_PATH,
    "litellmGateway({ basePath })",
  );
  validateProxyUpstream(options.upstream, "litellmGateway({ upstream })");
  return bearerGateway(secret, options.upstream, basePath, options.ratePerMinute);
}

/**
 * `https://host[:puerto]` con un nombre DNS o una IPv4 que no sea de
 * loopback, enlace local ni `0.0.0.0`: el sandbox no alcanza el `localhost`
 * del llamante y `169.254.0.0/16` es el servicio de metadatos.
 */
function validateProxyUpstream(upstream: unknown, label: string): void {
  const match = typeof upstream === "string" ? PROXY_UPSTREAM_PATTERN.exec(upstream) : null;
  if (match === null) {
    throw new InvalidArgumentError(
      `${label} debe ser 'https://host[:puerto]' con un nombre DNS o una IPv4`,
    );
  }
  const host = (match.groups?.host ?? "").toLowerCase();
  const port = match.groups?.port;
  if (
    host === LOCALHOST ||
    host.endsWith(`.${LOCALHOST}`) ||
    (IPV4_PATTERN.test(host) && isReservedIpv4(host)) ||
    (port !== undefined && (Number(port) < MIN_PORT || Number(port) > MAX_PORT))
  ) {
    throw new InvalidArgumentError(
      `${label} no puede apuntar a loopback, enlace local ni a un puerto fuera de rango`,
    );
  }
}

function isReservedIpv4(host: string): boolean {
  const octets = host.split(".").map(Number);
  if (octets.some((octet) => octet > MAX_IPV4_OCTET)) {
    return true;
  }
  const [first, second] = octets;
  return (
    (RESERVED_IPV4_FIRST_OCTETS as readonly number[]).includes(first ?? -1) ||
    (first === LINK_LOCAL_IPV4_PREFIX[0] && second === LINK_LOCAL_IPV4_PREFIX[1])
  );
}

function openaiStyleAllow(basePath: string): (readonly [string, string])[] {
  return OPENAI_STYLE_OPERATIONS.map((operation) => [POST, `${basePath}${operation}`] as const);
}

function bearerGateway(
  secret: SecretLike,
  upstream: string,
  basePath: string,
  ratePerMinute: number | undefined,
): SecretGateway {
  return new SecretGateway({
    upstream,
    headers: { [OPENAI_AUTH_HEADER]: secret },
    allow: openaiStyleAllow(basePath),
    ratePerMinute,
  });
}

function chatCompletionsGateway(
  secret: SecretLike,
  upstream: string,
  basePath: string,
  ratePerMinute: number | undefined,
): SecretGateway {
  return new SecretGateway({
    upstream,
    headers: { [OPENAI_AUTH_HEADER]: secret },
    allow: [[POST, `${basePath}${OPENAI_CHAT_COMPLETIONS_PATH}`]],
    ratePerMinute,
  });
}

function modelList(models: readonly string[], label: string): readonly string[] {
  if (!Array.isArray(models) || models.length === 0) {
    throw new InvalidArgumentError(`${label} debe ser una lista no vacía de ids de modelo`);
  }
  return [...new Set(models.map((modelId) => validateModelId(modelId, label)))];
}
