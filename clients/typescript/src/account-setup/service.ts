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
 * de `rayito-base` (`CreateMicrovmImage` o `UpdateMicrovmImage`); lee con
 * `GetCallerIdentity`, `DescribeStacks`, `HeadBucket`, `GetMicrovmImage` y
 * `ListMicrovmImageVersions`. Nunca crea un bucket.
 *
 * Coste aproximado: la pila de IAM es gratis; cada versión publicada de la
 * imagen ≈ $0,04 por semana de almacenamiento, con mínimo de una semana
 * (`cost.md`, "Mantener publicada una versión de imagen"); el zip, céntimos.
 *
 * IAM: `cloudformation:CreateStack`/`UpdateStack`/`DescribeStacks` y los
 * permisos de IAM que exige `CAPABILITY_NAMED_IAM` para crear roles y
 * políticas, `s3:ListBucket`/`PutObject`/`GetObject` sobre `rayito/images/*` y
 * `lambda:CreateMicrovmImage`/`UpdateMicrovmImage` (`CallerPolicy` de
 * `infra/iam.yaml`).
 *
 * Cómo apagarla: no ejecutes `rayito setup` (o usa `--dry-run`); para borrar lo
 * creado, `aws cloudformation delete-stack` y `rayito image prune`.
 *
 * Ejemplo: `await new AccountSetup(gateways).plan(request)` devuelve el plan sin
 * escribir nada.
 */

import {
  AccountSetupError,
  BASE_IMAGE_NAME,
  BUILD_ROLE_OUTPUT_KEY,
  chooseBucketCandidate,
  formatTemplate,
  IAM_STACK_NAME,
  IMAGE_ASSET_NAME,
  PROGRESS_MESSAGES,
  planSetup,
  requireSupportedRegion,
  type SetupPlan,
  type SetupRequest,
  type SetupResult,
  type StackObservation,
  stepOf,
  verifyChecksum,
} from "./domain.js";
import type { AccountSetupGateways, Progress } from "./ports.js";

export interface ApplyOptions {
  /** Directorio temporal propio de la invocación, donde se descarga la release. */
  readonly workdir: string;
  readonly onProgress: Progress;
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
    const stack = await iamStack.observe();
    const account = await buckets.accountId();
    const candidate = chooseBucketCandidate(request, stack, account);
    const probe = await buckets.probe(candidate.name);
    const image = await images.observe(BASE_IMAGE_NAME);
    return planSetup({ request, account, stack, candidate, probe, image });
  }

  /** Aplica el plan: el bucket no se escribe, luego la pila de IAM y por último la imagen. */
  async apply(plan: SetupPlan, options: ApplyOptions): Promise<SetupResult> {
    const bucket = stepOf(plan, "bucket").name;
    const stack = await this.#applyStack(plan, bucket, options.onProgress);
    return this.#applyImage(plan, bucket, stack, options);
  }

  async #applyStack(
    plan: SetupPlan,
    bucket: string,
    onProgress: Progress,
  ): Promise<StackObservation | undefined> {
    const step = stepOf(plan, "iamStack");
    const { iamStack } = this.#gateways;
    const name = { name: IAM_STACK_NAME };
    let observed: StackObservation;
    if (step.action === "create") {
      onProgress(formatTemplate(PROGRESS_MESSAGES.stackCreate, name));
      observed = await iamStack.create(bucket);
    } else if (step.action === "update") {
      onProgress(formatTemplate(PROGRESS_MESSAGES.stackUpdate, name));
      observed = await iamStack.updateBucket(bucket);
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
  const arn = stack.outputs[BUILD_ROLE_OUTPUT_KEY];
  if (!arn) {
    throw new AccountSetupError("stack_incomplete", { name: IAM_STACK_NAME });
  }
  return arn;
}
