/**
 * Las líneas de progreso de `--target agentcore`: cada una es un `AgentDeployStep`
 * (`kind` es la clave de `messages.ts` y `detail` la línea en español con sus
 * datos), que `onStep` recibe y la CLI imprime en stderr. Puro.
 */

import { formatTemplate, type TemplateFields } from "../template-text.js";
import {
  DELETE_PROGRESS_MESSAGES,
  type DeleteProgressKey,
  PROGRESS_MESSAGES,
  type ProgressKey,
} from "./messages.js";

export interface ProgressStep {
  readonly kind: string;
  readonly detail: string;
}

export type OnStep = ((step: ProgressStep) => void) | undefined;
export type Emit = (key: ProgressKey, fields?: TemplateFields) => void;
export type EmitDelete = (key: DeleteProgressKey, fields: TemplateFields) => void;

export function deployEmitter(onStep: OnStep): Emit {
  return (key, fields = {}) => {
    onStep?.({ kind: key, detail: formatTemplate(PROGRESS_MESSAGES[key], fields) });
  };
}

export function deleteEmitter(onStep: OnStep): EmitDelete {
  return (key, fields) => {
    onStep?.({ kind: key, detail: formatTemplate(DELETE_PROGRESS_MESSAGES[key], fields) });
  };
}
