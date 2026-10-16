/**
 * `deployAgent()` y `planDeploy()`: un proyecto de agente que ya tienes,
 * desplegado con una llamada y sin tocar su código (ADR-029, D7 y D8). Espejo
 * de `rayito._agent._deploy_facade`.
 *
 * Detectan el proyecto (`AgentProject.detect()`, sin ejecutar su código),
 * resuelven lo que falta de AWS (región, imagen base, secreto del modelo,
 * bucket y access token: `aws-resolve.ts`), construyen la imagen sólo si
 * cambió (el hash del contenido, como `AgentProject.build()`) y arrancan la
 * flota (`AgentFleet.start()`). `planDeploy()` calcula lo mismo sin llamar a
 * AWS, para `rayito agent deploy --dry-run`.
 *
 * Cada paso que cuesta es visible: `AgentDeployOptions.onStep` recibe un
 * `AgentDeployStep` por cada uno, y ninguno crea nada en silencio.
 */

import { InvalidArgumentError } from "../errors.js";
import type { ControlPlaneOptions } from "../sandbox/sandbox.js";
import type { SecretGateway } from "../secret-gateway/domain.js";
import { SecretStore } from "../secrets/store.js";
import {
  AwsResolver,
  type DeployAws,
  discoverBucket,
  type Environ,
  ensureBaseImage,
  ensureModelSecrets,
  resolveBucket,
  resolveToken,
  type SecretStorePort,
} from "./aws-resolve.js";
import {
  AgentDeployOptions,
  type AgentDeployOptionsInput,
  type AgentDeployPlan,
  buildPlan,
  projectBucket,
  projectFor,
  resolvedRegion,
  withDefaultExcludes,
} from "./deploy-plan.js";
import type { DetectOverrides } from "./detect-domain.js";
import { AgentFleet, type AgentFleetOptions } from "./fleet.js";
import { AGENT_FLEET_WARM_POLL_MS } from "./fleet-base.js";
import type { AgentProject } from "./project.js";
import { copyProject } from "./project-copy.js";
import { ProjectService, type ProjectServiceOptions, templateName } from "./project-service.js";

export const STEP_BASE_IMAGE = "base_image";
export const STEP_SECRET_CREATED = "secret_created";
export const STEP_BUCKET = "bucket";
export const STEP_IMAGE = "image";
export const STEP_FLEET = "fleet";
export const STEP_WARM = "warm";

/** Las credenciales de AWS de las llamadas del despliegue, las mismas de
 * `Sandbox.create()`. */
export interface DeployConnection extends ControlPlaneOptions {
  readonly credentials?: ConstructorParameters<typeof AwsResolver>[0]["credentials"];
}

export interface DeployAgentOptions extends DeployConnection {
  readonly project?: AgentProject | undefined;
  readonly options?: AgentDeployOptions | AgentDeployOptionsInput | undefined;
  readonly overrides?: DetectOverrides | undefined;
}

/** Las dependencias de `deployAgent()` que los tests sustituyen: AWS (`aws`,
 * `store`), el entorno y lo que recibe el servicio y la flota. En producción
 * es el valor por defecto y no se pasa. */
export interface DeployPorts {
  readonly aws?: DeployAws | undefined;
  readonly store?: SecretStorePort | undefined;
  readonly environ?: Environ | undefined;
  readonly serviceOptions?: ProjectServiceOptions | undefined;
  readonly fleetOptions?: Partial<AgentFleetOptions> | undefined;
}

/** Lo que se decidió antes de crear nada: el proyecto con los secretos
 * resueltos, el bucket (o `undefined` si no se construye), el token y la
 * región. */
export interface Preflight {
  readonly project: AgentProject;
  readonly bucket: string | undefined;
  readonly token: string;
  readonly region: string | undefined;
  readonly steps: readonly { readonly kind: string; readonly detail: string }[];
}

function optionsOf(
  options: AgentDeployOptions | AgentDeployOptionsInput | undefined,
): AgentDeployOptions {
  return options instanceof AgentDeployOptions ? options : new AgentDeployOptions(options);
}

/**
 * Lo que `deployAgent()` haría con estos argumentos, sin llamar a AWS:
 * detecta el proyecto (lee ficheros locales, no ejecuta su código), y calcula
 * el escalado, el origen de cada valor y el coste de lista.
 *
 * Coste y activación
 * -------------------
 * Activa: llamar a `planDeploy()` (o `rayito agent deploy --dry-run`).
 * Recursos y llamadas AWS: ninguno; no crea clientes de AWS ni carga ningún
 *     paquete opcional.
 * Coste aproximado: ninguno.
 * IAM: ninguna.
 * Cómo apagarla: no la llames.
 * Ejemplo:
 *   const plan = await planDeploy("./mi-agente");
 *   plan.cost.runningUsdPerHour;
 */
export async function planDeploy(
  path = ".",
  args: DeployAgentOptions = {},
): Promise<AgentDeployPlan> {
  const options = optionsOf(args.options);
  const { project, report } = await projectFor(path, args.project, args.overrides);
  return buildPlan(project, options, {
    region: resolvedRegion(args.region, project, process.env),
    detection: report,
  });
}

/**
 * Despliega el agente de `path` y devuelve su `AgentFleet`.
 *
 * `path` es un directorio con el proyecto (con o sin `rayito-agent.toml`): el
 * lenguaje, el framework, el punto de entrada y la instalación se detectan de
 * sus ficheros sin ejecutar su código, y todo lo detectado se puede fijar con
 * `overrides` o con el fichero. Un `project` explícito se usa tal cual.
 * `options` (`AgentDeployOptions`) dice dónde y cómo; `region` y las credenciales
 * son las de `Sandbox.create()`.
 *
 * El orden de cada valor es argumento explícito, fichero de proyecto,
 * variable de entorno y descubrimiento; ninguno crea nada en silencio: Rayito
 * no crea buckets, un secreto sólo se crea con `createSecretFromEnv: true` y
 * una imagen base ausente sólo se publica si pasas `basePublisher`. La imagen
 * del agente se construye sólo si cambió su contenido. El access token de la
 * flota es `options.accessToken`, o `RAYITO_ACCESS_TOKEN`, o uno nuevo en
 * `AgentFleet.accessToken`.
 *
 * Coste y activación
 * -------------------
 * Activa: llamar a `deployAgent()` (o `rayito agent deploy`); nada más lo
 *     crea. `planDeploy()` (`--dry-run`) calcula el coste sin llamar a AWS.
 * Recursos y llamadas AWS: `minVms` MicroVMs (`run-microvm`) y `warmVms`
 *     plazas aparcadas; una versión de imagen nueva si cambió el contenido
 *     (`create`/`update-microvm-image`, un objeto en S3); `GetCallerIdentity`,
 *     `HeadBucket`, `GetMicrovmImage` y `DescribeSecret` de lectura; con
 *     `createSecretFromEnv: true`, `CreateSecret`; con `basePublisher`, una
 *     imagen base; con `sessionIndex`, DynamoDB.
 * Coste aproximado: por VM, el del MicroVM mientras vive (2 GB: ≈ $0,13 por
 *     hora en us-east-1, consultado 2026-10-06 en
 *     https://aws.amazon.com/lambda/pricing/); la imagen nueva, ≈ 3,0 GB de
 *     snapshot (≈ $0,011 de escritura y ≈ $0,056 por semana mínima de
 *     almacenamiento); un secreto, $0,40 al mes
 *     (https://aws.amazon.com/secrets-manager/pricing/, consultado
 *     2026-09-30); el modelo lo cobra su proveedor.
 * IAM: los de `Sandbox.create()`, `Sandbox.list()` y `AgentTemplate.build()`
 *     (`RayitoSandboxRunner`, `RayitoTemplateBuilder`),
 *     `secretsmanager:DescribeSecret` y, con `createSecretFromEnv`,
 *     `secretsmanager:CreateSecret`, `sts:GetCallerIdentity` y `s3:ListBucket`
 *     sobre el bucket del artefacto.
 * Cómo apagarla: `AgentFleet.stop()` termina los VMs; los valores por defecto
 *     de `AgentDeployOptions` no crean ningún secreto, imagen base ni índice; sin
 *     llamar a `deployAgent()` nada se crea.
 * Ejemplo:
 *   const fleet = await deployAgent("./mi-agente", {
 *     options: { bucket: "amzn-s3-demo-bucket" },
 *   });
 *   (await fleet.invoke("Resume el README.")).text;
 *   await fleet.stop();
 */
export async function deployAgent(path = ".", args: DeployAgentOptions = {}): Promise<AgentFleet> {
  return deployWith({}, path, args);
}

/** `deployAgent()` con las dependencias de `ports`. */
export async function deployWith(
  ports: DeployPorts,
  path: string,
  args: DeployAgentOptions = {},
): Promise<AgentFleet> {
  const options = optionsOf(args.options);
  const detected = await projectFor(path, args.project, args.overrides);
  const flight = await preflight(detected.project, options, ports, args);
  emit(options, flight.steps);
  const service = new ProjectService(flight.project, ports.serviceOptions);
  if (flight.bucket !== undefined) {
    const info = await service.build({
      bucket: flight.bucket,
      timeoutMs: options.buildTimeoutMs,
      ...(options.onBuildLogs === undefined ? {} : { onBuildLogs: options.onBuildLogs }),
      ...(flight.region === undefined ? {} : { region: flight.region }),
      ...(args.credentials === undefined ? {} : { credentials: args.credentials }),
      baseVersion: options.baseImageVersion,
    });
    emit(options, [{ kind: STEP_IMAGE, detail: info.buildId.split("/")[0] ?? "" }]);
  }
  const fleet = new AgentFleet({
    project: flight.project,
    accessToken: flight.token,
    scaling: options.scaling,
    sessionIndex: options.sessionIndex,
    poolBackend: options.poolBackend,
    keepOnFailure: options.keepOnFailure,
    connection: connectionOf(args, flight.region),
    service,
    ...ports.fleetOptions,
  });
  await fleet.start();
  emit(options, [{ kind: STEP_FLEET, detail: String(fleet.sandboxIds.length) }]);
  if (fleet.scaling.warmVms > 0) {
    const ready = await fleet.waitWarm(options.warmTimeoutMs, AGENT_FLEET_WARM_POLL_MS);
    emit(options, [{ kind: STEP_WARM, detail: ready ? "ready" : "timeout" }]);
  }
  return fleet;
}

function connectionOf(args: DeployAgentOptions, region: string | undefined): ControlPlaneOptions {
  const { project: _project, options: _options, overrides: _overrides, ...connection } = args;
  return { ...connection, ...(region === undefined ? {} : { region }) };
}

/** Lo que se resuelve antes de crear nada: la imagen base, el secreto del
 * modelo, el bucket y el token. Todo lo que lee AWS ocurre aquí, y lo único
 * que puede crear es un secreto (con `createSecretFromEnv`) o la imagen base
 * (con `basePublisher`). */
export async function preflight(
  project: AgentProject,
  options: AgentDeployOptions,
  ports: DeployPorts,
  args: DeployConnection,
): Promise<Preflight> {
  const environ = ports.environ ?? process.env;
  const region = resolvedRegion(args.region, project, environ);
  const aws =
    ports.aws ??
    new AwsResolver({
      region,
      ...(args.credentials === undefined ? {} : { credentials: args.credentials }),
    });
  const steps: { kind: string; detail: string }[] = [];
  const building = options.buildImage !== false;
  if (
    building &&
    (await ensureBaseImage(aws, project.base, options.basePublisher, {
      ensure: options.ensureBaseImage,
    }))
  ) {
    steps.push({ kind: STEP_BASE_IMAGE, detail: "published" });
  }
  const store =
    ports.store ??
    new SecretStore({
      ...(region === undefined ? {} : { region }),
      ...(args.credentials === undefined ? {} : { credentials: args.credentials }),
    });
  const secrets = await ensureModelSecrets(project, {
    secretNames: options.secretNames,
    createFromEnv: options.createSecretFromEnv,
    store,
    environ,
  });
  for (const gateway of secrets.created) {
    steps.push({ kind: STEP_SECRET_CREATED, detail: gateway });
  }
  const resolved = withDefaultExcludes(
    copyProject(project, { gateways: secrets.gateways as Record<string, SecretGateway> }),
  );
  const bucket = await bucketFor(resolved, options, aws, environ, steps);
  return {
    project: resolved,
    bucket,
    token: resolveToken(options.accessToken, environ),
    region,
    steps,
  };
}

async function bucketFor(
  project: AgentProject,
  options: AgentDeployOptions,
  aws: DeployAws,
  environ: Environ,
  steps: { kind: string; detail: string }[],
): Promise<string | undefined> {
  if (options.buildImage === false) {
    return undefined;
  }
  try {
    const choice = await resolveBucket(options.bucket, projectBucket(project), environ, () =>
      discoverBucket(aws, aws.resolvedRegion()),
    );
    steps.push({ kind: STEP_BUCKET, detail: `${choice.source}:${choice.bucket}` });
    return choice.bucket;
  } catch (error) {
    if (
      error instanceof InvalidArgumentError &&
      options.buildImage === undefined &&
      (await aws.imageExists(templateName(project)))
    ) {
      return undefined;
    }
    throw error;
  }
}

function emit(
  options: AgentDeployOptions,
  steps: readonly { kind: string; detail: string }[],
): void {
  for (const step of steps) {
    options.onStep?.(step);
  }
}
