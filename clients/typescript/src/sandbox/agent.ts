/**
 * `sandbox.agent`: `sbx.agent.run/stream/prepare` (`ai-agent-core`,
 * design.md §4). Aplica la configuración del runtime con `files.writeFiles`
 * (una vez por sha), lanza el script del runtime con `commands.run` en
 * background con stdin y trocea su stdout en eventos, imponiendo
 * `AgentLimits` sobre ellos. `sbx.agent` en sí es perezoso: construirlo (el
 * campo `agent` del `Sandbox`) no manda ningún RPC; lo primero en costar
 * algo es el primer `run()`/`stream()`.
 */

import type { Span } from "@opentelemetry/api";
import { AgentLimits, type AgentSpec, DEFAULT_AGENT_RUNTIME } from "../agent/domain.js";
import type { AgentResult } from "../agent/events.js";
import type { AgentRuntime } from "../agent/runtime.js";
import { resolveRuntime } from "../agent/runtimes.js";
import {
  AGENT_RUN_TAG,
  type AgentCommandHandle,
  type AgentSandbox,
  AgentStream,
  buildRunRequest,
  ConfigCache,
  gatewayUrlsFor,
} from "../agent/stream.js";
import { startAttributes } from "../agent/telemetry.js";
import { DEFAULT_AGENT_WORKDIR } from "../limits.js";

export interface AgentRunOptions {
  readonly spec: AgentSpec;
  readonly runtime?: string | AgentRuntime | undefined;
  readonly sessionId?: string | undefined;
  readonly model?: string | undefined;
  readonly limits?: AgentLimits | undefined;
  readonly workdir?: string | undefined;
  readonly reasoning?: boolean | undefined;
  readonly signal?: AbortSignal | undefined;
}

export interface AgentPrepareOptions {
  readonly runtime?: string | AgentRuntime | undefined;
}

/**
 * El agente de IA de este sandbox (`ai-agent-core`, ADR-025). Tocar este
 * campo no manda ningún RPC; el coste empieza con el primer `run()`/
 * `stream()`, que llama al modelo a través de una pasarela ya creada.
 *
 * Coste y activación
 * -------------------
 * Activa: llamar a `run()`, `stream()` o `prepare()`. No hace falta ningún
 *     flag nuevo: basta con usar `sbx.agent`.
 * Recursos y llamadas AWS: ninguno nuevo de Rayito. El propio runtime llama
 *     al modelo (Bedrock `Converse`/`ConverseStream`, o la API que exponga
 *     la pasarela) a través de la pasarela ya configurada con `gateways` en
 *     `create()`; sin ella, `run()`/`stream()` fallan con
 *     `InvalidArgumentError` antes de cualquier llamada. El propio sandbox
 *     ya factura por segundo, con o sin agente.
 * Coste aproximado: depende del modelo y de los pasos, no de Rayito. Guía
 *     con precios de lista (us-east-1, consultados 2026-10-06,
 *     https://aws.amazon.com/bedrock/pricing y
 *     https://aws.amazon.com/lambda/pricing/): 10 pasos de Haiku 4.5
 *     regional (15k tokens de entrada + 400 de salida por paso) ≈ $0,187
 *     sin caché / $0,083 con caché de prompts; 5 minutos de MicroVM de 2 GB
 *     ≈ $0,0119. El modelo cuesta entre 7× y 21× la VM
 *     (`docs/site/docs/cost.md`, "Coste de un agente").
 * IAM: ninguno adicional a lo que ya pide la pasarela (`SecretGateway`,
 *     `secretsmanager:GetSecretValue`).
 * Cómo apagarla: no llames a `sbx.agent.run()`/`stream()`/`prepare()`.
 * Ejemplo:
 * ```ts
 * import { Sandbox, AgentSpec, AgentModel, bedrockGateway } from "rayito";
 *
 * const modelId = "us.anthropic.claude-haiku-4-5-20251001-v1:0";
 * await using sbx = await Sandbox.create({
 *   template: "rayito-agent",
 *   allowInternetAccess: false,
 *   gateways: { bedrock: bedrockGateway("bedrock-key", { region: "us-east-1", models: [modelId] }) },
 * });
 * const result = await sbx.agent.run("lista los ficheros de /home/user", {
 *   spec: new AgentSpec({ model: new AgentModel({ provider: "bedrock", id: modelId, gateway: "bedrock", region: "us-east-1" }) }),
 * });
 * ```
 */
export class Agent {
  readonly #sandbox: AgentSandbox;
  readonly #configCache = new ConfigCache();

  constructor(sandbox: AgentSandbox) {
    this.#sandbox = sandbox;
  }

  async run(prompt: string, options: AgentRunOptions): Promise<AgentResult> {
    const stream = await this.stream(prompt, options);
    try {
      return await stream.result();
    } finally {
      await stream.close();
    }
  }

  async stream(prompt: string, options: AgentRunOptions): Promise<AgentStream> {
    const limits = options.limits ?? new AgentLimits();
    const runtime = resolveRuntime(options.runtime ?? DEFAULT_AGENT_RUNTIME);
    const workdir = options.workdir ?? DEFAULT_AGENT_WORKDIR;
    const gatewayUrls = gatewayUrlsFor(options.spec, this.#sandbox.gateways());
    const config = runtime.buildConfig(options.spec, { gatewayUrls, workdir });
    if (this.#configCache.needsWrite(runtime.name, config)) {
      await this.#sandbox.files.writeFiles(
        config.files.map((file) => ({ path: file.path, data: file.data, mode: file.mode })),
      );
      this.#configCache.markApplied(runtime.name, config);
    }
    const request = buildRunRequest({
      spec: options.spec,
      prompt,
      workdir,
      sessionId: options.sessionId,
      model: options.model,
      reasoning: options.reasoning ?? false,
    });
    const runCommand = runtime.command(request);
    const instrumentation = this.#sandbox.instrumentation();
    let capturedSpan: Span | undefined;
    let settleSpan: ((error?: unknown) => void) | undefined;
    const spanDone = new Promise<void>((resolve, reject) => {
      settleSpan = (error?: unknown) => {
        if (error === undefined) {
          resolve();
        } else {
          reject(error);
        }
      };
    });
    const spanPromise = instrumentation.span(
      "rayito.agent.run",
      startAttributes(options.spec, runtime.name),
      (span) => {
        capturedSpan = span;
        return spanDone;
      },
    );
    spanPromise.catch(() => {
      // El rechazo (si lo hay) ya se propaga por `result()`/`run()`, que
      // esperan `spanDone` indirectamente a través de `AgentStream`; esto
      // sólo evita un `unhandledRejection` del lado de la promesa interna.
    });
    const finishSpan = (error?: unknown): void => settleSpan?.(error);
    let handle: AgentCommandHandle;
    try {
      handle = (await this.#sandbox.commands.run(runCommand.script, {
        background: true,
        envs: runCommand.envs,
        stdin: true,
        timeoutMs: limits.timeoutMs,
        maxOutputBytes: limits.maxOutputBytes,
        tag: AGENT_RUN_TAG,
        signal: options.signal,
      })) as AgentCommandHandle;
      await handle.sendStdin(runCommand.stdin);
      await handle.closeStdin();
    } catch (error) {
      finishSpan(error);
      throw error;
    }
    const stream = new AgentStream({
      sandbox: this.#sandbox,
      handle,
      runtime,
      state: runtime.newState(),
      limits,
      sessionId: options.sessionId,
      span: capturedSpan,
      finishSpan,
    });
    options.signal?.addEventListener(
      "abort",
      () => {
        void stream.abort();
      },
      { once: true },
    );
    return stream;
  }

  async prepare(options: AgentPrepareOptions = {}): Promise<void> {
    const runtime = resolveRuntime(options.runtime ?? DEFAULT_AGENT_RUNTIME);
    for (const step of runtime.warmupSteps()) {
      const handle = (await this.#sandbox.commands.run(step.cmd, {
        background: true,
        timeoutMs: step.background === true ? 0 : step.timeoutMs,
        tag: step.tag,
      })) as AgentCommandHandle;
      handle.disconnect();
    }
  }
}

export type { AgentSandbox };
export { AgentStream };
