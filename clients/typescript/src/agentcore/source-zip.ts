/**
 * El zip del contexto de build (ADR-031, D5): los ficheros de
 * `renderExport(project, "agentcore")` en orden de ruta, con fecha fija y modo
 * `0644`, para que el mismo árbol dé siempre el mismo contenido. Se sube al
 * bucket de artefactos justo antes de `StartBuild` y se borra al terminar el
 * build. Puro.
 */

import { writeZipEntries } from "../templates/zip-node.js";

const UTF8 = new TextEncoder();

export function sourceZip(tree: ReadonlyMap<string, string | Uint8Array>): Uint8Array {
  const entries = [...tree.entries()]
    .sort(([left], [right]) => (left < right ? -1 : 1))
    .map(([name, content]) => ({
      name,
      content: typeof content === "string" ? UTF8.encode(content) : content,
    }));
  return writeZipEntries(entries, { compress: true, unixCreator: true });
}
