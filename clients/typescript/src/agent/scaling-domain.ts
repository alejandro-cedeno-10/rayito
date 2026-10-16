/**
 * `ScalingOptions`: cuántos MicroVMs sirven un agente y cómo se reparten las
 * sesiones entre ellos (`AgentFleet`, `deployAgent`). Espejo de
 * `rayito._agent._scaling_domain`. Un valor puro: nada aquí llama a AWS.
 *
 * Los valores por defecto describen exactamente el despliegue de un solo VM
 * de `AgentProject.deploy()` (1 mínimo, 1 máximo, 0 calientes, sesiones
 * compartidas).
 */

import { InvalidArgumentError } from "../errors.js";
import { IDLE_MAX_IDLE_MIN_SECONDS, POOL_SIZE_MAX } from "../limits.js";

export type SessionMode = "shared" | "dedicated";
/** Las sesiones comparten VM hasta `maxConcurrency`. */
export const SESSIONS_SHARED = "shared";
/** Cada sesión tiene su propio MicroVM y un VM nunca atiende a dos sesiones. */
export const SESSIONS_DEDICATED = "dedicated";
export const SESSION_MODES: readonly string[] = [SESSIONS_SHARED, SESSIONS_DEDICATED];
/** Un solo VM, sin plazas calientes: lo mismo que `AgentProject.deploy()`. */
export const SCALING_MIN_VMS_DEFAULT = 1;
export const SCALING_MAX_VMS_DEFAULT = 1;
export const SCALING_WARM_VMS_DEFAULT = 0;
/** Los VMs por encima de `minVms` se terminan solos tras este tiempo sin uso
 * (`rayd` `set_timeout`): por encima de los ≈ 140 s en que suspender compensa
 * (`AWS_API_NOTES.md`, sección de coste) y por debajo de una hora. */
export const SCALE_IN_AFTER_SECONDS_DEFAULT = 900;
/** Suelo de `scaleInAfterSeconds`: el mínimo de inactividad que admite la
 * plataforma para una política de idle. */
export const SCALE_IN_AFTER_SECONDS_MIN = IDLE_MAX_IDLE_MIN_SECONDS;

export interface ScalingOptionsInput {
  readonly minVms?: number | undefined;
  readonly maxVms?: number | undefined;
  readonly warmVms?: number | undefined;
  readonly sessions?: SessionMode | undefined;
  readonly scaleInAfterSeconds?: number | undefined;
}

function integer(value: unknown, label: string, minimum: number): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum) {
    throw new InvalidArgumentError(`ScalingOptions.${label} debe ser un entero >= ${minimum}`);
  }
  return value;
}

/**
 * Escalado de un agente servido: `minVms` VMs siempre vivos, hasta `maxVms`,
 * y `warmVms` plazas aparcadas (suspendidas) listas para absorber un pico sin
 * esperar un `run-microvm`. `sessions: "shared"` reparte las sesiones entre
 * los VMs con cupo; `"dedicated"` da un MicroVM a cada sesión.
 * `scaleInAfterSeconds` es la inactividad tras la que un VM por encima de
 * `minVms` se termina solo.
 *
 * Coste y activación
 * -------------------
 * Activa: nada por sí mismo; el valor es puro. Lo que cuesta es
 *     `deployAgent()` (o `rayito agent deploy`), que crea `minVms` VMs y
 *     `warmVms` plazas aparcadas.
 * Recursos y llamadas AWS: ninguno aquí.
 * Coste aproximado: ninguno aquí; ver `deployAgent()`.
 * IAM: ninguna para este valor.
 * Cómo apagarla: los valores por defecto (1, 1, 0) son un único VM.
 * Ejemplo:
 *   new ScalingOptions({ minVms: 1, maxVms: 3, warmVms: 1, sessions: "dedicated" });
 */
export class ScalingOptions {
  readonly minVms: number;
  readonly maxVms: number;
  readonly warmVms: number;
  readonly sessions: SessionMode;
  readonly scaleInAfterSeconds: number;

  constructor(options: ScalingOptionsInput = {}) {
    this.minVms = integer(options.minVms ?? SCALING_MIN_VMS_DEFAULT, "minVms", 1);
    this.maxVms = integer(options.maxVms ?? SCALING_MAX_VMS_DEFAULT, "maxVms", 1);
    this.warmVms = integer(options.warmVms ?? SCALING_WARM_VMS_DEFAULT, "warmVms", 0);
    this.scaleInAfterSeconds = integer(
      options.scaleInAfterSeconds ?? SCALE_IN_AFTER_SECONDS_DEFAULT,
      "scaleInAfterSeconds",
      0,
    );
    this.sessions = options.sessions ?? SESSIONS_SHARED;
    if (this.maxVms > POOL_SIZE_MAX) {
      throw new InvalidArgumentError(`ScalingOptions.maxVms debe ser <= ${POOL_SIZE_MAX}`);
    }
    if (this.minVms > this.maxVms) {
      throw new InvalidArgumentError("ScalingOptions.minVms no puede superar maxVms");
    }
    if (this.warmVms > this.maxVms - this.minVms) {
      throw new InvalidArgumentError("ScalingOptions.warmVms no puede superar maxVms - minVms");
    }
    if (!SESSION_MODES.includes(this.sessions)) {
      throw new InvalidArgumentError(
        `ScalingOptions.sessions debe ser uno de ${SESSION_MODES.join(", ")}`,
      );
    }
    if (this.scaleInAfterSeconds < SCALE_IN_AFTER_SECONDS_MIN) {
      throw new InvalidArgumentError(
        `ScalingOptions.scaleInAfterSeconds debe ser >= ${SCALE_IN_AFTER_SECONDS_MIN}`,
      );
    }
  }

  /** Con más de un VM posible, el id de sesión lleva su VM (handle). */
  get routable(): boolean {
    return this.maxVms > 1;
  }
}
