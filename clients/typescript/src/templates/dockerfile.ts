/**
 * `DockerfileRenderer` (dominio puro, investigación §3.5). Espejo de
 * `rayito._templates._dockerfile`: compila las instrucciones de cable de un
 * `TemplateSpec` en texto Dockerfile, e inserta esa capa en el Dockerfile
 * de la imagen base justo antes de su última `CMD`/`ENTRYPOINT`, cerrando
 * con `USER root` y esa misma instrucción repetida, para que `rayd` siga
 * siendo PID 1.
 */

import { BuildError } from "../errors.js";
import type { TemplateSpec, WireStep } from "./instructions.js";

export const LAYER_BEGIN_MARKER = "# >>> rayito template layer (m15-templates), no editar a mano";
export const LAYER_END_MARKER = "# <<< rayito template layer";

/** Ruta del `StartSpec` serializado dentro del contexto de build, y ruta a
 * la que esa `COPY` lo deja en la imagen (`rayito.template/1`). */
export const TEMPLATE_JSON_CONTEXT_PATH = "__rayito_template.json";
export const TEMPLATE_JSON_IMAGE_PATH = "/etc/rayito/template.json";

const TERMINAL_KEYWORDS = ["CMD", "ENTRYPOINT"];

function renderStep(step: WireStep): string {
  switch (step.kind) {
    case "copy":
      return `COPY ${step.src} ${step.dst}`;
    case "env":
      return `ENV ${step.key}="${step.value}"`;
    case "run":
      return `RUN ${step.cmd}`;
    case "workdir":
      return `WORKDIR ${step.path}`;
    case "user":
      return `USER ${step.user}`;
  }
}

/** La capa que este `Template` añade, en orden, como texto Dockerfile. No
 * incluye `FROM`: eso depende de la imagen base, resuelta sólo al construir. */
export function renderAppendedLayer(spec: TemplateSpec): string {
  const lines = [LAYER_BEGIN_MARKER, ...spec.steps.map(renderStep)];
  if (spec.start !== undefined) {
    lines.push(`COPY ${TEMPLATE_JSON_CONTEXT_PATH} ${TEMPLATE_JSON_IMAGE_PATH}`);
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
