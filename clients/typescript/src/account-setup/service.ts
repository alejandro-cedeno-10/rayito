/**
 * `AccountSetup` (ADR-030, D6): lee la cuenta, decide el plan puro de
 * `domain.ts` y lo aplica por puertos. Espejo de
 * `rayito._account_setup._service`. Es privado: nada lo exporta desde
 * `index.ts`, y sólo `rayito setup` lo invoca.
 *
 * Coste y activación
 *
 * Activa: `rayito setup` (sin `--dry-run` y con `--yes` o la confirmación de
 * una terminal); construir `AccountSetup` y sus adaptadores no llama a AWS ni
 * carga ningún cliente, y `plan()` sólo lee.
 *
 * Recursos y llamadas AWS: la pila de IAM `rayito-m0-iam` (`CreateStack` o
 * `UpdateStack`), el zip de la imagen en el bucket (`PutObject`) y una versión
 * de `rayito-base` (`CreateMicrovmImage` o `UpdateMicrovmImage`); sólo si no
 * hay ningún bucket que reutilizar, también la pila `rayito-m0-artifacts`
 * (`CreateStack`, la plantilla `infra/artifact-bucket.yaml`) que crea el
 * bucket: el SDK nunca llama a `CreateBucket` por sí mismo. Lee con
 * `GetCallerIdentity`, `DescribeStacks`, `HeadBucket` (hasta tres, y un
 * `DescribeStacks` más de `rayito-m0-artifacts`, sólo en una cuenta sin bucket
 * utilizable), `GetMicrovmImage` y `ListMicrovmImageVersions`.
 *
 * Coste aproximado: el bucket creado y su pila son gratis salvo los zips, que
 * caducan a los 7 días (céntimos); la pila de IAM es gratis; cada versión publicada de la
 * imagen ≈ $0,04 por semana de almacenamiento, con mínimo de una semana
 * (`cost.md`, "Mantener publicada una versión de imagen"); el zip, céntimos.
 *
 * IAM: `cloudformation:CreateStack`/`UpdateStack`/`DescribeStacks` y los
 * permisos de IAM que exige `CAPABILITY_NAMED_IAM` para crear roles y
 * políticas, `s3:ListBucket`/`PutObject`/`GetObject` sobre `rayito/images/*` y
 * `lambda:CreateMicrovmImage`/`UpdateMicrovmImage` (`CallerPolicy` de
 * `infra/iam.yaml`). Crear el bucket exige además permisos de S3 de nivel de
 * bootstrap (`s3:CreateBucket`, `s3:PutBucketPolicy`,
 * `s3:PutEncryptionConfiguration`, `s3:PutLifecycleConfiguration`, entre
 * otros): `CallerPolicy` no los da, así que ese paso pide credenciales de
 * administrador o de bootstrap.
 *
 * Cómo apagarla: no ejecutes `rayito setup` (o usa `--dry-run`); para borrar lo
 * creado, `aws cloudformation delete-stack` (el bucket de
 * `rayito-m0-artifacts` se conserva) y `rayito image prune`.
 *
 * Ejemplo: `await new AccountSetup(gateways).plan(request)` devuelve el plan sin
 * escribir nada.
 */

import {
  AccountSetupError,
  ARTIFACT_STACK_NAME,
  ARTIFACT_TEMPLATE_PATH,
  BASE_IMAGE_NAME,
  BUCKET_NAME_OUTPUT_KEY,
  BUILD_ROLE_OUTPUT_KEY,
  bucketCandidates,
  creationTarget,
  decideBucket,
  formatTemplate,
  IAM_STACK_NAME,
  IAM_TEMPLATE_PATH,
  IMAGE_ASSET_NAME,
  needsDefaultProbe,
  PROGRESS_MESSAGES,
  planBucketCreation,
  planSetup,
  requireSupportedRegion,
  type SetupPlan,
  type SetupRequest,
  type SetupResult,
  type SetupStep,
  type StackObservation,
  stackOutput,
  stepOf,
  verifyChecksum,
} from "./domain.js";
import type { AccountSetupGateways, Progress, StackTags } from "./ports.js";

export interface ApplyOptions {
  /** Directorio temporal propio de la invocación, donde se descarga la release. */
  readonly workdir: string;
  readonly onProgress: Progress;
  /** Etiquetas de `--tag` para las pilas que `apply` crea o actualiza; sin ellas, ninguna. */
  readonly tags?: StackTags;
}

export class AccountSetup {
  readonly #gateways: AccountSetupGateways;

  constructor(gateways: AccountSetupGateways) {
    this.#gateways = gateways;
  }

  /** Valida la región antes de cualquier llamada y observa la cuenta sólo con lecturas. */
  async plan(request: SetupRequest): Promise<SetupPlan> {
    requireSupportedRegion(request.region);
    const { iamStack, buckets, images } = this.#gateways;
    const account = await buckets.accountId();
    const stack = await iamStack.observe();
    const bucketStep = await this.#resolveBucket(request, stack, account);
    const image = await images.observe(BASE_IMAGE_NAME);
    return planSetup({ request, account, stack, bucketStep, image });
  }

  /** Aplica el plan: el bucket (si falta), luego la pila de IAM y por último la imagen. */
  async apply(plan: SetupPlan, options: ApplyOptions): Promise<SetupResult> {
    const tags = options.tags ?? {};
    const bucket = await this.#applyBucket(plan, options.onProgress, tags);
    const stack = await this.#applyStack(plan, bucket, options.onProgress, tags);
    return this.#applyImage(plan, bucket, stack, options);
  }

  /**
   * Fase de reutilización (cada candidato se sondea una vez hasta que uno
   * decide) y, sólo si nada se reutilizó, fase de creación.
   */
  async #resolveBucket(
    request: SetupRequest,
    stack: StackObservation,
    account: string,
  ): Promise<SetupStep> {
    const { artifactStack, buckets } = this.#gateways;
    for (const candidate of bucketCandidates(request, stack, account)) {
      const step = decideBucket(candidate, await buckets.probe(candidate.name), request.region);
      if (step !== undefined) {
        return step;
      }
    }
    const target = creationTarget(request, account);
    const observed = await artifactStack.observe();
    const probe = needsDefaultProbe(target, observed)
      ? await buckets.probe(target.name)
      : undefined;
    return planBucketCreation(target, observed, probe);
  }

  /** Crea o espera la pila del bucket y devuelve su `BucketName`; un bucket reutilizado no se escribe. */
  async #applyBucket(plan: SetupPlan, onProgress: Progress, tags: StackTags): Promise<string> {
    const step = stepOf(plan, "bucket");
    const { artifactStack } = this.#gateways;
    const stack = { name: ARTIFACT_STACK_NAME };
    let observed: StackObservation;
    if (step.action === "create") {
      onProgress(formatTemplate(PROGRESS_MESSAGES.bucketCreate, { ...stack, bucket: step.name }));
      observed = await artifactStack.create(step.name, tags);
    } else if (step.action === "wait") {
      onProgress(
        formatTemplate(PROGRESS_MESSAGES.bucketWait, { ...stack, status: step.status ?? "" }),
      );
      observed = await artifactStack.wait();
    } else {
      return step.name;
    }
    const bucket = stackOutput(observed, BUCKET_NAME_OUTPUT_KEY, ARTIFACT_TEMPLATE_PATH);
    onProgress(formatTemplate(PROGRESS_MESSAGES.bucketReady, { ...stack, bucket }));
    return bucket;
  }

  async #applyStack(
    plan: SetupPlan,
    bucket: string,
    onProgress: Progress,
    tags: StackTags,
  ): Promise<StackObservation | undefined> {
    const step = stepOf(plan, "iamStack");
    const { iamStack } = this.#gateways;
    const name = { name: IAM_STACK_NAME };
    let observed: StackObservation;
    if (step.action === "create") {
      onProgress(formatTemplate(PROGRESS_MESSAGES.stackCreate, name));
      observed = await iamStack.create(bucket, tags);
    } else if (step.action === "update") {
      onProgress(formatTemplate(PROGRESS_MESSAGES.stackUpdate, name));
      observed = await iamStack.updateBucket(bucket, tags);
    } else if (step.action === "wait") {
      onProgress(
        formatTemplate(PROGRESS_MESSAGES.stackWait, { ...name, status: step.status ?? "" }),
      );
      observed = await iamStack.wait();
    } else {
      return undefined;
    }
    requireBuildRole(observed);
    onProgress(formatTemplate(PROGRESS_MESSAGES.stackReady, name));
    return observed;
  }

  async #applyImage(
    plan: SetupPlan,
    bucket: string,
    stack: StackObservation | undefined,
    options: ApplyOptions,
  ): Promise<SetupResult> {
    const step = stepOf(plan, "baseImage");
    const { onProgress } = options;
    if (step.action === "reuse") {
      return {
        bucket,
        imageArn: plan.imageArn,
        imageVersion: step.imageVersion ?? "",
        artifactUploaded: undefined,
      };
    }
    if (step.action === "wait") {
      const imageVersion = await this.#waitForBuild(plan, step.imageVersion ?? "", onProgress);
      return { bucket, imageArn: plan.imageArn, imageVersion, artifactUploaded: undefined };
    }
    const buildRoleArn = requireBuildRole(stack ?? (await this.#gateways.iamStack.observe()));
    const artifact = await this.#artifactToPublish(plan, options);
    const published = await this.#gateways.publisher.publish(artifact, {
      bucket,
      buildRoleArn,
      onProgress,
    });
    onProgress(
      formatTemplate(PROGRESS_MESSAGES.imageReady, {
        name: BASE_IMAGE_NAME,
        imageVersion: published.imageVersion,
      }),
    );
    return {
      bucket,
      imageArn: published.imageArn,
      imageVersion: published.imageVersion,
      artifactUploaded: published.artifactUploaded,
    };
  }

  async #waitForBuild(
    plan: SetupPlan,
    imageVersion: string,
    onProgress: Progress,
  ): Promise<string> {
    onProgress(
      formatTemplate(PROGRESS_MESSAGES.imageWait, { name: BASE_IMAGE_NAME, imageVersion }),
    );
    const gate = await this.#gateways.images.wait(plan.imageArn, imageVersion, onProgress);
    if (!gate.launchable) {
      throw new AccountSetupError("image_build_failed", {
        name: BASE_IMAGE_NAME,
        imageVersion,
        state: gate.versionState,
      });
    }
    onProgress(
      formatTemplate(PROGRESS_MESSAGES.imageReady, { name: BASE_IMAGE_NAME, imageVersion }),
    );
    return imageVersion;
  }

  /** `--artifact` se publica tal cual; sin él se descarga la release y se comprueba su sha256. */
  async #artifactToPublish(plan: SetupPlan, options: ApplyOptions): Promise<string> {
    if (plan.artifact !== undefined) {
      return plan.artifact;
    }
    const tag = stepOf(plan, "baseImage").release;
    if (tag === undefined) {
      throw new Error("el plan de publicación no nombra la release ni el artefacto");
    }
    const downloaded = await this.#gateways.releases.fetch(
      tag,
      options.workdir,
      options.onProgress,
    );
    verifyChecksum(downloaded, tag);
    options.onProgress(
      formatTemplate(PROGRESS_MESSAGES.verified, {
        asset: IMAGE_ASSET_NAME,
        sha256: downloaded.artifactSha256,
      }),
    );
    return downloaded.artifact;
  }
}

/** El rol de build de la pila; sin la salida no es la plantilla de Rayito. */
function requireBuildRole(stack: StackObservation): string {
  return stackOutput(stack, BUILD_ROLE_OUTPUT_KEY, IAM_TEMPLATE_PATH);
}
