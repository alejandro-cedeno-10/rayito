/**
 * `secretEnv` y `secretEnvFrom` sobre un `AgentProject` (ADR-033 D4). Espejo
 * de `rayito._agent._secret_env_project`: las banderas `--secret-env
 * NOMBRE=<id>[#clave]` y `--secret-env-from <id>[,prefix=P][,keys=A|B]`,
 * leídas con las mismas reglas que el fichero de proyecto
 * (`testdata/agent-secret-env/parse.json`), y la comprobación de que ninguna
 * variable choca con las que el runtime ya fija. Un error es
 * `AgentProjectError` con `code` y `path`; su mensaje nunca repite el valor de
 * la bandera, que podría ser un secreto mal pegado.
 */

import { InvalidArgumentError } from "../errors.js";
import {
  type AgentProject,
  AgentProjectError,
  checkSecretEnvName,
  checkSecretEnvSource,
  checkSecretId,
  secretEnvFromPath,
  secretEnvPath,
} from "./project.js";
import { copyProject } from "./project-copy.js";
import {
  SECRET_ENV_KEY_SEPARATOR,
  SECRET_ENV_NAME_PATTERN,
  type SecretEnvMap,
  SecretEnvRef,
  SecretEnvSource,
  type SecretEnvValue,
  secretEnvNames,
} from "./secret-env.js";

const NAME_SEPARATOR = "=";
const OPTION_SEPARATOR = ",";
const KEYS_SEPARATOR = "|";
const PREFIX_OPTION = "prefix";
const KEYS_OPTION = "keys";

function invalid(path: string, detail: string): AgentProjectError {
  return new AgentProjectError(`${path}: ${detail}`, { path, code: "invalid_value" });
}

function partition(value: string, separator: string): readonly [string, boolean, string] {
  const index = value.indexOf(separator);
  return index < 0
    ? [value, false, ""]
    : [value.slice(0, index), true, value.slice(index + separator.length)];
}

/** `NOMBRE=<id>[#clave]` -> el nombre y su secreto (un `SecretEnvRef` si lleva
 * clave). Sin `=`, o con un nombre que no es de variable, lo escrito podría ser
 * un secreto mal pegado: el error va a la tabla, sin nombre. */
export function parseSecretEnvFlag(value: string): readonly [string, SecretEnvValue] {
  const [name, hasSeparator, reference] = partition(value, NAME_SEPARATOR);
  if (!hasSeparator) {
    throw invalid(secretEnvPath(""), "se esperaba NOMBRE=<secreto>[#clave]");
  }
  const path = secretEnvPath(SECRET_ENV_NAME_PATTERN.test(name) ? name : "");
  checkSecretEnvName(name, path);
  const [secret, hasKey, key] = partition(reference, SECRET_ENV_KEY_SEPARATOR);
  if (hasKey && key === "") {
    throw invalid(path, "falta la clave JSON tras '#'");
  }
  checkSecretId(secret, path);
  return [name, key === "" ? secret : new SecretEnvRef({ secret, key })];
}

function sourceOptions(
  path: string,
  options: readonly string[],
): { prefix: string; keys: readonly string[] | undefined } {
  let prefix = "";
  let keys: readonly string[] | undefined;
  for (const option of options) {
    const [optionName, hasValue, optionValue] = partition(option, NAME_SEPARATOR);
    if (hasValue && optionName === PREFIX_OPTION) {
      prefix = optionValue;
    } else if (hasValue && optionName === KEYS_OPTION && optionValue !== "") {
      keys = optionValue.split(KEYS_SEPARATOR);
    } else {
      throw invalid(path, "opción desconocida; admitidas: prefix=, keys=");
    }
  }
  return { prefix, keys };
}

/** `<id>[,prefix=P][,keys=A|B]` -> un `SecretEnvSource`; `index` es su
 * posición entre las `--secret-env-from` (la ruta del error). */
export function parseSecretEnvFromFlag(value: string, index: number): SecretEnvSource {
  const path = secretEnvFromPath(index);
  const [secret = "", ...options] = value.split(OPTION_SEPARATOR);
  const { prefix, keys } = sourceOptions(path, options);
  checkSecretId(secret, `${path}.secret`);
  let source: SecretEnvSource;
  try {
    source = new SecretEnvSource({ secret, prefix, keys });
  } catch (error) {
    if (error instanceof InvalidArgumentError) {
      throw invalid(path, error.message);
    }
    throw error;
  }
  return checkSecretEnvSource(source, path);
}

/** Las `--secret-env` repetidas; un nombre repetido gana el último. */
export function parseSecretEnvFlags(values: readonly string[]): Record<string, SecretEnvValue> {
  return Object.fromEntries(values.map(parseSecretEnvFlag));
}

export function parseSecretEnvFromFlags(values: readonly string[]): readonly SecretEnvSource[] {
  return values.map((value, index) => parseSecretEnvFromFlag(value, index));
}

/** El proyecto con las banderas ya leídas sumadas a su `secretEnv` (la
 * bandera gana) y a su `secretEnvFrom` (detrás de las del fichero). */
export function withSecretEnv(
  project: AgentProject,
  secretEnv: SecretEnvMap,
  secretEnvFrom: readonly SecretEnvSource[],
): AgentProject {
  if (Object.keys(secretEnv).length === 0 && secretEnvFrom.length === 0) {
    return project;
  }
  return copyProject(project, {
    secretEnv: { ...project.secretEnv, ...secretEnv },
    secretEnvFrom: [...project.secretEnvFrom, ...secretEnvFrom],
  });
}

/** Una variable de `secretEnv` (o una clave de una lista `keys`) que el
 * runtime ya fija en su entorno es `invalid_value`: el host la rechazaría al
 * arrancar. */
export function checkRuntimeCollisions(
  project: AgentProject,
  envs: Readonly<Record<string, string>>,
): void {
  for (const name of secretEnvNames(project.secretEnv, project.secretEnvFrom)) {
    if (Object.hasOwn(envs, name)) {
      throw invalid(secretEnvPath(name), "el runtime ya fija esa variable");
    }
  }
}
