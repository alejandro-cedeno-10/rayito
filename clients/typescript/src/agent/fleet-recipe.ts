/**
 * La receta de una flota de agentes (`rayito.agent-recipe/1`, ADR-034 D1 y
 * D2). Espejo de `rayito._agent._fleet_recipe`: lo que `deployAgent()` usó
 * para arrancarla (el proyecto tras las banderas, la versión de la imagen, los
 * presets de las pasarelas, el escalado y la renovación de claves), para que
 * `AgentFleet.connect(nombre)` cree VMs iguales en cualquier proceso sin el
 * proyecto.
 *
 * Dominio puro: construir el documento, codificarlo (JSON compacto con claves
 * ordenadas o, si no cabe en un parámetro estándar, el sobre `gzip+base64`),
 * decodificarlo y validarlo, y el nombre del parámetro. La E/S con Parameter
 * Store vive en `fleet-recipe-aws.ts`. La receta sólo lleva nombres de
 * secretos, nunca valores ni el access token.
 */

import { gunzipSync, gzipSync } from "node:zlib";
import { InvalidArgumentError } from "../errors.js";
import {
  AGENT_RECIPE_MAX_BYTES,
  AGENT_RECIPE_PARAMETER_TEMPLATE,
  AGENT_RECIPE_SCHEMA,
} from "../limits.js";
import { MS_PER_SECOND } from "../units.js";
import { VERSION } from "../version.js";
import { AgentProject } from "./project.js";
import { copyProject } from "./project-copy.js";
import { ScalingOptions, type SessionMode } from "./scaling-domain.js";

/** El sobre de una receta que no cabe en `agentRecipeMaxBytes` como JSON. */
export const RECIPE_ENCODING_GZIP = "gzip+base64";
/** La regla de nombres de Parameter Store para cada nivel de la ruta
 * (API_PutParameter.html, consultado 2026-10-22). */
export const RECIPE_NAME_PATTERN = /^[A-Za-z0-9_.-]+$/;
/** Los nombres que cumplen el patrón pero son un nivel relativo de la ruta
 * (`/rayito/agents/../recipe`): nunca nombran una receta. */
export const RECIPE_RESERVED_NAMES: ReadonlySet<string> = new Set([".", ".."]);
/** Nivel de compresión del gzip del sobre (el mismo que Python). */
export const RECIPE_GZIP_LEVEL = 9;
/** `savedBy` de las recetas que guarda este SDK. */
export const RECIPE_SAVED_BY = `typescript/${VERSION}`;
/** Los avisos de la receta (lista cerrada, los mismos en los dos SDK). Nunca
 * llevan la ruta del parámetro ni su valor. */
export const RECIPE_ACCESS_DENIED = "recipe_access_denied";
export const RECIPE_NOT_SAVED = "recipe_not_saved";
export const RECIPE_TOO_LARGE = "recipe_too_large";
export const RECIPE_UNREADABLE = "recipe_unreadable";
/** La receta pide renovar las claves de Bedrock pero quien conecta no lo
 * pidió (`renewGatewayKeys: true`): la flota no renueva ninguna. */
export const RECIPE_RENEWAL_IGNORED = "recipe_renewal_ignored";
export const RECIPE_WARNING_CODES = [
  RECIPE_ACCESS_DENIED,
  RECIPE_NOT_SAVED,
  RECIPE_RENEWAL_IGNORED,
  RECIPE_TOO_LARGE,
  RECIPE_UNREADABLE,
] as const;
export type RecipeWarningCode = (typeof RECIPE_WARNING_CODES)[number];
/** El directorio base del proyecto rehecho desde la receta: sus rutas
 * relativas no se usan para servir (la imagen ya está construida). */
const RECIPE_PROJECT_BASE_DIR = ".";
const BASE64_PATTERN = /^[A-Za-z0-9+/]*={0,2}$/;
const ENVELOPE_KEYS = ["data", "encoding", "schema"];

/** Una receta que no se guardó, no se pudo leer o no se pudo borrar: la flota
 * sigue como en 0.16 y quien la captura emite `code` como aviso. */
export class RecipeWarning extends Error {
  readonly code: RecipeWarningCode;

  constructor(code: RecipeWarningCode) {
    super(code);
    this.name = "RecipeWarning";
    this.code = code;
  }
}

/** Una receta ya validada. `project` es el documento de `AgentProject`
 * (`toObject()`) tras las banderas del despliegue; `imageVersion`, la versión
 * de la imagen (`undefined`: la más reciente activa). */
export interface FleetRecipe {
  readonly name: string;
  readonly savedBy: string;
  readonly imageName: string;
  readonly imageVersion: string | undefined;
  readonly project: Readonly<Record<string, unknown>>;
  readonly gatewayPresets: Readonly<Record<string, string>>;
  readonly scaling: ScalingOptions;
  readonly renewGatewayKeys: boolean;
}

/** El parámetro de la receta de `fleetName` (`agentRecipeParameterTemplate`),
 * o `undefined` si el nombre no cumple la regla de Parameter Store: esa flota
 * no tiene receta (`recipe_not_saved`). */
export function recipeParameterName(fleetName: string): string | undefined {
  if (RECIPE_RESERVED_NAMES.has(fleetName) || !RECIPE_NAME_PATTERN.test(fleetName)) {
    return undefined;
  }
  return AGENT_RECIPE_PARAMETER_TEMPLATE.replace("{name}", fleetName);
}

/** Si una flota reconectada renueva las claves de Bedrock y el aviso que toca
 * (`testdata/fleet-recipe/renewal.json`, `connect`).
 *
 * Sólo renueva quien lo pide (`requested: true`): la receta nunca la activa,
 * porque la clave se firma con las credenciales de quien conecta. Si la
 * receta la pedía y el llamante no dijo nada (`undefined`), el aviso es
 * `recipe_renewal_ignored`; un `false` explícito no avisa. */
export function renewalOnConnect(
  recipe: FleetRecipe | undefined,
  requested: boolean | undefined,
): { readonly renew: boolean; readonly warning: RecipeWarningCode | undefined } {
  if (requested === undefined && recipe?.renewGatewayKeys === true) {
    return { renew: false, warning: RECIPE_RENEWAL_IGNORED };
  }
  return { renew: requested === true, warning: undefined };
}

/** La receta de una flota recién arrancada con `project`. */
export function recipeOf(
  project: AgentProject,
  options: {
    readonly name: string;
    readonly imageVersion: string | undefined;
    readonly scaling: ScalingOptions;
    readonly renewGatewayKeys: boolean;
    readonly savedBy?: string | undefined;
  },
): FleetRecipe {
  return {
    name: options.name,
    savedBy: options.savedBy ?? RECIPE_SAVED_BY,
    imageName: project.template ?? project.name,
    imageVersion: options.imageVersion,
    project: project.toObject(),
    gatewayPresets: { ...project.gatewayPresets },
    scaling: options.scaling,
    renewGatewayKeys: options.renewGatewayKeys,
  };
}

/** El documento `rayito.agent-recipe/1`, en camelCase en los dos SDK y con
 * el escalado en segundos. */
export function recipeDocument(recipe: FleetRecipe): Record<string, unknown> {
  return {
    schema: AGENT_RECIPE_SCHEMA,
    name: recipe.name,
    savedBy: recipe.savedBy,
    image:
      recipe.imageVersion === undefined
        ? { name: recipe.imageName }
        : { name: recipe.imageName, version: recipe.imageVersion },
    project: recipe.project,
    gatewayPresets: recipe.gatewayPresets,
    scaling: {
      minVms: recipe.scaling.minVms,
      maxVms: recipe.scaling.maxVms,
      warmVms: recipe.scaling.warmVms,
      sessions: recipe.scaling.sessions,
      scaleInAfterSeconds: recipe.scaling.scaleInAfterMs / MS_PER_SECOND,
    },
    renewGatewayKeys: recipe.renewGatewayKeys,
  };
}

/** JSON compacto con las claves ordenadas, como `json.dumps(sort_keys=True,
 * separators=(",", ":"), ensure_ascii=False)` de Python: los dos SDK escriben
 * los mismos bytes. */
export function canonicalJson(document: unknown): string {
  return JSON.stringify(sorted(document));
}

/** El valor del parámetro: el JSON si cabe en `agentRecipeMaxBytes`; si no,
 * el sobre `gzip+base64`; `undefined` si tampoco cabe (`recipe_too_large`). */
export function encodeRecipe(document: Readonly<Record<string, unknown>>): string | undefined {
  const plain = canonicalJson(document);
  if (fits(plain)) {
    return plain;
  }
  const compressed = gzipSync(Buffer.from(plain, "utf8"), { level: RECIPE_GZIP_LEVEL });
  const envelope = canonicalJson({
    schema: AGENT_RECIPE_SCHEMA,
    encoding: RECIPE_ENCODING_GZIP,
    data: compressed.toString("base64"),
  });
  return fits(envelope) ? envelope : undefined;
}

/** La receta de un valor de Parameter Store, en cualquiera de sus dos
 * formas. Otro esquema, otra codificación o un documento mal formado:
 * `RecipeWarning("recipe_unreadable")`. */
export function decodeRecipe(value: string): FleetRecipe {
  let document = jsonObject(value);
  if ("encoding" in document) {
    document = unwrap(document);
  }
  return recipeFromDocument(document);
}

/** La receta de un documento ya decodificado, validada. */
export function recipeFromDocument(document: Readonly<Record<string, unknown>>): FleetRecipe {
  if (document.schema !== AGENT_RECIPE_SCHEMA) {
    throw new RecipeWarning(RECIPE_UNREADABLE);
  }
  const image = record(document.image);
  const project = record(document.project);
  const presets = record(document.gatewayPresets);
  const scaling = record(document.scaling);
  const version = image.version;
  if (
    typeof document.name !== "string" ||
    typeof document.savedBy !== "string" ||
    typeof image.name !== "string" ||
    !(version === undefined || typeof version === "string") ||
    typeof document.renewGatewayKeys !== "boolean" ||
    !Object.values(presets).every((preset) => typeof preset === "string")
  ) {
    throw new RecipeWarning(RECIPE_UNREADABLE);
  }
  return {
    name: document.name,
    savedBy: document.savedBy,
    imageName: image.name,
    imageVersion: version,
    project,
    gatewayPresets: presets as Record<string, string>,
    scaling: scalingOf(scaling),
    renewGatewayKeys: document.renewGatewayKeys,
  };
}

/** El `AgentProject` de la receta, leído como un fichero de proyecto y con
 * sus presets de pasarela. Un documento que el lector rechaza es
 * `RecipeWarning("recipe_unreadable")`. */
export function recipeProject(recipe: FleetRecipe): AgentProject {
  try {
    const project = AgentProject.fromObject(recipe.project, { baseDir: RECIPE_PROJECT_BASE_DIR });
    return copyProject(project, { gatewayPresets: recipe.gatewayPresets });
  } catch (error) {
    if (error instanceof InvalidArgumentError) {
      throw new RecipeWarning(RECIPE_UNREADABLE);
    }
    throw error;
  }
}

function scalingOf(scaling: Readonly<Record<string, unknown>>): ScalingOptions {
  const seconds = scaling.scaleInAfterSeconds;
  if (typeof seconds !== "number") {
    throw new RecipeWarning(RECIPE_UNREADABLE);
  }
  try {
    return new ScalingOptions({
      minVms: scaling.minVms as number,
      maxVms: scaling.maxVms as number,
      warmVms: scaling.warmVms as number,
      sessions: scaling.sessions as SessionMode,
      scaleInAfterMs: seconds * MS_PER_SECOND,
    });
  } catch (error) {
    if (error instanceof InvalidArgumentError) {
      throw new RecipeWarning(RECIPE_UNREADABLE);
    }
    throw error;
  }
}

function fits(value: string): boolean {
  return Buffer.byteLength(value, "utf8") <= AGENT_RECIPE_MAX_BYTES;
}

function jsonObject(value: string): Readonly<Record<string, unknown>> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new RecipeWarning(RECIPE_UNREADABLE);
  }
  return record(parsed);
}

function unwrap(envelope: Readonly<Record<string, unknown>>): Readonly<Record<string, unknown>> {
  const data = envelope.data;
  if (
    Object.keys(envelope).sort().join(",") !== ENVELOPE_KEYS.join(",") ||
    envelope.schema !== AGENT_RECIPE_SCHEMA ||
    envelope.encoding !== RECIPE_ENCODING_GZIP ||
    typeof data !== "string" ||
    !BASE64_PATTERN.test(data)
  ) {
    throw new RecipeWarning(RECIPE_UNREADABLE);
  }
  let plain: string;
  try {
    plain = gunzipSync(Buffer.from(data, "base64")).toString("utf8");
  } catch {
    throw new RecipeWarning(RECIPE_UNREADABLE);
  }
  return jsonObject(plain);
}

function record(value: unknown): Readonly<Record<string, unknown>> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new RecipeWarning(RECIPE_UNREADABLE);
  }
  return value as Record<string, unknown>;
}

function sorted(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sorted);
  }
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0));
    return Object.fromEntries(entries.map(([key, item]) => [key, sorted(item)]));
  }
  return value;
}
