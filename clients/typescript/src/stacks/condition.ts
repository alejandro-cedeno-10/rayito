/**
 * Qué es una pila de CloudFormation para quien decide si desplegarla: ausente,
 * utilizable, en curso o bloqueada. Lo comparten `rayito setup` (ADR-030) y
 * `--target agentcore` (ADR-031). Espejo de `rayito._stacks._condition`. Puro:
 * no llama a AWS.
 */

/** Estados de pila que sirven tal cual (`UPDATE_ROLLBACK_COMPLETE` vuelve a la última plantilla buena). */
export const USABLE_STACK_STATES: ReadonlySet<string> = new Set([
  "CREATE_COMPLETE",
  "UPDATE_COMPLETE",
  "UPDATE_ROLLBACK_COMPLETE",
]);
export const IN_PROGRESS_STACK_SUFFIX = "_IN_PROGRESS";
/** Esperar a que una pila que se borra "esté desplegada" no terminaría nunca. */
export const DELETING_STACK_STATE = "DELETE_IN_PROGRESS";

export type StackCondition = "missing" | "usable" | "in_progress" | "blocked";

export interface StackObservation {
  readonly name: string;
  /** `undefined` si la pila no existe. */
  readonly status: string | undefined;
  readonly outputs: Readonly<Record<string, string>>;
  readonly parameters: Readonly<Record<string, string>>;
}

export function classifyStack(stack: StackObservation): StackCondition {
  const status = stack.status;
  if (status === undefined) {
    return "missing";
  }
  if (USABLE_STACK_STATES.has(status)) {
    return "usable";
  }
  if (status !== DELETING_STACK_STATE && status.endsWith(IN_PROGRESS_STACK_SUFFIX)) {
    return "in_progress";
  }
  return "blocked";
}
