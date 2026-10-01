/**
 * Dominio puro del convenio `OptionalStack` (M15 foundations, ADR-016).
 * Espejo de `rayito._stacks._model`. Nada aquí importa un SDK de AWS: las
 * llamadas a CloudFormation viven en `cloudformation.ts`, detrás del puerto
 * `StackProvisioner` (`port.ts`).
 */

import { VERSION } from "../version.js";

export const MANAGED_BY_TAG = "rayito-sdk";
export const ROLLBACK_COMPLETE = "ROLLBACK_COMPLETE";

export type DeployAction = "create" | "update" | "blocked";

export interface StackParameter {
  readonly name: string;
  readonly description: string;
  readonly required?: boolean;
  readonly default?: string | undefined;
  readonly noEcho?: boolean;
}

/** Un fichero que `deploy()` sube a `artifactBucket` antes de desplegar la
 * plantilla (código Lambda); `parameterKey` es el parámetro de la plantilla
 * al que se pasa la clave S3 resultante (sha256 del contenido). */
export interface StackArtifact {
  readonly name: string;
  readonly parameterKey: string;
}

/** El bloque "Coste y activación" de cada componente, de forma estructurada. */
export interface CostStatement {
  readonly creates: readonly string[];
  readonly idleMonthly: string;
  readonly perUse?: readonly string[];
  readonly removal?: string;
  readonly source?: string;
}

export interface StackComponent {
  readonly name: string;
  readonly description: string;
  readonly cost: CostStatement;
  readonly parameters?: readonly StackParameter[];
  readonly artifacts?: readonly StackArtifact[];
  readonly capabilities?: readonly string[];
  /** `false` para un componente que `list()` ya describe pero cuya función
   * todavía no tiene plantilla: `deploy`/`status`/`destroy` lanzan
   * `UnimplementedError` antes de tocar AWS. */
  readonly supported?: boolean;
}

export function defaultStackName(component: StackComponent): string {
  return `rayito-${component.name}`;
}

export interface StackStatus {
  readonly name: string;
  readonly state: string | undefined;
  readonly outputs: Readonly<Record<string, string>>;
  readonly reasonCode?: string | undefined;
}

export function stackExists(status: StackStatus | undefined): boolean {
  return status !== undefined && status.state !== undefined;
}

export interface DeployPlan {
  readonly action: DeployAction;
  readonly reason?: string;
}

/**
 * `current === undefined` (la pila no existe) -> `"create"`;
 * `ROLLBACK_COMPLETE` -> `"blocked"` (hay que `destroy()` primero);
 * cualquier otro estado existente -> `"update"`. `"no_changes"` no es un
 * resultado de este plan: sólo se sabe tras intentar `UpdateStack`
 * (`StackProvisioner.update()` devuelve `"changed"`/`"no_changes"`).
 */
export function planDeploy(current: StackStatus | undefined): DeployPlan {
  if (!stackExists(current)) {
    return { action: "create" };
  }
  if (current?.state === ROLLBACK_COMPLETE) {
    return {
      action: "blocked",
      reason:
        `la pila ${JSON.stringify(current.name)} está en ROLLBACK_COMPLETE: bórrala ` +
        "(destroy) antes de volver a desplegarla",
    };
  }
  return { action: "update" };
}

/** Las tres etiquetas fijas siempre ganan sobre las del llamante. */
export function stackTags(
  component: StackComponent,
  userTags: Readonly<Record<string, string>>,
): Record<string, string> {
  return {
    ...userTags,
    "rayito:component": component.name,
    "rayito:managed-by": MANAGED_BY_TAG,
    "rayito:sdk-version": VERSION,
  };
}
