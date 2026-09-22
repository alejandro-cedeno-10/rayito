/**
 * Guardrails del e2e (MILESTONES.md, M1 "para siempre"), como en
 * `clients/python/tests/e2e/conftest.py`: nada corre sin `RAYITO_E2E=1` y
 * `RAYITO_TEMPLATE`; todo sandbox nace con `timeoutMs <= 1 800 000`; un
 * pre-flight falla con más de 10 MicroVMs vivos de la imagen; `afterAll`
 * termina todo lo creado (idempotente) e imprime `run-microvm -> Health` en
 * segundos por sandbox. `RAYITO_EXECUTION_ROLE_ARN` activa `logging: "cloudwatch"`.
 */

import { afterAll, beforeAll } from "vitest";
import {
  type IdlePolicyInput,
  LambdaMicrovmsControlPlane,
  type LoggingOption,
  type OutputChunk,
  Sandbox,
  SandboxNotFoundError,
} from "../../src/index.js";

export const E2E_FLAG_VAR = "RAYITO_E2E";
export const TEMPLATE_VAR = "RAYITO_TEMPLATE";
export const EXECUTION_ROLE_VAR = "RAYITO_EXECUTION_ROLE_ARN";
export const TEST_SANDBOX_TIMEOUT_MS = 900_000;
export const MAX_TEST_SANDBOX_TIMEOUT_MS = 1_800_000;
export const MAX_LIVE_TEST_SANDBOXES = 10;
export const POLL_MS = 200;

export function e2eEnabled(): boolean {
  return process.env[E2E_FLAG_VAR] === "1" && Boolean(process.env[TEMPLATE_VAR]);
}

export interface E2ESettings {
  readonly template: string;
  readonly region: string | undefined;
  readonly executionRoleArn: string | undefined;
  readonly logging: LoggingOption;
}

export function e2eSettings(templateVar: string = TEMPLATE_VAR): E2ESettings {
  const template = process.env[templateVar];
  if (!e2eEnabled() || !template) {
    throw new Error(`los tests e2e requieren ${E2E_FLAG_VAR}=1 y ${templateVar}=<arn|nombre>`);
  }
  const executionRoleArn = process.env[EXECUTION_ROLE_VAR] || undefined;
  return {
    template,
    region: process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || undefined,
    executionRoleArn,
    logging: executionRoleArn ? "cloudwatch" : "disabled",
  };
}

export function report(label: string, seconds: number): void {
  console.log(`\n[m6] ${label}: ${seconds.toFixed(2)} s`);
}

export function seconds(sinceMs: number): number {
  return (performance.now() - sinceMs) / 1000;
}

export async function timed<T>(label: string, action: () => Promise<T>): Promise<[T, number]> {
  const started = performance.now();
  const value = await action();
  const elapsed = seconds(started);
  report(label, elapsed);
  return [value, elapsed];
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function waitUntil(
  predicate: () => boolean | Promise<boolean>,
  budgetMs: number,
  what: string,
  pollMs = POLL_MS,
): Promise<number> {
  const started = performance.now();
  while (!(await predicate())) {
    if (performance.now() - started >= budgetMs) {
      throw new Error(`${what} no ocurrió en ${budgetMs / 1000} s`);
    }
    await sleep(pollMs);
  }
  return seconds(started);
}

export function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} no llegó en ${ms / 1000} s`)), ms);
  });
  return Promise.race([promise, deadline]).finally(() => {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
  });
}

/**
 * Una línea de salida del shell, nunca el eco de la entrada: bash 5.2 envuelve
 * cada comando con el bracketed paste de readline, así que entre el eco
 * `echo hola\r\n` y la salida va `\x1b[?2004l\r` (Q37); sin bracketed paste la
 * salida sigue a `\n`.
 */
export function outputLine(text: string): RegExp {
  return new RegExp(`[\\r\\n]${text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\r\\n`);
}

/** Itera un `PtyHandle` con un plazo por chunk hasta que `needle` aparece en el texto acumulado. */
export async function readUntil(
  iterator: AsyncIterator<OutputChunk>,
  needle: string | RegExp,
  timeoutMs: number,
): Promise<string> {
  let buffer = "";
  const decoder = new TextDecoder();
  const deadline = performance.now() + timeoutMs;
  const matches = () =>
    typeof needle === "string" ? buffer.includes(needle) : needle.test(buffer);
  while (!matches()) {
    const remaining = deadline - performance.now();
    if (remaining <= 0) {
      throw new Error(
        `${String(needle)} no llegó en ${timeoutMs / 1000} s: ${JSON.stringify(buffer)}`,
      );
    }
    const result = await withTimeout(iterator.next(), remaining, String(needle));
    if (result.done) {
      throw new Error(`la PTY terminó sin ${String(needle)}: ${JSON.stringify(buffer)}`);
    }
    const chunk = result.value;
    buffer +=
      chunk.pty !== undefined
        ? decoder.decode(chunk.pty, { stream: true })
        : (chunk.stdout ?? chunk.stderr ?? "");
  }
  return buffer;
}

/** Consume un handle en segundo plano acumulando la salida y el error final. */
export class Collector {
  readonly chunks: string[] = [];
  error: Error | undefined;
  done = false;

  constructor(handle: AsyncIterable<OutputChunk>) {
    void this.#run(handle);
  }

  async #run(handle: AsyncIterable<OutputChunk>): Promise<void> {
    const decoder = new TextDecoder();
    try {
      for await (const chunk of handle) {
        this.chunks.push(
          chunk.pty !== undefined
            ? decoder.decode(chunk.pty, { stream: true })
            : (chunk.stdout ?? chunk.stderr ?? ""),
        );
      }
    } catch (error) {
      this.error = error as Error;
    } finally {
      this.done = true;
    }
  }
}

export interface E2EContext {
  readonly settings: E2ESettings;
  readonly controlPlane: LambdaMicrovmsControlPlane;
  readonly templateArn: string;
  readonly created: Sandbox[];
  readonly bootTimings: Map<string, number>;
}

async function liveSandboxIds(
  controlPlane: LambdaMicrovmsControlPlane,
  templateArn: string,
): Promise<string[]> {
  const ids: string[] = [];
  for await (const item of controlPlane.listMicrovms({ imageArn: templateArn })) {
    ids.push(item.sandboxId);
  }
  return ids;
}

/**
 * Pre-flight en `beforeAll` y sweeper en `afterAll`; devuelve el contexto
 * compartido de la suite. `templateVar` permite a una suite usar otra imagen
 * (`RAYITO_TEMPLATE_POLY` para los kernels de M7).
 */
export function useE2E(templateVar: string = TEMPLATE_VAR): E2EContext {
  const context: { current: E2EContext | undefined } = { current: undefined };
  const created: Sandbox[] = [];
  const bootTimings = new Map<string, number>();

  beforeAll(async () => {
    const settings = e2eSettings(templateVar);
    const controlPlane = LambdaMicrovmsControlPlane.fromRegion(settings.region);
    const templateArn = await controlPlane.resolveTemplateArn(settings.template);
    const live = await liveSandboxIds(controlPlane, templateArn);
    if (live.length > MAX_LIVE_TEST_SANDBOXES) {
      throw new Error(
        `pre-flight: hay ${live.length} MicroVMs vivos de ${templateArn}; limpia antes de correr`,
      );
    }
    context.current = { settings, controlPlane, templateArn, created, bootTimings };
  });

  afterAll(async () => {
    const current = context.current;
    if (current === undefined) {
      return;
    }
    for (const sandbox of created) {
      sandbox.close();
      try {
        await current.controlPlane.terminateMicrovm(sandbox.sandboxId);
      } catch (error) {
        if (!(error instanceof SandboxNotFoundError)) {
          throw error;
        }
      }
    }
    for (const [sandboxId, elapsed] of bootTimings) {
      report(
        `${sandboxId}: run-microvm -> Health agent_ready y kernel_ready (kernel_ready_s)`,
        elapsed,
      );
    }
  });

  return new Proxy({} as E2EContext, {
    get(_target, property) {
      const current = context.current;
      if (current === undefined) {
        throw new Error("el contexto e2e aún no está inicializado (beforeAll)");
      }
      return current[property as keyof E2EContext];
    },
  });
}

/** `create()` con los parámetros de todo sandbox de test; el tiempo medido es `run-microvm -> kernel_ready`. */
export async function createTestSandbox(
  context: E2EContext,
  options: { readonly timeoutMs?: number; readonly idle?: IdlePolicyInput | null } = {},
): Promise<Sandbox> {
  const timeoutMs = options.timeoutMs ?? TEST_SANDBOX_TIMEOUT_MS;
  if (timeoutMs > MAX_TEST_SANDBOX_TIMEOUT_MS) {
    throw new Error("guardrail: timeoutMs <= 1 800 000");
  }
  const started = performance.now();
  const sandbox = await Sandbox.create({
    template: context.templateArn,
    timeoutMs,
    idle: options.idle ?? null,
    executionRoleArn: context.settings.executionRoleArn,
    ingress: ["ALL_INGRESS"],
    logging: context.settings.logging,
    controlPlane: context.controlPlane,
  });
  const elapsed = seconds(started);
  context.created.push(sandbox);
  context.bootTimings.set(sandbox.sandboxId, elapsed);
  report(
    `${sandbox.sandboxId}: run-microvm -> Health agent_ready y kernel_ready (kernel_ready_s)`,
    elapsed,
  );
  return sandbox;
}
