/**
 * Lo que `deployAgent()` y `rayito agent deploy` hacen con `secretEnv` además
 * de pasarlo al runtime (ADR-033 D4). Espejo de
 * `rayito._agent._secret_env_deploy` y `_secret_env_reader`: crear o
 * actualizar, con `createSecretsFrom` / `--create-secrets-from`, un secreto
 * JSON con los valores de un `.env` y usarlo como `secretEnvFrom`; avisar
 * (`checkEnv` / `--check-env`, o siempre que el proyecto use `secretEnv`) de
 * las variables de `.env.example` que no reciben valor; y leer los secretos en
 * MicroVMs con las credenciales de quien despliega.
 *
 * Crear el secreto es la única operación con coste y la única que lee los
 * valores de un `.env`; es explícita y nunca ocurre en la detección
 * (ADR-029). Ningún aviso, paso ni error nombra un valor.
 */

import { readFileSync } from "node:fs";
import { InvalidArgumentError } from "../errors.js";
import { defineHidden } from "../hidden.js";
import { AGENT_SECRET_NAME_TEMPLATE } from "../limits.js";
import type { SecretStoreOptions } from "../secrets/store.js";
import { GATEWAY_SECRET_ENV_VARS } from "./aws-resolve.js";
import { envMissingWarnings, missingEnvNames, requiredEnvNames } from "./detect-env-names.js";
import { LocalProjectTree } from "./detect-fs.js";
import type { AgentProject } from "./project.js";
import { copyProject } from "./project-copy.js";
import {
  isReservedName,
  parseDotenv,
  SecretEnvSource,
  type SecretReader,
  secretEnvNames,
  unlistedPrefixes,
  usesSecretEnv,
} from "./secret-env.js";

/** El hueco de `AGENT_SECRET_NAME_TEMPLATE` del secreto de un `.env`: el
 * secreto queda en `rayito/agents/<proyecto>/env`. */
export const ENV_FILE_SECRET_SLOT = "env";
/** El paso de `onStep` al crear o actualizar ese secreto; `detail` es
 * `created` o `updated`, nunca el nombre ni un valor. */
export const STEP_SECRET_ENV_CREATED = "secret_env_created";
/** El paso de `onStep` de cada aviso `env_missing`; `detail` es el aviso (sólo
 * el nombre de la variable). */
export const STEP_ENV_MISSING = "env_missing";
export const SECRET_CREATED = "created";
export const SECRET_UPDATED = "updated";
/** Los nombres de `secretEnv` son completos: sin el prefijo `rayito/`. */
const FULL_NAME_PREFIX = "";

/** Lo que el despliegue usa de `new SecretStore({ prefix: "" })`. */
export interface EnvSecretStore {
  exists(name: string): Promise<boolean>;
  create(name: string, value: string): Promise<unknown>;
  update(name: string, value: string): Promise<unknown>;
}

/** El secreto JSON de un `.env`: su nombre, los nombres de sus variables y su
 * valor, que no es enumerable (ni `console.log` ni `JSON.stringify` lo ven). */
export interface EnvFileSecret {
  readonly name: string;
  readonly variables: readonly string[];
  readonly value: string;
}

export function envFileSecretName(project: string): string {
  return AGENT_SECRET_NAME_TEMPLATE.replace("{project}", project).replace(
    "{provider}",
    ENV_FILE_SECRET_SLOT,
  );
}

/** Lee el `.env` de `path` (`testdata/agent-secret-env/dotenv.json`). Un
 * fichero que no existe o sin variables es `InvalidArgumentError`, sin
 * nombrar ningún valor. */
export function readEnvFileSecret(project: string, path: string): EnvFileSecret {
  let text: string;
  try {
    text = readFileSync(path, "utf8");
  } catch {
    throw new InvalidArgumentError("createSecretsFrom: no se pudo leer el fichero");
  }
  const variables = parseDotenv(text);
  const names = Object.keys(variables).sort();
  if (names.length === 0) {
    throw new InvalidArgumentError("createSecretsFrom: el fichero no tiene variables");
  }
  const sorted = Object.fromEntries(names.map((name) => [name, variables[name]]));
  const secret = { name: envFileSecretName(project), variables: Object.freeze(names) };
  return Object.freeze(defineHidden(secret, "value", JSON.stringify(sorted)));
}

/** El proyecto que además lee ese secreto como `secretEnvFrom`, con sus
 * variables como lista `keys` (así `checkEnv` las da por cubiertas). */
export function withEnvFileSource(project: AgentProject, secret: EnvFileSecret): AgentProject {
  const source = new SecretEnvSource({ secret: secret.name, keys: secret.variables });
  if (project.secretEnvFrom.some((existing) => existing.equals(source))) {
    return project;
  }
  return copyProject(project, { secretEnvFrom: [...project.secretEnvFrom, source] });
}

/** Crea el secreto o, si ya existe, escribe una versión nueva. */
export async function publishEnvFileSecret(
  store: EnvSecretStore,
  secret: EnvFileSecret,
): Promise<typeof SECRET_CREATED | typeof SECRET_UPDATED> {
  if (await store.exists(secret.name)) {
    await store.update(secret.name, secret.value);
    return SECRET_UPDATED;
  }
  await store.create(secret.name, secret.value);
  return SECRET_CREATED;
}

/** Lo que recibe valor sin leer ningún secreto: `env`, `secretEnv`, las
 * listas `keys` de `secretEnvFrom` y las variables de las pasarelas. */
export function coveredEnvNames(project: AgentProject): ReadonlySet<string> {
  return new Set([
    ...Object.keys(project.env),
    ...secretEnvNames(project.secretEnv, project.secretEnvFrom),
    ...Object.values(GATEWAY_SECRET_ENV_VARS),
  ]);
}

/** Los avisos `env_missing` del proyecto: sólo con `checkEnv` o si usa
 * `secretEnv`, y sólo si tiene código. Los nombres reservados (región y
 * credenciales de AWS) los pone la plataforma. */
export function envMissingOf(project: AgentProject, checkEnv: boolean): readonly string[] {
  if (!checkEnv && !usesSecretEnv(project.secretEnv, project.secretEnvFrom)) {
    return [];
  }
  if (project.source === undefined) {
    return [];
  }
  const required = requiredEnvNames(new LocalProjectTree(project.source)).filter(
    (name) => !isReservedName(name),
  );
  const missing = missingEnvNames(
    required,
    coveredEnvNames(project),
    unlistedPrefixes(project.secretEnvFrom),
  );
  return envMissingWarnings(missing);
}

/** El proyecto con el secreto de `createSecretsFrom` ya conectado (aún sin
 * crear), ese secreto y los avisos de las variables sin valor. */
export interface PreparedSecretEnv {
  readonly project: AgentProject;
  readonly secret: EnvFileSecret | undefined;
  readonly warnings: readonly string[];
}

export function prepareSecretEnv(
  project: AgentProject,
  createSecretsFrom: string | undefined,
  checkEnv: boolean,
): PreparedSecretEnv {
  const secret =
    createSecretsFrom === undefined
      ? undefined
      : readEnvFileSecret(project.name, createSecretsFrom);
  const prepared = secret === undefined ? project : withEnvFileSource(project, secret);
  return { project: prepared, secret, warnings: envMissingOf(prepared, checkEnv) };
}

export interface SecretClientOptions {
  readonly region?: string | undefined;
  readonly credentials?: SecretStoreOptions["credentials"];
}

/** `SecretStore` con nombres completos; no llama a AWS al construirse. */
export async function envSecretStore(options: SecretClientOptions): Promise<EnvSecretStore> {
  const { SecretStore } = await import("../secrets/store.js");
  return new SecretStore({ ...definedOptions(options), prefix: FULL_NAME_PREFIX });
}

/** Lee un secreto de `secretEnv` (nombre completo o ARN, y etiqueta de
 * versión) una vez por despliegue con `SecretCache`, con las credenciales de
 * quien despliega. El cliente nace en la primera lectura. */
export function secretReaderFor(options: SecretClientOptions = {}): SecretReader {
  let cache: Promise<{ get(ref: unknown): Promise<string> }> | undefined;
  return async (secret, versionStage) => {
    cache ??= import("../secrets/cache.js").then(
      ({ SecretCache }) =>
        new SecretCache({ ...definedOptions(options), prefix: FULL_NAME_PREFIX }),
    );
    const { SecretRef } = await import("../secrets/names.js");
    const ref = new SecretRef(secret, versionStage === undefined ? {} : { versionStage });
    return (await cache).get(ref);
  };
}

function definedOptions(options: SecretClientOptions): SecretClientOptions {
  return {
    ...(options.region === undefined ? {} : { region: options.region }),
    ...(options.credentials === undefined ? {} : { credentials: options.credentials }),
  };
}
