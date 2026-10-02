/**
 * `ArtifactAssembler` (m15-templates). Espejo de
 * `rayito._templates._artifact`: junta el Dockerfile compuesto, los
 * ficheros de contexto y `/etc/rayito/template.json` en un zip
 * determinista sobre el zip de la imagen base (un `RUN`/`COPY` de su
 * propio Dockerfile puede necesitar cualquiera de sus ficheros).
 */

import { composeDockerfile, contextEntryPath, TEMPLATE_JSON_CONTEXT_PATH } from "./dockerfile.js";
import type { StartSpec, TemplateSpec } from "./instructions.js";
import { readZipEntries, writeZip } from "./zip-node.js";

export const DOCKERFILE_NAME = "Dockerfile";

/** El `/etc/rayito/template.json` que lee `rayd` (`rayito.template/1`). */
export function startSpecToJson(start: StartSpec): Uint8Array {
  const payload = {
    version: "rayito.template/1",
    start_cmd: start.startCmd,
    ready_cmd: start.readyCmd ?? null,
    user: start.user,
    workdir: start.workdir ?? null,
    envs: start.envs,
    ready_poll:
      start.readyPoll === undefined
        ? null
        : {
            interval_seconds: start.readyPoll.intervalSeconds,
            timeout_seconds: start.readyPoll.timeoutSeconds,
          },
  };
  return new TextEncoder().encode(`${JSON.stringify(payload, null, 2)}\n`);
}

/** El zip completo que sube `Template.build()`: todas las entradas del zip
 * base, con su Dockerfile sustituido por la composición, más cada fichero
 * de `contextFiles` bajo `CONTEXT_ENTRY_PREFIX` (nunca en el espacio de
 * nombres de la imagen base: un fichero del usuario no puede sustituir el
 * Dockerfile compuesto ni el binario de `rayd`) y `template.json` si
 * `spec.start` está puesto. */
export function assembleArtifact(
  baseZip: Uint8Array,
  spec: TemplateSpec,
  contextFiles: ReadonlyArray<readonly [string, Uint8Array]>,
): Uint8Array {
  const entries = readZipEntries(baseZip);
  const baseDockerfile = new TextDecoder().decode(entries.get(DOCKERFILE_NAME) ?? new Uint8Array());
  entries.set(DOCKERFILE_NAME, new TextEncoder().encode(composeDockerfile(baseDockerfile, spec)));
  for (const [path, content] of contextFiles) {
    entries.set(contextEntryPath(path), content);
  }
  if (spec.start !== undefined) {
    entries.set(TEMPLATE_JSON_CONTEXT_PATH, startSpecToJson(spec.start));
  }
  const sortedEntries = [...entries.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return writeZip(sortedEntries);
}
