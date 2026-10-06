/**
 * Lógica de aplicación de `sbx.agent.run/stream/prepare` (`ai-agent-core`,
 * design.md §4). Espejo de `rayito._agent._stream_base` +
 * `sandbox_sync/agent.py`: construir la petición, decidir si hace falta
 * `filesWrite`, trocear stdout en líneas, imponer `AgentLimits` sobre los
 * eventos del runtime, abrir el span `rayito.agent.run` y montar el
 * `AgentResult` final.
 */

import type { Span } from "@opentelemetry/api";
import { InvalidArgumentError, SandboxError, TimeoutError } from "../errors.js";
import { MAX_AGENT_EVENT_LINE_BYTES } from "../limits.js";
import type { Instrumentation } from "../otel.js";
import type { AgentLimits, AgentSpec } from "./domain.js";
import {
  type AgentEvent,
  type AgentFailed,
  agentFailed,
  agentFailedToError,
  type Done,
  type StepFinished,
  type StepStarted,
  type Text,
  TokenUsage,
  type ToolCall,
} from "./events.js";
import type { AgentRuntime, RunRequest, RuntimeFiles, RuntimeState } from "./runtime.js";
import { doneAttributes, failureAttributes } from "./telemetry.js";

/** Tag que `commands.run({ tag })` lleva en cada ejecución del agente. */
export const AGENT_RUN_TAG = "rayito-agent-run";

/** Los `reason` con los que el SDK, no el runtime, corta una ejecución
 * (`LimitTracker`): el runtime sigue vivo y hay que pararlo. */
export const SDK_LIMIT_REASONS: ReadonlySet<string> = new Set(["max_steps", "token_budget"]);

/** Plazo de la orden de `stopTreeCommand`. */
export const AGENT_STOP_TREE_TIMEOUT_MS = 30_000;

/**
 * Congela (`SIGSTOP`) el proceso del runtime y, de padres a hijos, cada
 * descendiente suyo, y luego mata (`SIGKILL`) a los descendientes. Hace
 * falta porque OpenCode y deepagents lanzan cada orden de su herramienta de
 * shell en una sesión propia (`setsid`): el `SIGKILL` de `rayd` al grupo del
 * proceso no las alcanza y, sin esto, un `sleep` o un servidor lanzado por el
 * agente sobreviviría a `abort()` y a los límites del SDK (medido con
 * `make local-e2e`, `agents.local.test.ts`). El propio runtime queda
 * congelado para el `kill()` del handle. Corre como el mismo `user` que el
 * agente, así que no puede tocar procesos ajenos.
 */
export function stopTreeCommand(pid: number): string {
  if (!Number.isSafeInteger(pid) || pid <= 1) {
    throw new InvalidArgumentError("pid de agente no válido");
  }
  return (
    't() { local c; for c in $(pgrep -P "$1"); do ' +
    'kill -STOP "$c" 2>/dev/null; t "$c"; kill -KILL "$c" 2>/dev/null; done; }; ' +
    `kill -STOP ${pid} 2>/dev/null; t ${pid}; true`
  );
}

/** Si `event` es el `AgentFailed` con el que el SDK corta una ejecución que
 * el runtime aún no terminó. */
export function isSdkLimitFailure(event: AgentEvent): boolean {
  return event.type === "agent_failed" && SDK_LIMIT_REASONS.has(event.reason);
}

/** Lo que `Agent`/`AsyncAgent` necesitan de un `Sandbox`: cualquier objeto
 * con estos cuatro atributos sirve (lo que le dan los tests con un doble). */
export interface AgentSandbox {
  readonly commands: {
    run(cmd: string, options?: Record<string, unknown>): Promise<unknown>;
  };
  readonly files: {
    writeFiles(
      files: readonly { path: string; data: Uint8Array; mode?: number | undefined }[],
    ): Promise<unknown>;
  };
  gateways(): {
    get(name: string): { readonly url: string } | undefined;
    entries(): readonly (readonly [string, unknown])[];
  };
  instrumentation(): Instrumentation;
}

/** Una llamada mínima de `CommandHandle`: lo que `AgentStream` necesita
 * (sync e irrelevante aquí si no es background; de verdad es
 * `CommandHandle` de `sandbox/commands.js`, tipado ancho para no acoplar
 * este módulo a su import). */
export interface AgentCommandHandle extends AsyncIterable<{ readonly stdout?: string }> {
  readonly pid: number;
  readonly exitCode: number | undefined;
  kill(): Promise<boolean>;
  wait(): Promise<unknown>;
  disconnect(): void;
  sendStdin(data: Uint8Array): Promise<void>;
  closeStdin(): Promise<void>;
}

/** Las URLs de las pasarelas que `spec` necesita (`spec.gatewayNames()`),
 * leídas de `sbx.gateways`. Falla con `InvalidArgumentError` **antes de
 * cualquier RPC** si falta alguna (`AgentSpec.requireGateways`). */
export function gatewayUrlsFor(
  spec: AgentSpec,
  gateways: ReturnType<AgentSandbox["gateways"]>,
): Readonly<Record<string, string>> {
  const names = [...spec.gatewayNames()];
  const available = gateways.entries().map(([name]) => name);
  spec.requireGateways(available);
  const urls: Record<string, string> = {};
  for (const name of names) {
    const status = gateways.get(name);
    if (status !== undefined) {
      urls[name] = status.url;
    }
  }
  return urls;
}

/** Guarda el sha256 de la configuración del runtime ya escrita en el
 * sandbox, por runtime (`AgentRuntime.name`). */
export class ConfigCache {
  readonly #applied = new Map<string, string>();

  needsWrite(runtimeName: string, config: RuntimeFiles): boolean {
    return this.#applied.get(runtimeName) !== config.configSha256;
  }

  markApplied(runtimeName: string, config: RuntimeFiles): void {
    this.#applied.set(runtimeName, config.configSha256);
  }
}

/** Trocea el stdout del comando del agente en líneas completas; una línea
 * mayor que `MAX_AGENT_EVENT_LINE_BYTES` se descarta entera y cuenta en
 * `dropped`. */
export class LineBuffer {
  #buffer = "";
  dropped = 0;
  readonly #encoder = new TextEncoder();

  feed(text: string): Uint8Array[] {
    if (!text) {
      return [];
    }
    this.#buffer += text;
    const lines: Uint8Array[] = [];
    let newlineAt = this.#buffer.indexOf("\n");
    while (newlineAt >= 0) {
      const line = this.#buffer.slice(0, newlineAt);
      this.#buffer = this.#buffer.slice(newlineAt + 1);
      const encoded = this.#encoder.encode(line);
      if (encoded.length > MAX_AGENT_EVENT_LINE_BYTES) {
        this.dropped += 1;
      } else {
        lines.push(encoded);
      }
      newlineAt = this.#buffer.indexOf("\n");
    }
    return lines;
  }
}

/** Impone `AgentLimits` sobre la secuencia de eventos que un adaptador
 * produce, y acumula lo que necesita el `AgentResult` final. */
export class LimitTracker {
  readonly #limits: AgentLimits;
  sessionId: string | undefined;
  steps = 0;
  usage = new TokenUsage();
  lastText = "";
  readonly toolCalls: ToolCall[] = [];

  constructor(limits: AgentLimits, sessionId: string | undefined) {
    this.#limits = limits;
    this.sessionId = sessionId;
  }

  track(event: AgentEvent): AgentEvent {
    switch (event.type) {
      case "step_started": {
        const started = event as StepStarted;
        this.steps = started.index;
        if (this.steps > this.#limits.maxSteps) {
          return this.#failed("max_steps");
        }
        return event;
      }
      case "step_finished": {
        const finished = event as StepFinished;
        this.usage = this.usage.plus(finished.usage);
        if (
          this.#limits.maxTotalTokens !== null &&
          this.usage.total > this.#limits.maxTotalTokens
        ) {
          return this.#failed("token_budget");
        }
        return event;
      }
      case "text":
        this.lastText = (event as Text).text;
        return event;
      case "tool_call":
        this.toolCalls.push(event as ToolCall);
        return event;
      case "done":
        this.sessionId = (event as Done).sessionId;
        return event;
      case "agent_failed": {
        const failed = event as AgentFailed;
        if (failed.sessionId === undefined && this.sessionId !== undefined) {
          return agentFailed(failed.reason, {
            exitCode: failed.exitCode,
            detailCode: failed.detailCode,
            sessionId: this.sessionId,
          });
        }
        return event;
      }
      default:
        return event;
    }
  }

  #failed(reason: string): AgentFailed {
    return agentFailed(reason, { sessionId: this.sessionId });
  }
}

export function requirePrompt(prompt: unknown): string {
  if (typeof prompt !== "string" || prompt === "") {
    throw new InvalidArgumentError(
      "el prompt de sbx.agent.run()/stream() debe ser una cadena no vacía",
    );
  }
  return prompt;
}

export function buildRunRequest(options: {
  readonly spec: AgentSpec;
  readonly prompt: string;
  readonly workdir: string;
  readonly sessionId: string | undefined;
  readonly model: string | undefined;
  readonly reasoning: boolean;
  readonly attach: boolean | "auto";
}): RunRequest {
  return {
    spec: options.spec,
    prompt: requirePrompt(options.prompt),
    workdir: options.workdir,
    sessionId: options.sessionId,
    model: options.model,
    reasoning: options.reasoning,
    attach: options.attach,
  };
}

export interface AgentStreamInit {
  readonly sandbox: AgentSandbox;
  readonly handle: AgentCommandHandle;
  readonly runtime: AgentRuntime;
  readonly state: RuntimeState;
  readonly limits: AgentLimits;
  readonly sessionId: string | undefined;
  readonly span: Span | undefined;
  readonly finishSpan: (error?: unknown) => void;
  readonly attached: boolean;
}

/**
 * Iterable asíncrono de `AgentEvent` sobre un `CommandHandle` en
 * background: cada chunk de stdout se trocea en líneas, cada línea se
 * traduce con el adaptador del runtime y cada evento pasa por
 * `LimitTracker` antes de entregarse. Nunca lanza por un fallo del agente;
 * `result()` sí.
 */
export class AgentStream implements AsyncIterable<AgentEvent> {
  readonly #sandbox: AgentSandbox;
  readonly #handle: AgentCommandHandle;
  readonly #handleIterator: AsyncIterator<{ readonly stdout?: string }>;
  readonly #runtime: AgentRuntime;
  readonly #state: RuntimeState;
  readonly #limits: AgentLimits;
  readonly #tracker: LimitTracker;
  readonly #buffer = new LineBuffer();
  readonly #queue: AgentEvent[] = [];
  #final: Done | AgentFailed | undefined;
  #closed = false;
  #abortRequested = false;
  #stopped = false;
  readonly #span: Span | undefined;
  readonly #finishSpan: (error?: unknown) => void;
  readonly #attached: boolean;

  constructor(init: AgentStreamInit) {
    this.#sandbox = init.sandbox;
    this.#handle = init.handle;
    this.#handleIterator = init.handle[Symbol.asyncIterator]();
    this.#runtime = init.runtime;
    this.#state = init.state;
    this.#limits = init.limits;
    this.#tracker = new LimitTracker(init.limits, init.sessionId);
    this.#span = init.span;
    this.#finishSpan = init.finishSpan;
    this.#attached = init.attached;
  }

  get sessionId(): string | undefined {
    return this.#tracker.sessionId;
  }

  get droppedLines(): number {
    return this.#buffer.dropped;
  }

  [Symbol.asyncIterator](): AsyncIterator<AgentEvent> {
    return this.#iterate();
  }

  async *#iterate(): AsyncGenerator<AgentEvent, void, undefined> {
    while (true) {
      while (this.#queue.length === 0) {
        if (this.#final !== undefined) {
          return;
        }
        await this.#pump();
      }
      const event = this.#queue.shift();
      if (event !== undefined) {
        yield event;
      }
    }
  }

  /** Le da al adaptador la oportunidad de pedirle al runtime que pare con
   * elegancia (`abortCommand`, por ejemplo `POST /session/<id>/abort`) y
   * luego mata el proceso. */
  async abort(): Promise<void> {
    if (this.#abortRequested) {
      return;
    }
    this.#abortRequested = true;
    await this.#stop();
  }

  /** Para el runtime y todo lo que lanzó: el `abortCommand` con elegancia
   * del adaptador, `stopTreeCommand` y el `kill()` del handle. Sirve a
   * `abort()` y a un límite del SDK (`max_steps`, `token_budget`), que sin
   * esto dejaría al runtime trabajando (y gastando tokens) en segundo plano. */
  async #stop(): Promise<void> {
    if (this.#stopped) {
      return;
    }
    this.#stopped = true;
    const command = this.#runtime.abortCommand(this.#state);
    if (command !== undefined) {
      try {
        await this.#sandbox.commands.run(command, { timeoutMs: this.#limits.timeoutMs });
      } catch (error) {
        if (!(error instanceof SandboxError)) {
          throw error;
        }
      }
    }
    try {
      await this.#sandbox.commands.run(stopTreeCommand(this.#handle.pid), {
        timeoutMs: AGENT_STOP_TREE_TIMEOUT_MS,
      });
    } catch (error) {
      if (!(error instanceof SandboxError)) {
        throw error;
      }
    }
    await this.#handle.kill();
  }

  /** Consume el resto del stream y devuelve su `AgentResult`. `AgentFailed`
   * se convierte en `AgentError`. */
  async result(): Promise<import("./events.js").AgentResult> {
    for await (const _event of this) {
      // Drena el stream: el resultado vive en `this.#final`.
    }
    const final = this.#final;
    if (final === undefined) {
      throw new SandboxError("el stream del agente terminó sin un evento final");
    }
    if (final.type === "agent_failed") {
      this.#finishSpan();
      for (const [key, value] of Object.entries(failureAttributes(final))) {
        this.#span?.setAttribute(key, value);
      }
      throw agentFailedToError(final, this.#tracker.usage);
    }
    this.#finishSpan();
    for (const [key, value] of Object.entries(
      doneAttributes(final, { steps: this.#tracker.steps, attached: this.#attached }),
    )) {
      this.#span?.setAttribute(key, value);
    }
    return {
      sessionId: final.sessionId,
      text: this.#tracker.lastText,
      steps: this.#tracker.steps,
      usage: this.#tracker.usage,
      exitCode: final.exitCode,
      toolCalls: [...this.#tracker.toolCalls],
      droppedLines: this.#buffer.dropped,
    };
  }

  async close(): Promise<void> {
    if (this.#closed) {
      return;
    }
    this.#closed = true;
    this.#handle.disconnect();
    this.#finishSpan();
  }

  async #pump(): Promise<void> {
    let step: IteratorResult<{ readonly stdout?: string }>;
    try {
      step = await this.#handleIterator.next();
    } catch (error) {
      if (error instanceof TimeoutError) {
        this.#enqueueFinal(agentFailed("timeout", { sessionId: this.#tracker.sessionId }));
        return;
      }
      throw error;
    }
    if (step.done === true) {
      await this.#finish();
      return;
    }
    for (const line of this.#buffer.feed(step.value.stdout ?? "")) {
      for (const rawEvent of this.#runtime.parseLine(line, this.#state)) {
        const event = this.#tracker.track(rawEvent);
        this.#queue.push(event);
        if (event.type === "done" || event.type === "agent_failed") {
          this.#final = event;
          if (isSdkLimitFailure(event)) {
            await this.#stop();
            await this.#drain();
          }
          return;
        }
      }
    }
  }

  async #finish(): Promise<void> {
    if (this.#abortRequested) {
      this.#enqueueFinal(
        agentFailed("aborted", {
          exitCode: this.#handle.exitCode,
          sessionId: this.#tracker.sessionId,
        }),
      );
      return;
    }
    if (await this.#endedByTimeout()) {
      this.#enqueueFinal(agentFailed("timeout", { sessionId: this.#tracker.sessionId }));
      return;
    }
    const exitCode = this.#handle.exitCode ?? -1;
    const final = this.#runtime.finish(this.#state, exitCode);
    this.#enqueueFinal(this.#tracker.track(final) as Done | AgentFailed);
  }

  /** Si `rayd` terminó el proceso por `AgentLimits.timeoutMs`. Iterar el
   * handle no lo dice (el stream acaba con el `EndEvent` sin lanzar);
   * `wait()`, sobre el stream ya consumido, lo convierte en `TimeoutError`
   * sin otra llamada. */
  async #endedByTimeout(): Promise<boolean> {
    try {
      await this.#handle.wait();
    } catch (error) {
      if (error instanceof TimeoutError) {
        return true;
      }
      if (!(error instanceof SandboxError)) {
        throw error;
      }
    }
    return false;
  }

  /** Consume el handle hasta su `EndEvent` tras pararlo por un límite:
   * `rayd` sólo lo manda cuando el proceso ya no existe, así que al volver el
   * cerrojo de ejecución está libre y el siguiente `run()` no encuentra el
   * runtime `busy` (un proceso con hilos tarda en morir del todo tras el
   * `SIGKILL`). */
  async #drain(): Promise<void> {
    try {
      let step = await this.#handleIterator.next();
      while (step.done !== true) {
        step = await this.#handleIterator.next();
      }
    } catch (error) {
      if (!(error instanceof SandboxError)) {
        throw error;
      }
    }
  }

  #enqueueFinal(event: Done | AgentFailed): void {
    this.#queue.push(event);
    this.#final = event;
  }
}
