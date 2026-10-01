/**
 * La unión pura de `list-microvms` con las filas del índice (M14), espejo de
 * `join_index` de Python. Una fila nunca inventa un sandbox: el ESTADO sale
 * siempre de `list-microvms`; un item sólo se queda si hay una fila con su
 * mismo id, su mismo ARN de imagen y su mismo `startedAt` (±1 s), no
 * caducada, y cuyos metadatos contienen cada par pedido. Un item sin fila se
 * excluye (sus metadatos son desconocidos) y nunca se sondea.
 */

import type { SandboxListItem } from "../models.js";
import type { IndexRecord } from "./record.js";

export const STARTED_AT_TOLERANCE_MS = 1000;

/** El item con `metadata` relleno si su fila casa; si no, `undefined`. */
export function joined(
  item: SandboxListItem,
  record: IndexRecord | undefined,
  wanted: Readonly<Record<string, string>>,
  nowSeconds: number,
): SandboxListItem | undefined {
  if (record === undefined || record.sandboxId !== item.sandboxId) {
    return undefined;
  }
  if (record.expiresAt < nowSeconds || record.imageArn !== item.template) {
    return undefined;
  }
  if (Math.abs(record.startedAtMs - item.startedAt.getTime()) > STARTED_AT_TOLERANCE_MS) {
    return undefined;
  }
  const matches = Object.entries(wanted).every(
    ([key, value]) => Object.hasOwn(record.metadata, key) && record.metadata[key] === value,
  );
  if (!matches) {
    return undefined;
  }
  return Object.freeze({ ...item, metadata: Object.freeze({ ...record.metadata }) });
}

/** `joined` sobre una página: los items que conserva, en su orden. */
export function joinIndex(
  items: Iterable<SandboxListItem>,
  records: ReadonlyMap<string, IndexRecord>,
  wanted: Readonly<Record<string, string>>,
  nowSeconds: number,
): SandboxListItem[] {
  const kept: SandboxListItem[] = [];
  for (const item of items) {
    const result = joined(item, records.get(item.sandboxId), wanted, nowSeconds);
    if (result !== undefined) {
      kept.push(result);
    }
  }
  return kept;
}
