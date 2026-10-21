/**
 * Lo que las banderas de `rayito agent deploy` cambian de un `AgentProject`
 * ya detectado (ADR-029). Espejo de `apply_serve_flags`,
 * `apply_project_flags` y `scaling_of` de `rayito.cli._agent_deploy`. Sólo se
 * importa al desplegar: cargar la CLI no carga el SDK del agente.
 */

import { fleetSettingsOf } from "../agent/fleet-base.js";
import { type AgentProject, ServeOptions, type ServeOptionsInput } from "../agent/project.js";
import { copyProject } from "../agent/project-copy.js";
import { ScalingOptions, type SessionMode } from "../agent/scaling-domain.js";
import {
  parseSecretEnvFlags,
  parseSecretEnvFromFlags,
  withSecretEnv,
} from "../agent/secret-env-project.js";
import { validateIdlePolicy } from "../models.js";
import type { AgentDeployFlags } from "./ops-ports.js";

/** Lo que las banderas fijan en `ServeOptions`: sólo lo que se pasó (un valor
 * `undefined` deja el del fichero o el de la detección). */
function serveChanges(flags: AgentDeployFlags): Partial<ServeOptionsInput> {
  const idle =
    flags.idleSeconds === undefined
      ? undefined
      : validateIdlePolicy({ maxIdleSeconds: flags.idleSeconds });
  const changes: Partial<ServeOptionsInput> = {
    maxConcurrency: flags.maxSessions,
    idle,
    queueMax: flags.queue,
    queueTimeoutMs: flags.queueTimeoutMs,
    timeoutMs: flags.timeoutMs,
    port: flags.port,
  };
  return Object.fromEntries(Object.entries(changes).filter(([, value]) => value !== undefined));
}

/** El proyecto con lo que las banderas fijan en `ServeOptions`. */
export function applyServeFlags(project: AgentProject, flags: AgentDeployFlags): AgentProject {
  const changes = serveChanges(flags);
  if (Object.keys(changes).length === 0) {
    return project;
  }
  return copyProject(project, { serve: new ServeOptions({ ...project.serve, ...changes }) });
}

/** El proyecto con la memoria, el entorno y las salidas de red de las
 * banderas: `--env` se suma al `env` del proyecto y `--allow-out` fija la
 * política de salida. `--secret-env` y `--secret-env-from` se suman a los del
 * proyecto. */
export function applyProjectFlags(project: AgentProject, flags: AgentDeployFlags): AgentProject {
  let result = project;
  if (flags.memory !== undefined) {
    result = copyProject(result, { memoryMib: flags.memory });
  }
  if (Object.keys(flags.env).length > 0) {
    result = copyProject(result, { env: { ...result.env, ...flags.env } });
  }
  if (flags.allowOut.length > 0) {
    result = copyProject(result, {
      network: { allowOut: [...flags.allowOut], denyOut: result.network?.denyOut ?? [] },
    });
  }
  return withSecretEnv(
    result,
    parseSecretEnvFlags(flags.secretEnv ?? []),
    parseSecretEnvFromFlags(flags.secretEnvFrom ?? []),
  );
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
    scaleInAfterMs: current.scaleInAfterMs,
  });
}
