/**
 * Puerto `AgentRuntime` (`ai-agent-core`, ADR-025, design.md §3). Espejo de
 * `rayito._agent._runtime`: lo que un adaptador de runtime (OpenCode,
 * deepagents) traduce de un `AgentSpec` a ficheros de configuración, a un
 * comando y de vuelta a eventos. Puro: sin `grpc`/Connect, `@aws-sdk/*` ni
 * reloj.
 *
 * Un adaptador concreto (OpenCode) llega con el resto de `ai-agent-core`;
 * mientras tanto, un llamante puede pasar su propio objeto que cumpla
 * `AgentRuntime` como `runtime`, y `runtimes.ts` no necesita tener ninguno
 * registrado por nombre para que `sbx.agent.run()` funcione.
 */

import type { AgentSpec } from "./domain.js";
import type { AgentEvent, AgentFailed, Done } from "./events.js";

/** Un fichero de configuración del runtime. */
export interface RuntimeFile {
  readonly path: string;
  readonly data: Uint8Array;
  readonly mode?: number | undefined;
}

/** Lo que `buildConfig()` devuelve: los ficheros a escribir y un sha256 de
 * su contenido conjunto, para que el servicio de aplicación se salte
 * `filesWrite` cuando ya está aplicado (mismo sha que la última vez). */
export interface RuntimeFiles {
  readonly files: readonly RuntimeFile[];
  readonly configSha256: string;
}

export interface RunRequest {
  readonly spec: AgentSpec;
  readonly prompt: string;
  readonly workdir: string;
  readonly sessionId?: string | undefined;
  readonly model?: string | undefined;
  readonly reasoning?: boolean | undefined;
  readonly attach?: boolean | "auto" | undefined;
}

/** El comando que ejecuta la petición: texto de script de bash (nunca un
 * secreto), sus variables de entorno y el stdin a mandarle (el prompt). */
export interface RunCommand {
  readonly script: string;
  readonly envs?: Readonly<Record<string, string>> | undefined;
  readonly stdin: Uint8Array;
}

/** Un paso DSL que `AgentTemplate` añade a la plantilla. */
export interface TemplateStep {
  readonly op: string;
  readonly args?: Readonly<Record<string, unknown>> | undefined;
}

/** Un paso de calentamiento: `agent.prepare()` y el `warmup` de un pool
 * corren la misma lista. `background: true` no espera a que termine (un
 * servidor residente); si no, se espera su salida con `timeoutMs`. */
export interface WarmupStep {
  readonly cmd: string;
  readonly background?: boolean | undefined;
  readonly timeoutMs?: number | undefined;
  readonly tag?: string | undefined;
}

/** Estado mutable que un adaptador acumula entre llamadas a `parseLine()`;
 * opaco para el servicio de aplicación. */
// biome-ignore lint/suspicious/noEmptyInterface: marcador opaco, como RuntimeState en Python.
export interface RuntimeState {}

/** Lo que un adaptador de runtime implementa. `name` identifica el
 * adaptador en los eventos de telemetría (`gen_ai.agent.name`). */
export interface AgentRuntime {
  readonly name: string;
  buildConfig(
    spec: AgentSpec,
    options: { readonly gatewayUrls: Readonly<Record<string, string>>; readonly workdir: string },
  ): RuntimeFiles;
  command(request: RunRequest): RunCommand;
  newState(): RuntimeState;
  parseLine(line: Uint8Array, state: RuntimeState): readonly AgentEvent[];
  finish(state: RuntimeState, exitCode: number): Done | AgentFailed;
  abortCommand(state: RuntimeState): string | undefined;
  templateSteps(): readonly TemplateStep[];
  warmupSteps(options: { readonly serve: boolean }): readonly WarmupStep[];
}
