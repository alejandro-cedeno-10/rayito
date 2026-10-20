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

import { rejectAgentCoreOptions } from "../agentcore/domain.js";
import type { AgentCoreFleet } from "../agentcore/fleet.js";
import {
  AGENT_TARGETS,
  type AgentCoreDeployPlan,
  type AgentCoreOptions,
  type AgentTarget,
  TARGET_AGENTCORE,
} from "../agentcore/models.js";
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
  type AgentDeployOptions,
  type AgentDeployOptionsInput,
  type AgentDeployPlan,
  baseVersionOf,
  buildPlan,
  deployOptionsOf,
  projectBucket,
  projectFor,
  resolvedRegion,
  withDefaultExcludes,
} from "./deploy-plan.js";
import type { DetectOverrides } from "./detect-report.js";
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

/**
 * Los argumentos de `deployAgent()` y `planDeploy()`. `target` (por defecto
 * `"microvm"`) elige dónde se despliega: una flota de MicroVMs o, con
 * `"agentcore"`, Amazon Bedrock AgentCore Runtime; `agentcore` (`AgentCoreOptions`)
 * sólo vale con ese destino y con otro es `option_requires_agentcore`. El tipo
 * del resultado sigue al de `target`.
 */
export interface DeployAgentOptions<T extends AgentTarget = "microvm"> extends DeployConnection {
  readonly project?: AgentProject | undefined;
  readonly options?: AgentDeployOptions | AgentDeployOptionsInput | undefined;
  readonly overrides?: DetectOverrides | undefined;
  readonly target?: T | undefined;
  readonly agentcore?: AgentCoreOptions | undefined;
}

/** El resultado de `deployAgent()` según el destino. */
export type DeployedAgent<T extends AgentTarget> = T extends "agentcore"
  ? AgentCoreFleet
  : AgentFleet;

/** El resultado de `planDeploy()` según el destino. */
export type DeployPlanOf<T extends AgentTarget> = T extends "agentcore"
  ? AgentCoreDeployPlan
  : AgentDeployPlan;

function requireKnownTarget(target: string | undefined): void {
  if (target !== undefined && !(AGENT_TARGETS as readonly string[]).includes(target)) {
    throw new InvalidArgumentError(
      `target debe ser uno de ${AGENT_TARGETS.map((name) => JSON.stringify(name)).join(", ")}`,
    );
  }
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
export async function planDeploy<T extends AgentTarget = "microvm">(
  path = ".",
  args: DeployAgentOptions<T> = {},
): Promise<DeployPlanOf<T>> {
  requireKnownTarget(args.target);
  if (args.target === TARGET_AGENTCORE) {
    const { planAgentCoreWith } = await import("../agentcore/facade.js");
    const planned = await planAgentCoreWith({}, path, args as DeployAgentOptions<"agentcore">);
    return planned.plan as DeployPlanOf<T>;
  }
  rejectAgentCoreOptions(args.agentcore);
  const options = deployOptionsOf(args.options);
  const { project, report } = await projectFor(path, args.project, args.overrides, args.region);
  return buildPlan(project, options, {
    region: resolvedRegion(args.region, project, process.env),
    detection: report,
  }) as DeployPlanOf<T>;
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
 * Con `target: "agentcore"` despliega en Amazon Bedrock AgentCore Runtime y
 * devuelve un `AgentCoreFleet` (la misma API de `invoke()` y `stream()`): sin
 * Docker local, construye la imagen arm64 en CodeBuild, la sube a ECR, crea el
 * rol del runtime con el mínimo privilegio y crea o actualiza el runtime sin
 * duplicarlo. Las opciones de MicroVMs que AgentCore no admite fallan con
 * `option_not_supported` en vez de aproximarse, y AgentCore no tiene la
 * pasarela de secretos de Rayito: el agente lee las credenciales del rol del
 * runtime, así que el rol sólo lleva los modelos de `agentcore.bedrockModels`.
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
 *     imagen base; con `sessionIndex`, DynamoDB. Con `target: "agentcore"`: la
 *     pila `rayito-agentcore-<nombre>` (repositorio ECR, proyecto de CodeBuild
 *     y dos roles), un zip del contexto en el bucket (se borra al terminar el
 *     build), un build de CodeBuild, una imagen en ECR y el runtime.
 * Coste aproximado: por VM, el del MicroVM mientras vive (2 GB: ≈ $0,13 por
 *     hora en us-east-1, consultado 2026-10-06 en
 *     https://aws.amazon.com/lambda/pricing/); la imagen nueva, ≈ 3,0 GB de
 *     snapshot (≈ $0,011 de escritura y ≈ $0,056 por semana mínima de
 *     almacenamiento); un secreto, $0,40 al mes
 *     (https://aws.amazon.com/secrets-manager/pricing/, consultado
 *     2026-09-30); el modelo lo cobra su proveedor. En AgentCore (us-east-1,
 *     plataforma V1, consultado 2026-10-19 en
 *     https://aws.amazon.com/bedrock/agentcore/pricing/): $0,0895 por
 *     vCPU-hora y $0,00945 por GB-hora de uso real y sin cómputo sin sesiones;
 *     un build ≈ $0,02-0,03 en CodeBuild (https://aws.amazon.com/codebuild/pricing/)
 *     y la imagen $0,10 por GB-mes.
 * IAM: los de `Sandbox.create()`, `Sandbox.list()` y `AgentTemplate.build()`
 *     (`RayitoSandboxRunner`, `RayitoTemplateBuilder`),
 *     `secretsmanager:DescribeSecret` y, con `createSecretFromEnv`,
 *     `secretsmanager:CreateSecret`, `sts:GetCallerIdentity` y `s3:ListBucket`
 *     sobre el bucket del artefacto. Con `target: "agentcore"`: los de
 *     `AgentCoreDeployer` (CloudFormation con `CAPABILITY_IAM`, CodeBuild, ECR,
 *     S3 sobre el prefijo del zip, `bedrock-agentcore` y `iam:PassRole`).
 * Cómo apagarla: `AgentFleet.stop()` termina los VMs y
 *     `AgentCoreFleet.delete()` borra lo que creó en AgentCore; los valores por
 *     defecto de `AgentDeployOptions` no crean ningún secreto, imagen base ni
 *     índice; sin llamar a `deployAgent()` (o con el destino por defecto) no se
 *     crea nada de AgentCore.
 * Ejemplo:
 *   const fleet = await deployAgent("./mi-agente", {
 *     options: { bucket: "amzn-s3-demo-bucket" },
 *   });
 *   (await fleet.invoke("Resume el README.")).text;
 *   await fleet.stop();
 */
export async function deployAgent<T extends AgentTarget = "microvm">(
  path = ".",
  args: DeployAgentOptions<T> = {},
): Promise<DeployedAgent<T>> {
  requireKnownTarget(args.target);
  if (args.target === TARGET_AGENTCORE) {
    const { deployAgentCoreWith } = await import("../agentcore/facade.js");
    return (await deployAgentCoreWith(
      {},
      path,
      args as DeployAgentOptions<"agentcore">,
    )) as DeployedAgent<T>;
  }
  return (await deployWith({}, path, args as DeployAgentOptions)) as DeployedAgent<T>;
}

/** `deployAgent()` con las dependencias de `ports`. */
export async function deployWith(
  ports: DeployPorts,
  path: string,
  args: DeployAgentOptions = {},
): Promise<AgentFleet> {
  rejectAgentCoreOptions(args.agentcore);
  const options = deployOptionsOf(args.options);
  const detected = await projectFor(path, args.project, args.overrides, args.region);
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
      baseVersion: baseVersionOf(options, flight.project),
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
  const {
    project: _project,
    options: _options,
    overrides: _overrides,
    target: _target,
    agentcore: _agentcore,
    ...connection
  } = args;
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
