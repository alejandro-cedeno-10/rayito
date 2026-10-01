/**
 * Puerto `StackProvisioner` (M15 foundations, ADR-016): lo que
 * `OptionalStacks` necesita de CloudFormation, sin saber si detrás hay el
 * SDK de AWS de verdad o un doble de test.
 */

import type { StackComponent, StackStatus } from "./model.js";

export type UpdateOutcome = "changed" | "no_changes";
export type DeployTarget = "deployed" | "deleted";

export interface StackProvisioner {
  /** `undefined` si la pila no existe. */
  describe(stackName: string): Promise<StackStatus | undefined>;

  create(
    component: StackComponent,
    options: {
      readonly stackName: string;
      readonly templateBody: string;
      readonly parameters: Readonly<Record<string, string>>;
      readonly tags: Readonly<Record<string, string>>;
    },
  ): Promise<void>;

  /** `"no_changes"` cuando CloudFormation responde "No updates are to be
   * performed" (no es un fallo). */
  update(
    component: StackComponent,
    options: {
      readonly stackName: string;
      readonly templateBody: string;
      readonly parameters: Readonly<Record<string, string>>;
      readonly tags: Readonly<Record<string, string>>;
    },
  ): Promise<UpdateOutcome>;

  /** Idempotente: borrar una pila que no existe no es un error. */
  delete(stackName: string): Promise<void>;

  /** Bloquea hasta que la pila alcanza un estado terminal del target pedido
   * o `timeoutMs` expira (`StackError` con `code: "in_progress"`). */
  wait(stackName: string, target: DeployTarget, timeoutMs: number): Promise<void>;

  /** Sube `data` a `bucket/key` sólo si no está ya (idempotente). */
  putArtifact(bucket: string, key: string, data: Uint8Array): Promise<void>;

  /** El motivo más reciente del fallo, o `undefined` si la pila no existe o
   * no falló. */
  failureReason(stackName: string): Promise<string | undefined>;
}
