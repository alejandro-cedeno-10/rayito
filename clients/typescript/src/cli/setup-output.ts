/**
 * La salida de `rayito setup` (ADR-030, D6): las plantillas de texto del
 * contrato (`testdata/account-setup/contract.json`), las líneas del plan y las
 * finales, y el documento JSON de `--json`. Espejo de `cli/_setup_output.py`.
 * Sólo da forma al texto: no lee AWS ni escribe en la terminal.
 */

import {
  type AccountSetupError,
  BUCKET_SOURCE_LABELS,
  type BucketSource,
  formatTemplate,
  type ResourceKind,
  resultJson,
  type SetupPlan,
  type SetupResult,
  type SetupStep,
  stepJson,
  stepOf,
} from "../account-setup/domain.js";

export const HEADER_TEMPLATE = "rayito setup: cuenta {account}, región {region}";
export const STEP_TEMPLATE = "  {label} {name}: {action}";
export const RESOURCE_LABELS: Readonly<Record<ResourceKind, string>> = {
  bucket: "bucket",
  iamStack: "pila de IAM",
  baseImage: "imagen",
};
/** El texto de cada acción; `bucketReuse` y las de imagen llevan los campos del paso. */
export const ACTION_TEMPLATES = {
  bucketReuse: "reutilizar ({source})",
  stackCreate: "crear",
  stackUpdate: "actualizar ArtifactBucket {previousBucket} -> {bucket}",
  stackReuse: "ya existe ({status})",
  stackWait: "en curso ({status}): esperar",
  imagePublishRelease: "publicar desde {release} (≈ 10 min)",
  imagePublishArtifact: "publicar desde {artifact} (≈ 10 min)",
  imageReuse: "ya existe (versión {imageVersion} lanzable)",
  imageWait: "build en curso (versión {imageVersion}): esperar",
} as const;
export const COST_HEADER = "Coste aproximado:";
export const COST_LINE_TEMPLATE = "  {text}";
export const NOTHING_TO_DO = "La cuenta ya está preparada: no hay nada que crear.";
export const DRY_RUN_FOOTER = "no se creó ni cambió nada (--dry-run)";
export const CONFIRM_QUESTION = "¿Preparar la cuenta?";
export const DONE_TEMPLATE = "rayito setup: cuenta preparada en {region}";
export const TEMPLATE_LINE_TEMPLATE = "RAYITO_TEMPLATE={imageArn}";
export const BUCKET_LINE_TEMPLATE = "RAYITO_BUCKET={bucket}";
export const DOCTOR_FAILED =
  "rayito setup: rayito doctor encontró algún FAIL; la cuenta no está lista";
export const ERROR_PREFIX = "rayito: ";

type ActionKey = keyof typeof ACTION_TEMPLATES;

/** La plantilla de cada par recurso-acción; `publish` distingue la release del `--artifact` por su origen. */
const ACTION_KEYS: Readonly<Record<string, ActionKey>> = {
  "bucket:reuse": "bucketReuse",
  "iamStack:create": "stackCreate",
  "iamStack:update": "stackUpdate",
  "iamStack:reuse": "stackReuse",
  "iamStack:wait": "stackWait",
  "baseImage:publish:release": "imagePublishRelease",
  "baseImage:publish:artifact": "imagePublishArtifact",
  "baseImage:reuse": "imageReuse",
  "baseImage:wait": "imageWait",
};

function actionKey(step: SetupStep): ActionKey {
  const pair = `${step.resource}:${step.action}`;
  const key = ACTION_KEYS[step.action === "publish" ? `${pair}:${step.source}` : pair];
  if (key === undefined) {
    throw new Error(`acción sin texto: ${pair}`);
  }
  return key;
}

function actionText(plan: SetupPlan, step: SetupStep): string {
  const template = ACTION_TEMPLATES[actionKey(step)];
  return formatTemplate(template, {
    source: BUCKET_SOURCE_LABELS[(step.source ?? "argument") as BucketSource],
    previousBucket: step.previousBucket ?? "",
    bucket: stepOf(plan, "bucket").name,
    status: step.status ?? "",
    release: step.release ?? "",
    artifact: plan.artifact ?? "",
    imageVersion: step.imageVersion ?? "",
  });
}

/** El encabezado, un paso por línea, el coste de lo que cuesta y, si todo existe, que no hay nada que hacer. */
export function planLines(plan: SetupPlan): string[] {
  const lines = [formatTemplate(HEADER_TEMPLATE, { account: plan.account, region: plan.region })];
  for (const step of plan.steps) {
    lines.push(
      formatTemplate(STEP_TEMPLATE, {
        label: RESOURCE_LABELS[step.resource],
        name: step.name,
        action: actionText(plan, step),
      }),
    );
  }
  if (plan.cost.length > 0) {
    lines.push(
      COST_HEADER,
      ...plan.cost.map((text) => formatTemplate(COST_LINE_TEMPLATE, { text })),
    );
  }
  if (plan.ready) {
    lines.push(NOTHING_TO_DO);
  }
  return lines;
}

/** Las tres últimas líneas de un `setup` correcto: la cuenta lista y las variables para lo siguiente. */
export function finalLines(result: SetupResult, region: string): string[] {
  return [
    formatTemplate(DONE_TEMPLATE, { region }),
    formatTemplate(TEMPLATE_LINE_TEMPLATE, { imageArn: result.imageArn }),
    formatTemplate(BUCKET_LINE_TEMPLATE, { bucket: result.bucket }),
  ];
}

export interface SetupDocumentInput {
  readonly sdkVersion: string;
  readonly region: string;
  readonly dryRun: boolean;
  readonly ready: boolean;
  readonly plan: SetupPlan | undefined;
  readonly result: SetupResult | undefined;
  readonly doctor: Record<string, unknown> | undefined;
  readonly error: AccountSetupError | undefined;
  readonly exitCode: number;
}

/** El documento único de `--json`, con las claves en el orden del contrato. */
export function setupDocument(input: SetupDocumentInput): Record<string, unknown> {
  const { plan, error } = input;
  return {
    rayito: input.sdkVersion,
    region: input.region,
    account: plan?.account ?? null,
    dryRun: input.dryRun,
    ready: input.ready,
    steps: plan === undefined ? [] : plan.steps.map(stepJson),
    cost: plan === undefined ? [] : [...plan.cost],
    result: input.result === undefined ? null : resultJson(input.result),
    doctor: input.doctor ?? null,
    error: error === undefined ? null : { code: error.code, message: error.message },
    exitCode: input.exitCode,
  };
}
