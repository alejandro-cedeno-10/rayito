/**
 * `DockerfileRenderer` (dominio puro, investigación §3.5). Espejo de
 * `rayito._templates._dockerfile`: compila las instrucciones de cable de un
 * `TemplateSpec` en texto Dockerfile, e inserta esa capa en el Dockerfile
 * de la imagen base justo antes de su última `CMD`/`ENTRYPOINT`, cerrando
 * con `USER root` y esa misma instrucción repetida, para que `rayd` siga
 * siendo PID 1.
 */

import { BuildError, InvalidArgumentError } from "../errors.js";
import type { TemplateSpec, WireStep } from "./instructions.js";

export const LAYER_BEGIN_MARKER = "# >>> rayito template layer (m15-templates), no editar a mano";
export const LAYER_END_MARKER = "# <<< rayito template layer";

/** Ruta del `StartSpec` serializado dentro del contexto de build, y ruta a
 * la que esa `COPY` lo deja en la imagen (`rayito.template/1`). */
export const TEMPLATE_JSON_CONTEXT_PATH = "__rayito_template.json";
export const TEMPLATE_JSON_IMAGE_PATH = "/etc/rayito/template.json";

/** Directorio reservado del zip para los ficheros de contexto del usuario
 * (`artifact.ts`), al que apunta cada `COPY` renderizada (amenaza T26). */
export const CONTEXT_ENTRY_PREFIX = "__rayito_context";

const TERMINAL_KEYWORDS = ["CMD", "ENTRYPOINT"];
/** Nombre de variable de entorno aceptado en `ENV` (POSIX §8.1). */
const ENV_KEY_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;
/** Un salto de línea terminaría la instrucción y empezaría otra. */
const LINE_BREAK_PATTERN = /[\n\r]/;
/** Caracteres que el parser de Dockerfile interpreta dentro de un `ENV`
 * entre comillas dobles (barra invertida primero). */
const ENV_ESCAPES: ReadonlyArray<readonly [string, string]> = [
  ["\\", "\\\\"],
  ['"', '\\"'],
  ["$", "\\$"],
];

/** Ruta dentro del zip de un fichero de contexto (o de un `CopyStep.src`). */
export function contextEntryPath(relpath: string): string {
  return `${CONTEXT_ENTRY_PREFIX}/${relpath}`;
}

function singleLine(value: string, instruction: string): string {
  if (LINE_BREAK_PATTERN.test(value)) {
    throw new InvalidArgumentError(
      `${instruction}: los valores no pueden contener saltos de línea (cada uno terminaría la ` +
        "instrucción Dockerfile y empezaría otra)",
    );
  }
  return value;
}

/** Forma JSON de `COPY`, con el mismo separador que `_dockerfile._json_array`. */
function jsonArray(...values: string[]): string {
  return `[${values.map((value) => JSON.stringify(value)).join(", ")}]`;
}

function escapeEnvValue(value: string): string {
  return ENV_ESCAPES.reduce((text, [raw, escaped]) => text.split(raw).join(escaped), value);
}

function renderCopy(src: string, dst: string): string {
  return `COPY ${jsonArray(singleLine(src, "COPY"), singleLine(dst, "COPY"))}`;
}

function renderStep(step: WireStep): string {
  switch (step.kind) {
    case "copy":
      return renderCopy(contextEntryPath(step.src), step.dst);
    case "env":
      if (!ENV_KEY_PATTERN.test(step.key)) {
        throw new InvalidArgumentError(
          `ENV: clave inválida ${JSON.stringify(step.key)} ([A-Za-z_][A-Za-z0-9_]*)`,
        );
      }
      return `ENV ${step.key}="${escapeEnvValue(singleLine(step.value, "ENV"))}"`;
    case "run":
      return `RUN ${singleLine(step.cmd, "RUN")}`;
    case "workdir":
      return `WORKDIR ${singleLine(step.path, "WORKDIR")}`;
    case "user":
      return `USER ${singleLine(step.user, "USER")}`;
  }
}

/** La capa que este `Template` añade, en orden, como texto Dockerfile. No
 * incluye `FROM`: eso depende de la imagen base, resuelta sólo al construir. */
export function renderAppendedLayer(spec: TemplateSpec): string {
  const lines = [LAYER_BEGIN_MARKER, ...spec.steps.map(renderStep)];
  if (spec.start !== undefined) {
    lines.push(renderCopy(TEMPLATE_JSON_CONTEXT_PATH, TEMPLATE_JSON_IMAGE_PATH));
  }
  lines.push(LAYER_END_MARKER);
  return `${lines.join("\n")}\n`;
}

function splitTerminalInstruction(baseDockerfile: string): { head: string; terminal: string } {
  const lines = baseDockerfile.split("\n");
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const stripped = (lines[index] ?? "").trim();
    if (TERMINAL_KEYWORDS.some((keyword) => stripped.startsWith(keyword))) {
      const head = lines.slice(0, index).join("\n");
      return { head: head.length > 0 ? `${head}\n` : "", terminal: lines[index] ?? "" };
    }
  }
  throw new BuildError(
    "la imagen base no tiene una instrucción CMD/ENTRYPOINT final: Template.build() no puede " +
      "garantizar que rayd siga siendo PID 1",
    { reason: "base_image_missing_entrypoint" },
  );
}

/** El Dockerfile completo que sube `Template.build()`. */
export function composeDockerfile(baseDockerfile: string, spec: TemplateSpec): string {
  const { head, terminal } = baseDockerfile.includes(LAYER_BEGIN_MARKER)
    ? {
        head: baseDockerfile.split(LAYER_BEGIN_MARKER, 1)[0] ?? "",
        terminal: splitTerminalInstruction(baseDockerfile).terminal,
      }
    : splitTerminalInstruction(baseDockerfile);
  const layer = renderAppendedLayer(spec);
  return `${head}${layer}USER root\n${terminal}\n`;
}
