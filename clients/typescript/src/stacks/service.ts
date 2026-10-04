/**
 * `OptionalStacks` (M15 foundations, ADR-016): `deploy`/`status`/`destroy`/
 * `components` sobre el catálogo de `registry.ts`, a través del puerto
 * `StackProvisioner`. Nunca se invoca implícitamente: ningún `create()`,
 * listado o getter del SDK lo llama; sólo una llamada explícita de este
 * servicio (o la CLI) despliega, cambia o borra algo. Espejo de
 * `rayito._stacks._service` (TS: una sola clase async, sin par síncrono).
 */

import type { AwsClientSettings } from "../aws/control-plane.js";
import { InvalidArgumentError, StackError, UnimplementedError } from "../errors.js";
import { CloudFormationProvisioner } from "./cloudformation.js";
import type { ParameterChange, ParameterPlan, StackComponent, StackStatus } from "./model.js";
import {
  parameterChanges,
  planDeploy,
  planParameters,
  rejectUnknownParameters,
  stackTags,
} from "./model.js";
import { artifactKey, loadArtifact, loadTemplate } from "./packaging.js";
import type { StackProvisioner } from "./port.js";
import { COMPONENTS, componentByName } from "./registry.js";

/** Una pila sin recursos anidados termina en segundos a minutos; diez
 * minutos deja margen generoso sin bloquear para siempre una pila atascada. */
export const DEFAULT_WAIT_TIMEOUT_MS = 600_000;

type Credentials = AwsClientSettings["credentials"];

export interface OptionalStacksOptions {
  readonly region?: string | undefined;
  readonly credentials?: Credentials;
  readonly provisioner?: StackProvisioner;
}

export interface DeployOptions {
  readonly stackName?: string;
  readonly parameters?: Readonly<Record<string, string>>;
  readonly artifactBucket?: string;
  readonly tags?: Readonly<Record<string, string>>;
  readonly wait?: boolean;
  readonly waitTimeoutMs?: number;
}

/** Los argumentos de `deploy()` que deciden los parámetros. */
export type ParameterChangesOptions = Pick<
  DeployOptions,
  "stackName" | "parameters" | "artifactBucket"
>;

export interface DestroyOptions {
  readonly stackName?: string;
  readonly wait?: boolean;
  readonly waitTimeoutMs?: number;
}

function resolveComponent(component: string | StackComponent): StackComponent {
  if (typeof component !== "string") {
    return component;
  }
  const resolved = componentByName(component);
  if (resolved === undefined) {
    const names = COMPONENTS.map((candidate) => candidate.name)
      .sort()
      .join(", ");
    throw new InvalidArgumentError(
      `componente desconocido: ${JSON.stringify(component)} (disponibles: ${names})`,
    );
  }
  return resolved;
}

function requireSupported(component: StackComponent): void {
  if (component.supported === false) {
    throw new UnimplementedError(
      `rayito stack deploy ${component.name}`,
      `${component.name} todavía no tiene plantilla: ${component.description}`,
    );
  }
}

/**
 * `given` más, para cada artefacto con `bucketParameterKey`, ese parámetro
 * con `artifactBucket` (el bucket al que `deploy()` sube el código). Uno ya
 * pasado con otro valor es `InvalidArgumentError`: la plantilla apuntaría a
 * un bucket donde el código no está. Espejo de `_with_artifact_bucket`.
 */
function withArtifactBucket(
  component: StackComponent,
  given: Readonly<Record<string, string>>,
  artifactBucket: string | undefined,
): Record<string, string> {
  const resolved: Record<string, string> = { ...given };
  if (!artifactBucket) {
    return resolved;
  }
  for (const artifact of component.artifacts ?? []) {
    const key = artifact.bucketParameterKey;
    if (key === undefined) {
      continue;
    }
    if ((resolved[key] ?? artifactBucket) !== artifactBucket) {
      throw new InvalidArgumentError(
        `${component.name}: el parámetro ${key} debe ser el mismo bucket que artifactBucket ` +
          "(es donde se sube el código); omítelo",
      );
    }
    resolved[key] = artifactBucket;
  }
  return resolved;
}

function requireArtifactBucket(
  component: StackComponent,
  artifactBucket: string | undefined,
): void {
  if ((component.artifacts?.length ?? 0) > 0 && !artifactBucket) {
    throw new InvalidArgumentError(
      `${component.name} necesita artifactBucket: sube el código Lambda del componente`,
    );
  }
}

interface Plan {
  readonly component: StackComponent;
  readonly stackName: string;
  readonly current: StackStatus | undefined;
  readonly parameters: ParameterPlan;
}

export class OptionalStacks {
  readonly #provisioner: StackProvisioner;

  constructor(options: OptionalStacksOptions = {}) {
    this.#provisioner =
      options.provisioner ??
      new CloudFormationProvisioner({ region: options.region, credentials: options.credentials });
  }

  /** Metadata pura del catálogo; nunca llama a AWS. */
  components(): readonly StackComponent[] {
    return COMPONENTS;
  }

  async status(
    component: string | StackComponent,
    options: { readonly stackName?: string } = {},
  ): Promise<StackStatus | undefined> {
    const resolved = resolveComponent(component);
    const name = options.stackName ?? `rayito-${resolved.name}`;
    return this.#provisioner.describe(name);
  }

  /**
   * Qué parámetros cambiaría `deploy()` con estos mismos argumentos, sin
   * desplegar nada (sólo un `DescribeStacks`). Los que no se pasan y la pila
   * ya tiene no aparecen: `deploy()` los conserva. Espejo de
   * `OptionalStacks.parameter_changes`.
   */
  async parameterChanges(
    component: string | StackComponent,
    options: ParameterChangesOptions = {},
  ): Promise<ParameterChange[]> {
    const plan = await this.#plan(component, options);
    return parameterChanges(plan.parameters, plan.current);
  }

  /**
   * Crea la pila o, si ya existe, la actualiza. Al crear, los parámetros no
   * pasados toman su valor por defecto del catálogo; al actualizar, los no
   * pasados conservan el valor con el que la pila está desplegada
   * (`UsePreviousValue`), así redesplegar sin repetir cada parámetro no
   * deshace la configuración anterior.
   */
  async deploy(
    component: string | StackComponent,
    options: DeployOptions = {},
  ): Promise<StackStatus> {
    const plan = await this.#plan(component, options);
    const { component: resolved, stackName: name } = plan;
    const parameters: Record<string, string> = { ...plan.parameters.values };
    if ((resolved.artifacts?.length ?? 0) > 0 && options.artifactBucket) {
      const data = await loadArtifact(resolved);
      const key = await artifactKey(resolved, data);
      await this.#provisioner.putArtifact(options.artifactBucket, key, data);
      for (const artifact of resolved.artifacts ?? []) {
        parameters[artifact.parameterKey] = key;
      }
    }
    const templateBody = await loadTemplate(resolved);
    const tags = stackTags(resolved, options.tags ?? {});
    if (planDeploy(plan.current).action === "create") {
      await this.#provisioner.create(resolved, { stackName: name, templateBody, parameters, tags });
    } else {
      await this.#provisioner.update(resolved, {
        stackName: name,
        templateBody,
        parameters,
        tags,
        keepPrevious: plan.parameters.keepPrevious,
      });
    }
    if (options.wait !== false) {
      await this.#provisioner.wait(
        name,
        "deployed",
        options.waitTimeoutMs ?? DEFAULT_WAIT_TIMEOUT_MS,
      );
    }
    const status = await this.#provisioner.describe(name);
    if (status === undefined) {
      throw new StackError(`la pila ${JSON.stringify(name)} desapareció justo tras desplegarla`, {
        code: "failed",
      });
    }
    return status;
  }

  /** Lo común a `deploy()` y `parameterChanges()`: valida todo lo que no
   * necesita AWS antes de la única llamada (`DescribeStacks`) y decide los
   * parámetros según exista o no la pila. */
  async #plan(component: string | StackComponent, options: ParameterChangesOptions): Promise<Plan> {
    const resolved = resolveComponent(component);
    requireSupported(resolved);
    const name = options.stackName ?? `rayito-${resolved.name}`;
    requireArtifactBucket(resolved, options.artifactBucket);
    const given = withArtifactBucket(resolved, options.parameters ?? {}, options.artifactBucket);
    rejectUnknownParameters(resolved, given);
    const current = await this.#provisioner.describe(name);
    const deployPlan = planDeploy(current);
    if (deployPlan.action === "blocked") {
      throw new StackError(deployPlan.reason ?? `deploy de ${JSON.stringify(name)} bloqueado`, {
        code: "blocked",
      });
    }
    return {
      component: resolved,
      stackName: name,
      current,
      parameters: planParameters(resolved, given, current),
    };
  }

  async destroy(component: string | StackComponent, options: DestroyOptions = {}): Promise<void> {
    const resolved = resolveComponent(component);
    requireSupported(resolved);
    const name = options.stackName ?? `rayito-${resolved.name}`;
    await this.#provisioner.delete(name);
    if (options.wait !== false) {
      await this.#provisioner.wait(
        name,
        "deleted",
        options.waitTimeoutMs ?? DEFAULT_WAIT_TIMEOUT_MS,
      );
    }
  }
}
