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
import type { StackComponent, StackStatus } from "./model.js";
import { planDeploy, stackTags } from "./model.js";
import { loadArtifact, loadTemplate } from "./packaging.js";
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

function resolvedParameters(
  component: StackComponent,
  given: Readonly<Record<string, string>>,
): Record<string, string> {
  const known = new Set((component.parameters ?? []).map((parameter) => parameter.name));
  const unknown = Object.keys(given).filter((key) => !known.has(key));
  if (unknown.length > 0) {
    throw new InvalidArgumentError(
      `parámetros desconocidos para ${component.name}: ${unknown.sort().join(", ")}`,
    );
  }
  const resolved: Record<string, string> = {};
  for (const parameter of component.parameters ?? []) {
    if (parameter.default !== undefined) {
      resolved[parameter.name] = parameter.default;
    }
  }
  Object.assign(resolved, given);
  const missing = (component.parameters ?? [])
    .filter((parameter) => parameter.required === true && resolved[parameter.name] === undefined)
    .map((parameter) => parameter.name);
  if (missing.length > 0) {
    throw new InvalidArgumentError(
      `faltan parámetros obligatorios para ${component.name}: ${missing.sort().join(", ")}`,
    );
  }
  return resolved;
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

  async deploy(
    component: string | StackComponent,
    options: DeployOptions = {},
  ): Promise<StackStatus> {
    const resolved = resolveComponent(component);
    requireSupported(resolved);
    const name = options.stackName ?? `rayito-${resolved.name}`;
    const hasArtifacts = (resolved.artifacts?.length ?? 0) > 0;
    if (hasArtifacts && !options.artifactBucket) {
      throw new InvalidArgumentError(
        `${resolved.name} necesita artifactBucket: sube el código Lambda del componente`,
      );
    }
    const parameters = resolvedParameters(
      resolved,
      withArtifactBucket(resolved, options.parameters ?? {}, options.artifactBucket),
    );
    if (hasArtifacts && options.artifactBucket) {
      const data = await loadArtifact(resolved);
      const key = await artifactKey(data);
      await this.#provisioner.putArtifact(options.artifactBucket, key, data);
      for (const artifact of resolved.artifacts ?? []) {
        parameters[artifact.parameterKey] = key;
      }
    }
    const templateBody = await loadTemplate(resolved);
    const tags = stackTags(resolved, options.tags ?? {});
    const plan = planDeploy(await this.#provisioner.describe(name));
    if (plan.action === "blocked") {
      throw new StackError(plan.reason ?? `deploy de ${JSON.stringify(name)} bloqueado`, {
        code: "blocked",
      });
    }
    if (plan.action === "create") {
      await this.#provisioner.create(resolved, { stackName: name, templateBody, parameters, tags });
    } else {
      await this.#provisioner.update(resolved, { stackName: name, templateBody, parameters, tags });
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

async function artifactKey(data: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
