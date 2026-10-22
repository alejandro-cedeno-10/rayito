/**
 * El adaptador de Parameter Store de la receta de la flota (ADR-034 D1-D3,
 * `AWS_API_NOTES.md` «Receta de la flota y claves de corta duración de
 * Bedrock»). Espejo de `rayito._agent._fleet_recipe_aws`.
 *
 * Sólo tres operaciones y sólo estos parámetros: `PutParameter` (`Name`,
 * `Value`, `Type: "SecureString"`, `Tier: "Standard"`, `Overwrite: true`,
 * `Description`), `GetParameter` (`Name`, `WithDecryption: true`) y
 * `DeleteParameter` (`Name`). Nunca `KeyId`, `Tags`, `Policies` ni
 * `AllowedPattern`. `@aws-sdk/client-ssm` es un peer opcional que se carga en
 * la primera llamada. Los errores se mapean por código.
 */

import type { AwsClientSettings } from "../aws/control-plane.js";
import { awsCode, LazyAwsApi, loadOptionalSdkClient } from "../aws/optional-client.js";
import { sanitizeAwsError } from "../aws/sanitize.js";
import { SandboxError } from "../errors.js";
import { AGENT_RECIPE_DESCRIPTION } from "../limits.js";
import type { Logger } from "../logger.js";
import {
  decodeRecipe,
  encodeRecipe,
  type FleetRecipe,
  RECIPE_ACCESS_DENIED,
  RECIPE_NOT_SAVED,
  RECIPE_TOO_LARGE,
  RecipeWarning,
  type RecipeWarningCode,
  recipeDocument,
  recipeParameterName,
  recipeProject,
} from "./fleet-recipe.js";
import type { AgentProject } from "./project.js";

/** El peer opcional del cliente de Parameter Store. */
export const SSM_PEER = "@aws-sdk/client-ssm";
/** `Type` y `Tier` del parámetro: cifrado con `aws/ssm`, nivel gratuito. */
export const RECIPE_PARAMETER_TYPE = "SecureString";
export const RECIPE_PARAMETER_TIER = "Standard";
/** El detalle del paso `recipe` cuando la receta se guardó. */
export const RECIPE_SAVED = "saved";
const PARAMETER_NOT_FOUND = "ParameterNotFound";
const ACCESS_DENIED = "AccessDeniedException";
/** Lo que impide guardar sin que el despliegue falle (AWS_API_NOTES.md,
 * tabla de errores de la receta). */
const NOT_SAVED_CODES: ReadonlySet<string> = new Set([
  "ParameterLimitExceeded",
  "ParameterPatternMismatchException",
  "HierarchyLevelLimitExceededException",
  "TooManyUpdates",
  "ThrottlingException",
]);
const SSM_IAM_ACTIONS = {
  putParameter: "ssm:PutParameter",
  getParameter: "ssm:GetParameter",
  deleteParameter: "ssm:DeleteParameter",
} as const;
type SsmOperation = keyof typeof SSM_IAM_ACTIONS;

/** Dónde vive la receta de cada flota (el puerto de la flota y del
 * despliegue). `parameter` es la ruta de `recipeParameterName`. */
export interface FleetRecipeStore {
  save(parameter: string, value: string): Promise<void>;
  load(parameter: string): Promise<string | undefined>;
  delete(parameter: string): Promise<void>;
}

/** Las tres operaciones de Parameter Store, con la forma del SDK v3. */
export interface SsmApi {
  putParameter(input: {
    Name: string;
    Value: string;
    Type: "SecureString";
    Tier: "Standard";
    Overwrite: boolean;
    Description: string;
  }): Promise<unknown>;
  getParameter(input: {
    Name: string;
    WithDecryption: boolean;
  }): Promise<{ Parameter?: { Value?: string | undefined } | undefined }>;
  deleteParameter(input: { Name: string }): Promise<unknown>;
}

interface SsmModule {
  readonly SSMClient: new (config: object) => { send(command: unknown): Promise<unknown> };
  readonly PutParameterCommand: new (input: object) => unknown;
  readonly GetParameterCommand: new (input: object) => unknown;
  readonly DeleteParameterCommand: new (input: object) => unknown;
}

type Credentials = AwsClientSettings["credentials"];

async function sdkApi(region: string, credentials: Credentials): Promise<SsmApi> {
  const { sdk, send } = await loadOptionalSdkClient<SsmModule>(
    SSM_PEER,
    "la receta de la flota (Parameter Store)",
    (module) => module.SSMClient,
    region,
    credentials,
  );
  return {
    putParameter: (input) => send(new sdk.PutParameterCommand(input)),
    getParameter: (input) => send(new sdk.GetParameterCommand(input)),
    deleteParameter: (input) => send(new sdk.DeleteParameterCommand(input)),
  };
}

export interface SsmRecipeStoreOptions {
  readonly region?: string | undefined;
  readonly credentials?: Credentials | undefined;
  /** Un cliente propio con la forma de `SsmApi`, p. ej. en tests. */
  readonly client?: SsmApi | undefined;
}

class NotFound extends Error {}

/**
 * `FleetRecipeStore` sobre Parameter Store.
 *
 * Coste y activación
 * -------------------
 * Activa: `deployAgent()` con `saveRecipe: true` (por defecto) guarda;
 *     `AgentFleet.connect(nombre)` sin `project` lee; `stop()` borra la que
 *     leyó o guardó. Construirlo no llama a AWS ni carga el peer.
 * Recursos y llamadas AWS: un parámetro `SecureString` estándar por flota
 *     (`PutParameter`, `GetParameter` con `WithDecryption`, `DeleteParameter`)
 *     y una petición de KMS (`aws/ssm`) por escritura y por lectura.
 * Coste aproximado: los parámetros estándar y sus llamadas no tienen cargo
 *     (https://aws.amazon.com/systems-manager/pricing/); KMS, 20 000
 *     peticiones gratis al mes y después $0,03 por 10 000
 *     (https://aws.amazon.com/kms/pricing/), consultado 2026-10-22.
 * IAM: `ssm:PutParameter`, `ssm:GetParameter` y `ssm:DeleteParameter` sobre
 *     `parameter/rayito/agents/*` (sentencia `AgentRecipe` de
 *     `infra/iam.yaml`); ningún permiso de KMS con `aws/ssm`.
 * Cómo apagarla: `saveRecipe: false` (`--no-save-recipe`) no guarda;
 *     `AgentFleet.connect(nombre, { useRecipe: false })` o `project` no lee.
 * Ejemplo:
 *   const store = new SsmRecipeStore({ region: "us-east-1" });
 *   await store.load("/rayito/agents/my-agent/recipe");
 */
export class SsmRecipeStore implements FleetRecipeStore {
  readonly #api: LazyAwsApi<SsmApi>;

  constructor(options: SsmRecipeStoreOptions = {}) {
    const credentials = options.credentials;
    this.#api = new LazyAwsApi(
      options.region,
      "falta la región: pasa `region` o define AWS_REGION",
      (region) => sdkApi(region, credentials),
      options.client,
    );
  }

  /** `PutParameter` con sobrescritura. Sin permiso: `recipe_access_denied`;
   * límite, nombre o tasa: `recipe_not_saved`. */
  async save(parameter: string, value: string): Promise<void> {
    await this.#call("putParameter", (api) =>
      api.putParameter({
        Name: parameter,
        Value: value,
        Type: RECIPE_PARAMETER_TYPE,
        Tier: RECIPE_PARAMETER_TIER,
        Overwrite: true,
        Description: AGENT_RECIPE_DESCRIPTION,
      }),
    );
  }

  /** El valor descifrado, o `undefined` si la flota no tiene receta. */
  async load(parameter: string): Promise<string | undefined> {
    try {
      const response = await this.#call("getParameter", (api) =>
        api.getParameter({ Name: parameter, WithDecryption: true }),
      );
      const value = response.Parameter?.Value;
      return typeof value === "string" ? value : undefined;
    } catch (error) {
      if (error instanceof NotFound) {
        return undefined;
      }
      throw error;
    }
  }

  /** `DeleteParameter`; borrar una receta que no existe no es un error. */
  async delete(parameter: string): Promise<void> {
    try {
      await this.#call("deleteParameter", (api) => api.deleteParameter({ Name: parameter }));
    } catch (error) {
      if (!(error instanceof NotFound)) {
        throw error;
      }
    }
  }

  async #call<T>(operation: SsmOperation, run: (api: SsmApi) => Promise<T>): Promise<T> {
    const api = await this.#api.get();
    try {
      return await run(api);
    } catch (error) {
      throw translateRecipeError(operation, error);
    }
  }
}

/** El error de AWS mapeado por código (nunca por el mensaje): sin receta,
 * aviso (sin permiso o sin poder guardar) o `SandboxError` sin el mensaje de
 * AWS. */
export function translateRecipeError(operation: SsmOperation, error: unknown): Error {
  const code = awsCode(error);
  if (code === PARAMETER_NOT_FOUND) {
    return new NotFound();
  }
  if (code === ACCESS_DENIED) {
    return new RecipeWarning(RECIPE_ACCESS_DENIED);
  }
  if (operation === "putParameter" && code !== undefined && NOT_SAVED_CODES.has(code)) {
    return new RecipeWarning(RECIPE_NOT_SAVED);
  }
  return new SandboxError(
    `Parameter Store falló en ${SSM_IAM_ACTIONS[operation]} (${code ?? "error"})`,
    { awsCode: code, cause: sanitizeAwsError(error, { includeMessage: false }) },
  );
}

/** Escribe el aviso de la receta en el log (sólo el código) y lo devuelve. */
export function warnRecipe(logger: Logger | undefined, code: RecipeWarningCode): string {
  logger?.warn?.("receta de la flota", { code });
  return code;
}

/** Guarda la receta y devuelve `saved` o el código del aviso: nunca falla.
 * Una receta que no cabe borra la anterior (`recipe_too_large`). */
export async function storeRecipe(
  store: FleetRecipeStore,
  recipe: FleetRecipe,
  logger?: Logger | undefined,
): Promise<string> {
  const parameter = recipeParameterName(recipe.name);
  if (parameter === undefined) {
    return warnRecipe(logger, RECIPE_NOT_SAVED);
  }
  const value = encodeRecipe(recipeDocument(recipe));
  try {
    if (value === undefined) {
      await store.delete(parameter);
      return warnRecipe(logger, RECIPE_TOO_LARGE);
    }
    await store.save(parameter, value);
  } catch (error) {
    if (value === undefined) {
      return warnRecipe(logger, RECIPE_TOO_LARGE);
    }
    if (error instanceof RecipeWarning) {
      return warnRecipe(logger, error.code);
    }
    if (error instanceof SandboxError) {
      return warnRecipe(logger, RECIPE_NOT_SAVED);
    }
    throw error;
  }
  return RECIPE_SAVED;
}

/** La receta de la flota `name` y su proyecto, o `undefined` si no tiene, no
 * hay permiso (`recipe_access_denied`) o no se puede leer
 * (`recipe_unreadable`). Cualquier otro error de AWS se propaga. */
export async function loadRecipe(
  store: FleetRecipeStore,
  name: string,
  logger?: Logger | undefined,
): Promise<{ readonly recipe: FleetRecipe; readonly project: AgentProject } | undefined> {
  const parameter = recipeParameterName(name);
  if (parameter === undefined) {
    return undefined;
  }
  try {
    const value = await store.load(parameter);
    if (value === undefined) {
      return undefined;
    }
    const recipe = decodeRecipe(value);
    return { recipe, project: recipeProject(recipe) };
  } catch (error) {
    if (error instanceof RecipeWarning) {
      warnRecipe(logger, error.code);
      return undefined;
    }
    throw error;
  }
}

/** Borra la receta de la flota `name`. Sin permiso, sólo un aviso. */
export async function forgetRecipe(
  store: FleetRecipeStore,
  name: string,
  logger?: Logger | undefined,
): Promise<void> {
  const parameter = recipeParameterName(name);
  if (parameter === undefined) {
    return;
  }
  try {
    await store.delete(parameter);
  } catch (error) {
    if (error instanceof RecipeWarning) {
      warnRecipe(logger, error.code);
      return;
    }
    throw error;
  }
}

/** La tienda de recetas de producción para la región y las credenciales de la
 * flota (sin llamar a AWS ni cargar el peer). */
export function defaultRecipeStore(options: SsmRecipeStoreOptions = {}): FleetRecipeStore {
  return new SsmRecipeStore(options);
}
