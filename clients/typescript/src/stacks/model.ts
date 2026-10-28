/**
 * Dominio puro del convenio `OptionalStack` (M15 foundations, ADR-016).
 * Espejo de `rayito._stacks._model`. Nada aquí importa un SDK de AWS: las
 * llamadas a CloudFormation viven en `cloudformation.ts`, detrás del puerto
 * `StackProvisioner` (`port.ts`).
 */

import { InvalidArgumentError } from "../errors.js";
import { VERSION } from "../version.js";

export const MANAGED_BY_TAG = "rayito-sdk";
export const COMPONENT_TAG_KEY = "rayito:component";
export const MANAGED_BY_TAG_KEY = "rayito:managed-by";
/** La etiqueta fija con la versión del SDK que desplegó la pila por última vez. */
export const SDK_VERSION_TAG_KEY = "rayito:sdk-version";
/** Las tres etiquetas que `stackTags` pone siempre y que ganan a las del llamante. */
export const FIXED_TAG_KEYS: readonly string[] = [
  COMPONENT_TAG_KEY,
  MANAGED_BY_TAG_KEY,
  SDK_VERSION_TAG_KEY,
];
export const ROLLBACK_COMPLETE = "ROLLBACK_COMPLETE";

/**
 * Los estados terminales en los que un `deploy` fallido tiene un recurso
 * causante que explicar (`failureReason`). Otros fallos terminales
 * (`UPDATE_ROLLBACK_FAILED`, `DELETE_FAILED`) no se explican: el motivo podría
 * venir de una operación anterior. Fuente: los valores de `StackStatus` de
 * https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_Stack.html
 * (consultada 2026-10-27).
 */
export const EXPLAINABLE_ROLLBACK_STATES: ReadonlySet<string> = new Set([
  "ROLLBACK_COMPLETE",
  "ROLLBACK_FAILED",
  "UPDATE_ROLLBACK_COMPLETE",
]);
/**
 * Estados de un recurso que marcan su fallo. Fuente: el `enum`
 * `ResourceStatus` de
 * https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_StackEvent.html
 * (consultada 2026-10-27).
 */
export const FAILED_RESOURCE_STATUSES: ReadonlySet<string> = new Set([
  "CREATE_FAILED",
  "UPDATE_FAILED",
]);
/** Estados del evento de la propia pila con el que empieza una operación (misma fuente). */
export const OPERATION_START_STATUSES: ReadonlySet<string> = new Set([
  "CREATE_IN_PROGRESS",
  "UPDATE_IN_PROGRESS",
]);

export type DeployAction = "create" | "update" | "blocked";

export interface StackParameter {
  readonly name: string;
  readonly description: string;
  readonly required?: boolean;
  readonly default?: string | undefined;
  readonly noEcho?: boolean;
}

/** Un fichero que `deploy()` sube a `artifactBucket` antes de desplegar la
 * plantilla (código Lambda); `parameterKey` es el parámetro de la plantilla
 * al que se pasa la clave S3 resultante (sha256 del contenido).
 * `bucketParameterKey`, si la plantilla también necesita el bucket, es el
 * parámetro al que `deploy()` pasa el mismo `artifactBucket`. */
export interface StackArtifact {
  readonly name: string;
  readonly parameterKey: string;
  readonly bucketParameterKey?: string;
}

/** El bloque "Coste y activación" de cada componente, de forma estructurada. */
export interface CostStatement {
  readonly creates: readonly string[];
  readonly idleMonthly: string;
  readonly perUse?: readonly string[];
  readonly removal?: string;
  readonly source?: string;
}

export interface StackComponent {
  readonly name: string;
  readonly description: string;
  readonly cost: CostStatement;
  readonly parameters?: readonly StackParameter[];
  readonly artifacts?: readonly StackArtifact[];
  readonly capabilities?: readonly string[];
  /** `false` para un componente que `list()` ya describe pero cuya función
   * todavía no tiene plantilla: `deploy`/`status`/`destroy` lanzan
   * `UnimplementedError` antes de tocar AWS. */
  readonly supported?: boolean;
  /** Validación de coherencia entre parámetros que la plantilla no puede
   * expresar (CloudFormation no compara números). Recibe los valores efectivos
   * del despliegue y lanza `InvalidArgumentError`; corre antes de llamar a AWS. */
  readonly validate?: (values: Readonly<Record<string, string>>) => void;
}

export function defaultStackName(component: StackComponent): string {
  return `rayito-${component.name}`;
}

export interface StackStatus {
  readonly name: string;
  readonly state: string | undefined;
  readonly outputs: Readonly<Record<string, string>>;
  readonly reasonCode?: string | undefined;
  /** `Parameters[].{ParameterKey: ParameterValue}` de `DescribeStacks`: los
   * valores con los que la pila está desplegada ahora (CloudFormation ya
   * enmascara los `NoEcho`). `planParameters` lo usa para no pisarlos. */
  readonly parameters?: Readonly<Record<string, string>>;
  /** `Tags[].{Key: Value}` de `DescribeStacks`: las etiquetas de la pila, las
   * fijas de `stackTags` incluidas (`rayito:sdk-version` es la versión del SDK
   * que la desplegó por última vez). */
  readonly tags?: Readonly<Record<string, string>>;
}

export function stackExists(status: StackStatus | undefined): boolean {
  return status !== undefined && status.state !== undefined;
}

export interface DeployPlan {
  readonly action: DeployAction;
  readonly reason?: string;
}

/**
 * `current === undefined` (la pila no existe) -> `"create"`;
 * `ROLLBACK_COMPLETE` -> `"blocked"` (hay que `destroy()` primero);
 * cualquier otro estado existente -> `"update"`. `"no_changes"` no es un
 * resultado de este plan: sólo se sabe tras intentar `UpdateStack`
 * (`StackProvisioner.update()` devuelve `"changed"`/`"no_changes"`).
 */
export function planDeploy(current: StackStatus | undefined): DeployPlan {
  if (!stackExists(current)) {
    return { action: "create" };
  }
  if (current?.state === ROLLBACK_COMPLETE) {
    return {
      action: "blocked",
      reason:
        `la pila ${JSON.stringify(current.name)} está en ROLLBACK_COMPLETE: bórrala ` +
        "(destroy) antes de volver a desplegarla",
    };
  }
  return { action: "update" };
}

/** Un parámetro cuyo valor cambiaría con el `deploy()`; `before` es
 * `undefined` si la pila no existe o todavía no lo tenía. */
export interface ParameterChange {
  readonly name: string;
  readonly before: string | undefined;
  readonly after: string;
}

/** Qué parámetros manda `deploy()`: `values` con `ParameterValue` y
 * `keepPrevious` con `UsePreviousValue: true` (sólo en `UpdateStack`: los
 * que el llamante no pasó y la pila ya tiene). */
export interface ParameterPlan {
  readonly values: Readonly<Record<string, string>>;
  readonly keepPrevious: readonly string[];
}

export function rejectUnknownParameters(
  component: StackComponent,
  given: Readonly<Record<string, string>>,
): void {
  const known = new Set((component.parameters ?? []).map((parameter) => parameter.name));
  const unknown = Object.keys(given).filter((key) => !known.has(key));
  if (unknown.length > 0) {
    throw new InvalidArgumentError(
      `parámetros desconocidos para ${component.name}: ${unknown.sort().join(", ")}`,
    );
  }
}

/**
 * Los valores por defecto del catálogo sólo se aplican al crear. Al
 * actualizar una pila existente, un parámetro que el llamante no pasó y la
 * pila ya tiene se conserva (`UsePreviousValue`) en vez de volver a su valor
 * por defecto: redesplegar sin repetir cada parámetro no puede ensanchar una
 * política (`s3-mounts` `Prefixes`), reemplazar una tabla (`metadata-index`
 * `TableName`) ni quitar un permiso (`secrets-access` `KmsKeyArn`). Un
 * parámetro nuevo de la plantilla que la pila aún no tiene recibe su valor
 * por defecto también al actualizar. Espejo de `plan_parameters`.
 */
export function planParameters(
  component: StackComponent,
  given: Readonly<Record<string, string>>,
  current: StackStatus | undefined,
): ParameterPlan {
  rejectUnknownParameters(component, given);
  const previous = stackExists(current) ? (current?.parameters ?? {}) : undefined;
  const values: Record<string, string> = { ...given };
  const keepPrevious: string[] = [];
  const missing: string[] = [];
  for (const parameter of component.parameters ?? []) {
    if (Object.hasOwn(given, parameter.name)) {
      continue;
    }
    if (previous !== undefined && Object.hasOwn(previous, parameter.name)) {
      keepPrevious.push(parameter.name);
    } else if (parameter.default !== undefined) {
      values[parameter.name] = parameter.default;
    } else if (parameter.required === true) {
      missing.push(parameter.name);
    }
  }
  if (missing.length > 0) {
    throw new InvalidArgumentError(
      `faltan parámetros obligatorios para ${component.name}: ${missing.sort().join(", ")}`,
    );
  }
  if (component.validate !== undefined) {
    const retained = Object.fromEntries(keepPrevious.map((name) => [name, previous?.[name] ?? ""]));
    component.validate({ ...retained, ...values });
  }
  return { values, keepPrevious: keepPrevious.sort() };
}

/** Los de `plan.values` que difieren de los actuales; los de
 * `keepPrevious` nunca cambian. Espejo de `ParameterPlan.changes`. */
export function parameterChanges(
  plan: ParameterPlan,
  current: StackStatus | undefined,
): ParameterChange[] {
  const before = current?.parameters ?? {};
  return Object.entries(plan.values)
    .sort(([a], [b]) => a.localeCompare(b))
    .filter(([name, value]) => before[name] !== value)
    .map(([name, after]) => ({ name, before: before[name], after }));
}

/** Las tres etiquetas fijas siempre ganan sobre las del llamante. */
export function stackTags(
  component: StackComponent,
  userTags: Readonly<Record<string, string>>,
): Record<string, string> {
  return {
    ...userTags,
    [COMPONENT_TAG_KEY]: component.name,
    [MANAGED_BY_TAG_KEY]: MANAGED_BY_TAG,
    [SDK_VERSION_TAG_KEY]: VERSION,
  };
}

/**
 * Un evento de `DescribeStackEvents` (`AWS_API_NOTES.md` §21): sólo los tres
 * campos que usa `firstFailedResource`. Espejo de `StackEvent`.
 */
export interface StackEvent {
  readonly logicalResourceId: string | undefined;
  readonly resourceStatus: string | undefined;
  readonly resourceStatusReason: string | undefined;
}

/**
 * El recurso que causó el fallo de la última operación de la pila. `events`
 * viene del más nuevo al más antiguo, el orden de la API. Se recorre hasta el
 * evento de inicio de operación de la propia pila (`logicalResourceId ===
 * stackName`), para no mezclar el fallo de un despliegue anterior. De los
 * recursos con estado fallido se devuelve el más antiguo: los posteriores
 * suelen ser cancelaciones por esa causa.
 */
export function firstFailedResource(
  events: readonly StackEvent[],
  stackName: string,
): StackEvent | undefined {
  let earliest: StackEvent | undefined;
  for (const event of events) {
    const status = event.resourceStatus ?? "";
    if (event.logicalResourceId === stackName) {
      if (OPERATION_START_STATUSES.has(status)) {
        break;
      }
      continue;
    }
    if (FAILED_RESOURCE_STATUSES.has(status)) {
      earliest = event;
    }
  }
  return earliest;
}

/** `<LogicalResourceId> (<ResourceStatus>): <ResourceStatusReason>`. */
export function describeFailedResource(event: StackEvent): string {
  return `${event.logicalResourceId ?? ""} (${event.resourceStatus ?? ""}): ${event.resourceStatusReason ?? ""}`;
}
