/**
 * Lo que las banderas de `rayito agent deploy` cambian de un `AgentProject`
 * ya detectado (ADR-029). Espejo de `apply_serve_flags`,
 * `apply_project_flags` y `scaling_of` de `rayito.cli._agent_deploy`. Sólo se
 * importa al desplegar: cargar la CLI no carga el SDK del agente.
 */

import { fleetSettingsOf } from "../agent/fleet-base.js";
import { type AgentProject, ServeOptions } from "../agent/project.js";
import { copyProject } from "../agent/project-copy.js";
import { ScalingOptions, type SessionMode } from "../agent/scaling-domain.js";
import { InvalidArgumentError } from "../errors.js";
import { validateIdlePolicy } from "../models.js";
import type { AgentDeployFlags } from "./ops-ports.js";

/** Una bandera que necesita un campo que el proyecto de esta versión del SDK
 * no tiene. */
export function notAvailableMessage(flag: string, field: string): string {
  return `${flag} necesita el campo ${field} del proyecto: esta versión del SDK no lo tiene`;
}

/** Los `ServeOptions` con `name = value`, o un error de uso que nombra la
 * bandera si el proyecto de esta versión no tiene ese campo. */
function serveWith(serve: ServeOptions, name: string, value: unknown, flag: string): ServeOptions {
  if (!(name in serve)) {
    throw new InvalidArgumentError(notAvailableMessage(flag, name));
  }
  return new ServeOptions({ ...serve, [name]: value });
}

/** El proyecto con lo que las banderas fijan en `ServeOptions`: sólo lo que se
 * pasó (un valor `undefined` deja el del fichero o el de la detección). */
export function applyServeFlags(project: AgentProject, flags: AgentDeployFlags): AgentProject {
  let serve = project.serve;
  const changes: readonly (readonly [string, unknown, string])[] = [
    ["maxConcurrency", flags.maxSessions, "--max-sessions"],
    [
      "idle",
      flags.idleSeconds === undefined
        ? undefined
        : validateIdlePolicy({ maxIdleSeconds: flags.idleSeconds }),
      "--idle",
    ],
    ["queueMax", flags.queue, "--queue"],
    ["queueTimeoutMs", flags.queueTimeoutMs, "--queue-timeout"],
    ["timeoutMs", flags.timeoutMs, "--timeout"],
    ["port", flags.port, "--port"],
  ];
  for (const [name, value, flag] of changes) {
    if (value !== undefined) {
      serve = serveWith(serve, name, value, flag);
    }
  }
  return serve === project.serve ? project : copyProject(project, { serve });
}

/** El proyecto con la memoria, el entorno y las salidas de red de las
 * banderas. `--env` (campo `env`) y `--allow-out` (campo `network`) son de las
 * opciones de proyecto de 0.14: sin ese campo en el proyecto, el error nombra
 * la bandera. */
export function applyProjectFlags(project: AgentProject, flags: AgentDeployFlags): AgentProject {
  let result = project;
  if (flags.memory !== undefined) {
    result = copyProject(result, { memoryMib: flags.memory });
  }
  if (Object.keys(flags.env).length > 0) {
    if (!("env" in result)) {
      throw new InvalidArgumentError(notAvailableMessage("--env", "env"));
    }
    const current = (result as unknown as { env: Readonly<Record<string, string>> }).env;
    result = copyProject(result, { env: { ...current, ...flags.env } } as never);
  }
  if (flags.allowOut.length > 0) {
    if (!("network" in result)) {
      throw new InvalidArgumentError(notAvailableMessage("--allow-out", "network"));
    }
    result = copyProject(result, { network: { allowOut: [...flags.allowOut] } } as never);
  }
  return result;
}

/** El `ScalingOptions` de las banderas sobre el del proyecto; `undefined` si no
 * se pasó ninguna (el proyecto decide). */
export function scalingOf(
  project: AgentProject,
  flags: AgentDeployFlags,
): ScalingOptions | undefined {
  if (
    flags.minVms === undefined &&
    flags.maxVms === undefined &&
    flags.warm === undefined &&
    flags.sessions === undefined
  ) {
    return undefined;
  }
  const current = fleetSettingsOf(project, undefined).scaling;
  return new ScalingOptions({
    minVms: flags.minVms ?? current.minVms,
    maxVms: flags.maxVms ?? current.maxVms,
    warmVms: flags.warm ?? current.warmVms,
    sessions: (flags.sessions ?? current.sessions) as SessionMode,
    scaleInAfterSeconds: current.scaleInAfterSeconds,
  });
}
