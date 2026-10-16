/**
 * Piezas compartidas de las recetas de instalación (ADR-028, design.md D8).
 * Espejo de `rayito._agent._recipes`: las rutas de la imagen del agente, el
 * instalador de un venv con requisitos con hash y los datos de paquete
 * (requisitos, demonio de precarga y host del agente). Cada runtime compone
 * sus `installSteps()` con estas piezas; `AgentTemplate` sólo las ensambla.
 *
 * Puro. Ningún valor de entorno concreto vive aquí: los pines llegan de
 * `limits.json`.
 */

import { createHash } from "node:crypto";
import { AGENT_HOST_FILES } from "./assets/host-assets.gen.js";
import { PREFETCH_SCRIPT } from "./assets/template-assets.gen.js";
import { DEFAULT_INSTALL_DIR, type InstallTarget } from "./runtime.js";

/** Directorio raíz de todo lo que la plantilla instala (root, 0755). */
export const AGENT_INSTALL_DIR = DEFAULT_INSTALL_DIR;
export const AGENT_BIN_DIR = `${AGENT_INSTALL_DIR}/bin`;
/** El código del usuario (`app`): de root, sin escritura para `user`. */
export const AGENT_APP_DIR = `${AGENT_INSTALL_DIR}/app`;
/** Lo que Rayito instala para ejecutar agentes (runner y host), de root. */
export const AGENT_RAYITO_DIR = `${AGENT_INSTALL_DIR}/rayito`;
/** El host del agente (`agent_host`) que sirve y ejecuta los runtimes. */
export const AGENT_HOST_DIR = `${AGENT_RAYITO_DIR}/agent_host`;
/** Nombres de las carpetas del contexto de build que `AgentTemplate` copia. */
export const AGENT_HOST_CONTEXT_DIR = "agent_host";
export const AGENT_APP_CONTEXT_DIR = "app";
export const PREFETCH_SCRIPT_NAME = "rayito-agent-prefetch";
export const PREFETCH_SCRIPT_PATH = `${AGENT_BIN_DIR}/${PREFETCH_SCRIPT_NAME}`;

/** El demonio de precarga (`rayito-agent-prefetch`). */
export function prefetchScript(): string {
  return PREFETCH_SCRIPT;
}

/** Los ficheros del host del agente, con su ruta relativa a `agent_host/`;
 * vacío mientras el paquete no lo incluya. */
export function agentHostFiles(): Readonly<Record<string, string>> {
  return AGENT_HOST_FILES;
}

/** sha256 de `ruta NUL contenido NUL` de cada fichero del host, por orden de
 * ruta; `undefined` si el paquete no incluye el host. */
export function hostSha256(files: Readonly<Record<string, string>>): string | undefined {
  const paths = Object.keys(files).sort();
  if (paths.length === 0) {
    return undefined;
  }
  const hash = createHash("sha256");
  for (const path of paths) {
    hash.update(path, "utf8");
    hash.update(Uint8Array.of(0));
    hash.update(files[path] as string, "utf8");
    hash.update(Uint8Array.of(0));
  }
  return hash.digest("hex");
}

/** El `RUN` que comprueba el sha256 del fichero de requisitos, crea el venv,
 * instala con `--require-hashes --no-deps --only-binary=:all:` y termina con
 * `pip check`. */
export function hashedVenvInstall(
  target: InstallTarget,
  options: {
    readonly venvDir: string;
    readonly requirementsPath: string;
    readonly requirementsSha256: string;
  },
): string {
  const venvPython = `${options.venvDir}/bin/python`;
  return (
    `echo '${options.requirementsSha256}  ${options.requirementsPath}' | sha256sum -c -` +
    ` && ${target.python} -m venv ${options.venvDir}` +
    ` && ${venvPython} -m pip install --no-cache-dir` +
    " --require-hashes --no-deps --only-binary=:all:" +
    ` -r ${options.requirementsPath}` +
    ` && ${venvPython} -m pip check`
  );
}
