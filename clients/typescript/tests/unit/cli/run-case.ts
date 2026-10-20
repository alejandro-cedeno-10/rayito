/** Ejecuta `runCli` con servicios falsos y la terminal falsa: lo común a los tests de comandos. */

import type { TerminalInput } from "../../../src/cli/interactive-terminal.js";
import type { AgentCoreTools, AgentProjectTools } from "../../../src/cli/ops-ports.js";
import { runCli } from "../../../src/cli/run.js";
import type { CliServices } from "../../../src/cli/services.js";
import { FakeTerminal } from "./terminal.js";

export const FIXED_NOW = new Date("2026-09-15T16:02:07Z");

export interface CliOutcome {
  readonly exitCode: number;
  readonly stdout: string;
  readonly stderr: string;
}

export interface CliRun {
  readonly args: readonly string[];
  readonly services: CliServices;
  readonly stdin?: string | undefined;
  /** La entrada cruda de `connect` y `create`; sin ella no hay sesión interactiva. */
  readonly rawInput?: TerminalInput | undefined;
  readonly now?: Date | undefined;
  readonly env?: Readonly<Record<string, string | undefined>> | undefined;
  readonly signal?: AbortSignal | undefined;
  readonly agentProjects?: AgentProjectTools | undefined;
  readonly agentcore?: AgentCoreTools | undefined;
  readonly sleep?: ((ms: number, signal: AbortSignal) => Promise<void>) | undefined;
}

export async function runWithServices(run: CliRun): Promise<CliOutcome> {
  const terminal = new FakeTerminal(run.stdin ?? "");
  terminal.stdin = run.rawInput;
  const exitCode = await runCli(run.args, {
    terminal,
    env: run.env ?? {},
    now: () => run.now ?? FIXED_NOW,
    sleep: run.sleep ?? (async () => undefined),
    signal: run.signal ?? new AbortController().signal,
    createServices: async () => run.services,
    agentProjects: run.agentProjects,
    agentcore: run.agentcore,
  });
  return { exitCode, stdout: terminal.stdoutText, stderr: terminal.stderrText };
}
