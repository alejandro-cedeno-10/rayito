/**
 * El puerto `ProjectReader` (agentcore-cdk, ADR-032 D7): lo único que la
 * detección de un proyecto, su export y el plan de AgentCore leen del disco
 * fuera de la detección misma (el fichero de proyecto, el parser de TOML y
 * el código del usuario). Hay dos adaptadores con las mismas tres lecturas:
 * el asíncrono del SDK (`nodeProjectReader`, `fs/promises` y el peer
 * opcional `smol-toml` importado bajo demanda) y el síncrono de los
 * constructos CDK (`syncProjectReader`, cuyos constructores no pueden
 * esperar). Las reglas se escriben una sola vez como pasos (`steps.ts`) que
 * piden estas lecturas, así que las dos rutas no pueden divergir.
 */

import { readFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { InvalidArgumentError } from "../errors.js";
import { loadOptionalPeer } from "../optional.js";
import { runSteps, runStepsAsync, type Steps } from "../steps.js";
import { appFiles, appFilesSync } from "./template.js";

/** El parser de TOML del peer opcional `smol-toml`. */
export interface TomlParser {
  parse(text: string): Record<string, unknown>;
}

/** El peer opcional que parsea TOML. */
export const TOML_PEER = "smol-toml";

/** Una lectura que piden los pasos de la detección, el export o el plan. */
export type ProjectRead =
  | { readonly kind: "text"; readonly file: string }
  | { readonly kind: "toml"; readonly feature: string }
  | {
      readonly kind: "appFiles";
      readonly source: string;
      readonly exclude: readonly string[];
    };

/** Los pasos de un cálculo que lee el proyecto a través de `ProjectReader`. */
export type ProjectSteps<T> = Steps<ProjectRead, T>;

/** El puerto asíncrono (el SDK). */
export interface ProjectReader {
  readText(file: string): Promise<string>;
  tomlParser(feature: string): Promise<TomlParser>;
  appFiles(source: string, exclude: readonly string[]): Promise<Array<[string, Uint8Array]>>;
}

/** El mismo puerto con lecturas síncronas (los constructos CDK). */
export interface SyncProjectReader {
  readText(file: string): string;
  tomlParser(feature: string): TomlParser;
  appFiles(source: string, exclude: readonly string[]): Array<[string, Uint8Array]>;
}

/** El texto UTF-8 de `file`. */
export function* readTextStep(file: string): ProjectSteps<string> {
  return (yield { kind: "text", file }) as string;
}

/** El parser de TOML; `feature` nombra lo que lo necesita en el error de peer ausente. */
export function* tomlParserStep(feature: string): ProjectSteps<TomlParser> {
  return (yield { kind: "toml", feature }) as TomlParser;
}

/** Los ficheros del código del usuario (`appFiles` de `template.ts`). */
export function* appFilesStep(
  source: string,
  exclude: readonly string[],
): ProjectSteps<Array<[string, Uint8Array]>> {
  return (yield { kind: "appFiles", source, exclude }) as Array<[string, Uint8Array]>;
}

/** Ejecuta `steps` con el adaptador asíncrono `reader`. */
export function runWithReader<T>(steps: ProjectSteps<T>, reader: ProjectReader): Promise<T> {
  return runStepsAsync(steps, (request) => {
    switch (request.kind) {
      case "text":
        return reader.readText(request.file);
      case "toml":
        return reader.tomlParser(request.feature);
      case "appFiles":
        return reader.appFiles(request.source, request.exclude);
    }
  });
}

/** Ejecuta `steps` con el adaptador síncrono `reader`. */
export function runWithSyncReader<T>(steps: ProjectSteps<T>, reader: SyncProjectReader): T {
  return runSteps(steps, (request) => {
    switch (request.kind) {
      case "text":
        return reader.readText(request.file);
      case "toml":
        return reader.tomlParser(request.feature);
      case "appFiles":
        return reader.appFiles(request.source, request.exclude);
    }
  });
}

/** El adaptador asíncrono de disco: el de `AgentProject.detect`, `renderExport` y `deployAgent`. */
export const nodeProjectReader: ProjectReader = {
  readText: (file) => readFile(file, "utf8"),
  tomlParser: (feature) => loadOptionalPeer<TomlParser>(TOML_PEER, feature),
  appFiles: (source, exclude) => appFiles(source, exclude),
};

const MODULE_NOT_FOUND_CODES: ReadonlySet<unknown> = new Set([
  "MODULE_NOT_FOUND",
  "ERR_MODULE_NOT_FOUND",
]);

/**
 * `smol-toml` con `require` (el paquete publica también CommonJS). Se
 * resuelve desde el directorio de trabajo, el de la app de CDK, como haría
 * su propio `require`; si no está, el mismo error que `loadOptionalPeer`.
 */
function requireToml(feature: string): TomlParser {
  try {
    return createRequire(`${process.cwd()}/`)(TOML_PEER) as TomlParser;
  } catch (cause) {
    if (!MODULE_NOT_FOUND_CODES.has((cause as { code?: unknown } | null)?.code)) {
      throw cause;
    }
    throw new InvalidArgumentError(
      `${feature} necesita el paquete opcional '${TOML_PEER}': instala npm install ${TOML_PEER}`,
      { cause },
    );
  }
}

/** El adaptador síncrono de disco: el de `planAgentCoreSync` y los constructos CDK. */
export const syncProjectReader: SyncProjectReader = {
  readText: (file) => readFileSync(file, "utf8"),
  tomlParser: requireToml,
  appFiles: (source, exclude) => appFilesSync(source, exclude),
};
