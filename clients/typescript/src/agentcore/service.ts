/**
 * `AgentCoreDeployer` (ADR-031, D5, D6 y D8): lee la cuenta, decide con
 * `decisions.ts` y lo aplica por los puertos de `ports.ts`. Nada aquí importa
 * un SDK de AWS: construir el servicio no llama a nadie, y sólo `apply()` crea
 * algo.
 *
 * Un despliegue observa y decide en este orden: la cuenta y el bucket, la
 * pila (`CreateStack`/`UpdateStack`), la imagen (`DescribeImages` y, si falta,
 * zip, `StartBuild` y `BatchGetBuilds`) y el runtime (crear y exigir MMDSv2, o
 * actualizar, o reutilizar: `runtime-apply.ts`). Un redespliegue sin cambios no
 * construye ni escribe nada.
 *
 * Coste y activación
 * -------------------
 * Activa: `deployAgent()` con `target: "agentcore"` (o `rayito agent deploy
 *     --target agentcore`); sin ella no se construye este servicio.
 * Recursos y llamadas AWS: la pila `rayito-agentcore-<nombre>`, el zip del
 *     contexto en el bucket de artefactos (se borra al terminar el build), un
 *     build de CodeBuild, una imagen en ECR y el runtime; lee con
 *     `GetCallerIdentity`, `HeadBucket`, `DescribeStacks`, `DescribeImages`,
 *     `ListAgentRuntimes` y `GetAgentRuntime`.
 * Coste aproximado: un build ≈ $0,02-0,03 (arm1.small, $0,0034 por minuto,
 *     100 minutos gratis al mes), la imagen $0,10 por GB-mes y el runtime sólo
 *     mientras hay sesiones (us-east-1, consultado 2026-10-19 en
 *     https://aws.amazon.com/bedrock/agentcore/pricing/).
 * IAM: `cloudformation:CreateStack`/`UpdateStack`/`DescribeStacks` de la pila
 *     y los permisos de IAM que exige `CAPABILITY_IAM`, `s3:PutObject`,
 *     `s3:DeleteObject` y `s3:ListBucket` sobre el prefijo del zip,
 *     `codebuild:StartBuild`/`BatchGetBuilds`, `ecr:DescribeImages`,
 *     `bedrock-agentcore:Create/Update/Get/ListAgentRuntimes` y `iam:PassRole`
 *     del rol del runtime.
 * Cómo apagarla: no pasar `target: "agentcore"`; para quitar lo creado,
 *     `rayito agent delete <nombre> --target agentcore`.
 * Ejemplo: `await new AgentCoreDeployer(ports).apply(request)`.
 */

import { AgentCoreError, StackError } from "../errors.js";
import {
  AGENTCORE_BUILD_LOG_RETENTION_DAYS,
  AGENTCORE_BUILD_POLL_SECONDS,
  AGENTCORE_BUILD_TIMEOUT_MINUTES,
  AGENTCORE_IMAGE_RETENTION_COUNT,
} from "../limits.js";
import { classifyStack, type StackObservation } from "../stacks/condition.js";
import { stackTags } from "../stacks/model.js";
import { loadTemplate } from "../stacks/packaging.js";
import { DEFAULT_WAIT_TIMEOUT_MS } from "../stacks/service.js";
import { formatTemplate } from "../template-text.js";
import { MS_PER_SECOND } from "../units.js";
import { bedrockResourceArns, secretResourceArns } from "./arns.js";
import { Clock, type ServiceSettings } from "./clock.js";
import {
  BUILD_IN_PROGRESS_STATE,
  BUILD_SUCCEEDED_STATE,
  BYTES_PER_MEGABYTE,
  checkImageSize,
  decideImage,
  decideStack,
  requireStackOutputs,
  type StackOutputs,
  stackObservation,
} from "./decisions.js";
import { baseImageReference } from "./identifiers.js";
import type {
  AgentCoreDeployPlan,
  AgentCoreDeployResult,
  AgentCoreStep,
  ImageObservation,
  StackAction,
} from "./models.js";
import { canonicalAgentName, runtimeLogGroupFor, sourceKeyFor } from "./names.js";
import { type AgentCorePorts, MissingResourceError } from "./ports.js";
import { deployEmitter, type Emit, type ProgressStep } from "./progress.js";
import { RuntimeApplier } from "./runtime-apply.js";
import { sourceZip } from "./source-zip.js";
import { AGENTCORE_STACK_COMPONENT, ARN_LIST_SEPARATOR, STACK_PARAMETERS } from "./stack.js";

export interface DeployRequest {
  readonly plan: AgentCoreDeployPlan;
  /** El árbol de `renderExport(project, "agentcore")`, que el zip empaqueta. */
  readonly tree: ReadonlyMap<string, string | Uint8Array>;
  /** La región del modelo del proyecto, que es la de los ARN de Bedrock del rol. */
  readonly modelRegion: string;
  readonly buildImage: boolean | undefined;
  readonly buildTimeoutMs: number;
  readonly onStep?: ((step: ProgressStep) => void) | undefined;
  readonly onBuildLogs?: ((line: string) => void) | undefined;
}

/** La línea de `onBuildLogs` cuando el build cambia de fase de CodeBuild. */
export const BUILD_PHASE_LINE = "build {buildId}: fase {phase}";
/** El estado de un build correcto cuya etiqueta no llegó al repositorio. */
export const BUILD_WITHOUT_IMAGE_STATUS = "SUCCEEDED_WITHOUT_IMAGE";

/** Lo que se hizo con la pila: crearla, actualizarla o, si CloudFormation respondió «No updates are to be performed», reutilizarla. */
type StackWrite = "create" | "update" | "reuse";

/** El estado que cuenta `stack_failed` cuando la pila no tiene el repositorio o el proyecto de CodeBuild que la plantilla crea. */
export const STACK_INCOMPLETE_STATUS = "INCOMPLETE";

function incompleteStack(stackName: string, error: unknown): unknown {
  return error instanceof MissingResourceError
    ? new AgentCoreError("stack_failed", { stackName, status: STACK_INCOMPLETE_STATUS })
    : error;
}

function megabytes(sizeBytes: number | undefined): number {
  return Math.floor((sizeBytes ?? 0) / BYTES_PER_MEGABYTE);
}

export class AgentCoreDeployer {
  readonly #ports: AgentCorePorts;
  readonly #clock: Clock;
  readonly #runtime: RuntimeApplier;

  constructor(ports: AgentCorePorts, settings: ServiceSettings = {}) {
    this.#ports = ports;
    this.#clock = new Clock(settings);
    this.#runtime = new RuntimeApplier(ports.control, this.#clock);
  }

  async apply(request: DeployRequest): Promise<AgentCoreDeployResult> {
    const { plan } = request;
    const emit = deployEmitter(request.onStep);
    const account = await this.#ports.identity.accountId();
    const bucket = await this.#resolveBucket(plan, account);
    await this.#runtime.check(plan);
    const stack = await this.#applyStack(request, bucket, account, emit);
    const image = await this.#applyImage(request, bucket, account, stack, emit).catch(
      (error: unknown) => {
        throw incompleteStack(plan.stackName, error);
      },
    );
    const runtime = await this.#runtime.apply(plan, stack.outputs, emit);
    return {
      name: plan.name,
      region: plan.region,
      runtimeName: plan.runtimeName,
      runtimeId: runtime.details.runtimeId,
      runtimeArn: runtime.details.runtimeArn,
      runtimeVersion: runtime.details.runtimeVersion,
      status: runtime.details.status,
      imageUri: runtime.details.containerUri,
      stackName: plan.stackName,
      logGroup: runtimeLogGroupFor(runtime.details.runtimeId),
      steps: [stack.step, image, runtime.step],
    };
  }

  async #resolveBucket(plan: AgentCoreDeployPlan, owner: string): Promise<string> {
    const bucket = plan.bucket ?? (await this.#ports.buckets.discover(plan.region));
    if (bucket === undefined) {
      throw new AgentCoreError("no_bucket");
    }
    const bucketRegion = await this.#ports.source.bucketRegion(bucket, owner);
    if (bucketRegion === undefined) {
      throw new AgentCoreError("no_bucket");
    }
    if (bucketRegion !== plan.region) {
      throw new AgentCoreError("bucket_region", { bucket, bucketRegion, region: plan.region });
    }
    return bucket;
  }

  async #observeStack(stackName: string): Promise<StackObservation> {
    return stackObservation(stackName, await this.#ports.stacks.describe(stackName));
  }

  async #applyStack(
    request: DeployRequest,
    bucket: string,
    account: string,
    emit: Emit,
  ): Promise<{
    readonly step: AgentCoreStep;
    readonly outputs: StackOutputs;
    readonly created: boolean;
  }> {
    const { plan } = request;
    const observed = await this.#observeStack(plan.stackName);
    const first = decideStack(observed, plan.name);
    if (first === "create" && request.buildImage === false) {
      throw new AgentCoreError("image_missing", {
        imageTag: plan.imageTag,
        repositoryName: plan.repositoryName,
      });
    }
    let written: StackWrite;
    try {
      written = await this.#settleStack(request, { observed, first }, { bucket, account }, emit);
    } catch (error) {
      throw await this.#stackFailure(plan.stackName, error);
    }
    const ready = await this.#observeStack(plan.stackName);
    if (classifyStack(ready) !== "usable") {
      throw new AgentCoreError("stack_failed", {
        stackName: plan.stackName,
        status: ready.status ?? "",
      });
    }
    const outputs = requireStackOutputs(ready);
    emit("stackReady", { stackName: plan.stackName });
    const action = first === "wait" ? "wait" : written;
    return {
      step: {
        resource: "stack",
        name: plan.stackName,
        action,
        status: first === "create" ? null : (observed.status ?? null),
      },
      outputs,
      created: written === "create",
    };
  }

  /** Espera la operación en curso, si la hay, y escribe la pila (crear, actualizar o, si CloudFormation no cambió nada, reutilizar). */
  async #settleStack(
    request: DeployRequest,
    seen: { readonly observed: StackObservation; readonly first: StackAction },
    target: { readonly bucket: string; readonly account: string },
    emit: Emit,
  ): Promise<StackWrite> {
    const { plan } = request;
    let current = seen.observed;
    let action = seen.first;
    while (action === "wait") {
      emit("stackWait", { stackName: plan.stackName, status: current.status ?? "" });
      await this.#waitStack(plan.stackName);
      current = await this.#observeStack(plan.stackName);
      action = decideStack(current, plan.name);
    }
    return this.#writeStack(request, target.bucket, target.account, action, emit);
  }

  async #writeStack(
    request: DeployRequest,
    bucket: string,
    account: string,
    action: "create" | "update",
    emit: Emit,
  ): Promise<StackWrite> {
    const { plan } = request;
    const stacks = this.#ports.stacks;
    const options = {
      stackName: plan.stackName,
      templateBody: await loadTemplate(AGENTCORE_STACK_COMPONENT),
      parameters: this.#stackParameters(request, bucket, account),
      tags: stackTags(AGENTCORE_STACK_COMPONENT, plan.tags),
    };
    emit("stackDeploy", { stackName: plan.stackName });
    if (action === "create") {
      await stacks.create(AGENTCORE_STACK_COMPONENT, options);
    } else if ((await stacks.update(AGENTCORE_STACK_COMPONENT, options)) === "no_changes") {
      emit("stackNoChanges", { stackName: plan.stackName });
      return "reuse";
    }
    await this.#waitStack(plan.stackName);
    return action;
  }

  #stackParameters(
    request: DeployRequest,
    bucket: string,
    account: string,
  ): Record<string, string> {
    const { plan } = request;
    const models = bedrockResourceArns(plan.bedrockModels, {
      modelRegion: request.modelRegion,
      account,
    });
    const secrets = secretResourceArns(plan.readableSecrets, { region: plan.region, account });
    return {
      [STACK_PARAMETERS.agentName]: canonicalAgentName(plan.name),
      [STACK_PARAMETERS.agentDisplayName]: plan.name,
      [STACK_PARAMETERS.runtimeName]: plan.runtimeName,
      [STACK_PARAMETERS.artifactBucket]: bucket,
      [STACK_PARAMETERS.bedrockModelArns]: models.join(ARN_LIST_SEPARATOR),
      [STACK_PARAMETERS.readableSecretArns]: secrets.join(ARN_LIST_SEPARATOR),
      [STACK_PARAMETERS.imageRetentionCount]: String(AGENTCORE_IMAGE_RETENTION_COUNT),
      [STACK_PARAMETERS.buildLogRetentionDays]: String(AGENTCORE_BUILD_LOG_RETENTION_DAYS),
      [STACK_PARAMETERS.buildTimeoutMinutes]: String(AGENTCORE_BUILD_TIMEOUT_MINUTES),
    };
  }

  async #waitStack(stackName: string): Promise<void> {
    await this.#ports.stacks.wait(stackName, "deployed", DEFAULT_WAIT_TIMEOUT_MS);
  }

  async #stackFailure(stackName: string, error: unknown): Promise<unknown> {
    if (!(error instanceof StackError)) {
      return error;
    }
    const observed = await this.#observeStack(stackName).catch(() => undefined);
    return new AgentCoreError(
      "stack_failed",
      { stackName, status: observed?.status ?? "" },
      { cause: error },
    );
  }

  async #applyImage(
    request: DeployRequest,
    bucket: string,
    account: string,
    stack: { readonly outputs: StackOutputs; readonly created: boolean },
    emit: Emit,
  ): Promise<AgentCoreStep> {
    const { plan } = request;
    const { outputs } = stack;
    const observed: ImageObservation | undefined = stack.created
      ? { exists: false, sizeBytes: undefined }
      : await this.#ports.registry.describeImage(outputs.repositoryName, plan.imageTag);
    const action = decideImage({
      image: observed,
      buildImage: request.buildImage,
      imageTag: plan.imageTag,
      repositoryName: plan.repositoryName,
    });
    let sizeBytes = observed?.sizeBytes;
    if (action === "build") {
      sizeBytes = await this.#buildImage(request, bucket, account, outputs, emit);
    } else {
      emit("imageReused", { imageTag: plan.imageTag });
    }
    checkImageSize(sizeBytes);
    return { resource: "image", name: plan.repositoryName, action, status: null };
  }

  async #buildImage(
    request: DeployRequest,
    bucket: string,
    account: string,
    outputs: StackOutputs,
    emit: Emit,
  ): Promise<number | undefined> {
    const { plan } = request;
    const key = sourceKeyFor(plan.name, plan.imageTag);
    const zip = sourceZip(request.tree);
    emit("sourceUpload", { bytes: zip.length });
    await this.#ports.source.putSource(bucket, key, zip, account);
    let buildId = "";
    try {
      ({ buildId } = await this.#ports.builder.startBuild({
        projectName: outputs.buildProjectName,
        sourceLocation: `${bucket}/${key}`,
        imageTag: plan.imageTag,
        baseImage: baseImageReference(),
      }));
      emit("buildStart", { imageTag: plan.imageTag, buildId });
      await this.#waitBuild(request, buildId, outputs.buildLogGroupName);
    } finally {
      await this.#ports.source.deleteSource(bucket, key, account).then(
        () => emit("sourceDeleted"),
        () => undefined,
      );
    }
    const built = await this.#ports.registry.describeImage(outputs.repositoryName, plan.imageTag);
    if (!built.exists) {
      throw new AgentCoreError("image_build_failed", {
        buildId,
        status: BUILD_WITHOUT_IMAGE_STATUS,
        logGroup: outputs.buildLogGroupName,
      });
    }
    emit("imageReady", { imageTag: plan.imageTag, megabytes: megabytes(built.sizeBytes) });
    return built.sizeBytes;
  }

  async #waitBuild(request: DeployRequest, buildId: string, logGroup: string): Promise<void> {
    const started = this.#clock.now();
    let phase: string | undefined;
    for (;;) {
      const build = await this.#ports.builder.getBuild(buildId);
      if (build.phase !== undefined && build.phase !== phase) {
        phase = build.phase;
        request.onBuildLogs?.(formatTemplate(BUILD_PHASE_LINE, { buildId, phase }));
      }
      if (build.status === BUILD_SUCCEEDED_STATE) {
        return;
      }
      if (build.status !== BUILD_IN_PROGRESS_STATE) {
        throw new AgentCoreError("image_build_failed", {
          buildId,
          status: build.status,
          logGroup: build.logGroup ?? logGroup,
        });
      }
      if (this.#clock.now() - started >= request.buildTimeoutMs) {
        throw new AgentCoreError("image_build_timeout", {
          buildId,
          seconds: Math.floor(request.buildTimeoutMs / MS_PER_SECOND),
          logGroup: build.logGroup ?? logGroup,
        });
      }
      await this.#clock.sleep(AGENTCORE_BUILD_POLL_SECONDS * MS_PER_SECOND);
    }
  }
}
