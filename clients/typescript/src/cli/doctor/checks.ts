/**
 * Las diez comprobaciones de `rayito doctor` (más `efs-network`, que sólo
 * corre con `--efs-vpc-id`). Espejo de `rayito.cli._checks`: cada una es una
 * función `(aws, contexto) → CheckResult`; `runCheck` la envuelve para que una
 * excepción sea `FAIL` (o `SKIP` cuando un `AccessDeniedException` sólo
 * impide un diagnóstico, no un prerequisito) y nunca aborte las siguientes.
 */

import { PortSpec } from "../../aws/control-plane.js";
import { buildQuotaEntries } from "../../dashboard/domain.js";
import { AuthenticationError, SandboxNotFoundError } from "../../errors.js";
import { VersionGate } from "../../images/gateway.js";
import {
  baseImageArn,
  IMAGE_ARTIFACT_KEY_PREFIX,
  MANAGED_BASE_IMAGE_NAME,
} from "../../images/managed.js";
import { DEFAULT_PORT, SUPPORTED_REGIONS, TOKEN_TTL_MINUTES } from "../../limits.js";
import type { SandboxListItem } from "../../models.js";
import { pythonStringRepr } from "../../strings.js";
import { MS_PER_SECOND } from "../../units.js";
import { VERSION } from "../../version.js";
import { isoUtc } from "../format.js";
import { assess } from "./compat.js";
import {
  ACCESS_DENIED,
  ADVISORY_SUMMARY,
  BUCKET_ACTIONS,
  BUCKET_OBJECT_ACTIONS,
  BUCKET_PERMISSIONS_HINT,
  CAPS_IMAGE_SUFFIX,
  CONNECTOR_ACTIONS,
  EFS_NETWORK_CHECK_NAME,
  IMAGE_ACTIONS,
  LAUNCH_METADATA,
  LAUNCH_TIMEOUT_SECONDS,
  MAX_LISTED_IDS,
  MAX_RUNNING_BEFORE_FAIL,
  PRE_LAUNCH_CHECK_COUNT,
  WILDCARD_ACTIONS,
} from "./constants.js";
import type { CheckResult, CheckStatus, DoctorContext } from "./model.js";
import { type AgentHealth, AwsOperationError, type DoctorAws, MissingPeerError } from "./ports.js";

const NOT_FOUND = "ResourceNotFoundException";
const PRINCIPAL_KINDS: ReadonlySet<string> = new Set([
  "user",
  "assumed-role",
  "root",
  "federated-user",
]);
const ARN_SEPARATOR_COUNT = 5;
const RUNNING_STATE = "RUNNING";
const UNKNOWN_VERSION = "?";

type CheckFunction = (aws: DoctorAws, context: DoctorContext) => Promise<CheckResult>;

export interface CheckSpec {
  readonly name: string;
  readonly run: CheckFunction;
  readonly deniedAction: string;
  readonly deniedStatus: CheckStatus;
}

function spec(
  name: string,
  run: CheckFunction,
  deniedAction: string,
  deniedStatus: CheckStatus = "FAIL",
): CheckSpec {
  return { name, run, deniedAction, deniedStatus };
}

function result(
  name: string,
  status: CheckStatus,
  summary: string,
  details: Record<string, unknown> = {},
): CheckResult {
  return { name, status, summary, details };
}

/** El tipo de principal de un ARN de STS o IAM (`user`, `assumed-role`, `root`...); `other` si no es ninguno. */
export function principalKind(arn: string): string {
  const separators = arn.split(":").length - 1;
  const resource =
    separators >= ARN_SEPARATOR_COUNT ? arn.split(":").slice(ARN_SEPARATOR_COUNT).join(":") : arn;
  const kind = resource.split("/")[0] ?? "";
  return PRINCIPAL_KINDS.has(kind) ? kind : "other";
}

/**
 * El simulador quiere el ARN IAM del principal: un usuario tal cual; un
 * assumed-role a través de `iam:GetRole` (el ARN de sesión pierde el path del
 * rol); root y el resto no se simulan.
 */
async function policySourceArn(aws: DoctorAws, arn: string): Promise<string | undefined> {
  const kind = principalKind(arn);
  if (kind === "user") {
    return arn;
  }
  if (kind === "assumed-role") {
    return aws.roleArn(arn.split("/")[1] ?? "");
  }
  return undefined;
}

async function checkCredentials(aws: DoctorAws, context: DoctorContext): Promise<CheckResult> {
  const identity = await aws.callerIdentity();
  context.account = identity.account;
  context.principalArn = identity.arn;
  context.principalKind = principalKind(identity.arn);
  const details: Record<string, unknown> = {
    account: context.account,
    arn: context.principalArn,
    principal_kind: context.principalKind,
    region: aws.region,
  };
  const summary = `cuenta ${context.account}, ${aws.region}, ${context.principalKind}`;
  if (!(SUPPORTED_REGIONS as readonly string[]).includes(aws.region)) {
    details.supported_regions = [...SUPPORTED_REGIONS];
    return result(
      "credentials",
      "WARN",
      `${summary}; región fuera de las ${SUPPORTED_REGIONS.length} con Lambda MicroVMs`,
      details,
    );
  }
  return result("credentials", "OK", summary, details);
}

async function newestManagedVersion(
  aws: DoctorAws,
  arn: string,
): Promise<{ imageVersion: string; createdAt: string | null } | null> {
  const versions = await aws.listManagedImageVersions(arn);
  const newest = versions.reduce<(typeof versions)[number] | undefined>(
    (best, item) => (best === undefined || item.createdAt > best.createdAt ? item : best),
    undefined,
  );
  return newest === undefined
    ? null
    : { imageVersion: newest.imageVersion, createdAt: isoUtc(newest.createdAt) };
}

async function checkManagedImages(aws: DoctorAws): Promise<CheckResult> {
  const managed = await aws.listManagedImageArns();
  const expected = baseImageArn(aws.region);
  const details: Record<string, unknown> = { managed, expected };
  if (!managed.includes(expected)) {
    return result(
      "managed-images",
      "WARN",
      `list-managed-microvm-images responde pero ${MANAGED_BASE_IMAGE_NAME} no está`,
      details,
    );
  }
  const newest = await newestManagedVersion(aws, expected);
  details.newest_version = newest;
  return result(
    "managed-images",
    "OK",
    `${MANAGED_BASE_IMAGE_NAME} disponible, versión más nueva ${newest?.imageVersion ?? UNKNOWN_VERSION}`,
    details,
  );
}

async function checkQuotas(aws: DoctorAws): Promise<CheckResult> {
  const applied = await aws.appliedQuotas();
  const listed = applied.map((quota) => ({
    code: quota.code,
    name: quota.name,
    value: quota.value,
    adjustable: quota.adjustable,
  }));
  const entries = buildQuotaEntries(applied, aws.region);
  const byCode = new Map(listed.map((quota) => [quota.code, quota]));
  const below = entries
    .filter((entry) => entry.belowDefault)
    .map((entry) => ({
      ...byCode.get(entry.code),
      short_name: entry.name,
      default: entry.defaultValue,
    }));
  const missing = entries
    .filter((entry) => entry.value === undefined)
    .map((entry) => `${entry.code} (${entry.name})`);
  const details = { quotas: listed, below_default: below, missing };
  if (below.length > 0 || missing.length > 0) {
    const named = below.map((quota) => `${quota.short_name} ${quota.value} < ${quota.default}`);
    if (missing.length > 0) {
      named.push(`sin listar: ${missing.join(", ")}`);
    }
    return result(
      "quotas",
      "WARN",
      `cuenta con cuotas reducidas: pide aumento (${named.join(", ")})`,
      details,
    );
  }
  return result(
    "quotas",
    "OK",
    `${listed.length} cuotas de MicroVM, ninguna por debajo del default`,
    details,
  );
}

interface SimulationGroup {
  readonly actions: readonly string[];
  readonly resources: readonly string[];
}

function simulationGroups(aws: DoctorAws, context: DoctorContext): SimulationGroup[] {
  const imageArn =
    context.templateArn ??
    `arn:aws:lambda:${aws.region}:${context.account}:microvm-image:${context.template}`;
  const connector = `arn:aws:lambda:${aws.region}:aws:network-connector:aws-network-connector:*`;
  const groups: SimulationGroup[] = [
    { actions: IMAGE_ACTIONS, resources: [imageArn] },
    { actions: WILDCARD_ACTIONS, resources: [] },
    { actions: CONNECTOR_ACTIONS, resources: [connector] },
  ];
  if (context.bucket) {
    groups.push({
      actions: BUCKET_OBJECT_ACTIONS,
      resources: [`arn:aws:s3:::${context.bucket}/${IMAGE_ARTIFACT_KEY_PREFIX}/*`],
    });
    groups.push({ actions: BUCKET_ACTIONS, resources: [`arn:aws:s3:::${context.bucket}`] });
  }
  return groups;
}

/**
 * Cada grupo de acciones se simula contra su recurso (una llamada por
 * recurso): el simulador cruza acciones y recursos, así que mezclar el ARN de
 * la imagen con el del bucket produciría denegaciones espurias.
 */
async function checkIamSimulation(aws: DoctorAws, context: DoctorContext): Promise<CheckResult> {
  if (context.principalArn === undefined) {
    return result("iam-simulation", "SKIP", "sin identidad (comprobación 1)");
  }
  const sourceArn = await policySourceArn(aws, context.principalArn);
  if (sourceArn === undefined) {
    return result(
      "iam-simulation",
      "SKIP",
      `principal ${context.principalKind}: nada que simular`,
      { principal_kind: context.principalKind },
    );
  }
  const results = [];
  for (const group of simulationGroups(aws, context)) {
    results.push(...(await aws.simulatePrincipalPolicy(sourceArn, group.actions, group.resources)));
  }
  const rows = results.map((item) => ({
    action: item.action,
    resource: item.resource,
    decision: item.decision,
    allowed_by_organizations: item.allowedByOrganizations ?? null,
  }));
  const explicit = rows.filter((row) => row.decision === "explicitDeny").map((row) => row.action);
  const bySCP = rows
    .filter((row) => row.allowed_by_organizations === false)
    .map((row) => row.action);
  const implicit = rows.filter((row) => row.decision === "implicitDeny").map((row) => row.action);
  const details = {
    policy_source_arn: sourceArn,
    results: rows,
    explicit_deny: explicit,
    denied_by_organizations: bySCP,
    implicit_deny: implicit,
  };
  if (explicit.length > 0 || bySCP.length > 0) {
    const named = [...new Set([...explicit, ...bySCP])].sort().join(", ");
    return result(
      "iam-simulation",
      "FAIL",
      `denegación explícita o por SCP: ${named}; ${ADVISORY_SUMMARY}`,
      details,
    );
  }
  if (implicit.length > 0) {
    return result(
      "iam-simulation",
      "WARN",
      `sin permiso explícito para ${implicit.join(", ")}; ${ADVISORY_SUMMARY}`,
      details,
    );
  }
  return result(
    "iam-simulation",
    "OK",
    `${rows.length} acciones permitidas; ${ADVISORY_SUMMARY}`,
    details,
  );
}

/**
 * Una sola `HeadBucket`: exige `s3:ListBucket`, el mismo permiso con el que
 * `publish` recibe un 404 (y no un 403) de `HeadObject` en un artefacto nuevo;
 * la región viene en su cabecera `x-amz-bucket-region`.
 */
async function checkBucket(aws: DoctorAws, context: DoctorContext): Promise<CheckResult> {
  const bucket = context.bucket;
  if (!bucket) {
    return result("bucket", "SKIP", "sin --bucket ni RAYITO_BUCKET");
  }
  let region: string;
  try {
    region = (await aws.headBucket(bucket)).region;
  } catch (error) {
    if (!(error instanceof AwsOperationError)) {
      throw error;
    }
    return result(
      "bucket",
      "FAIL",
      `s3://${bucket} no accesible (${error.operation} ${error.code}: ${error.message}); ${BUCKET_PERMISSIONS_HINT}`,
      { bucket, operation: error.operation, error: error.code },
    );
  }
  const details = { bucket, bucket_region: region, region: aws.region };
  if (region === "") {
    return result(
      "bucket",
      "WARN",
      `s3://${bucket} accesible pero head-bucket no trajo x-amz-bucket-region`,
      details,
    );
  }
  if (region !== aws.region) {
    return result(
      "bucket",
      "WARN",
      `s3://${bucket} está en ${region}, no en ${aws.region}: el build fallará con S3_CROSS_REGION_ACCESS_DENIED`,
      details,
    );
  }
  return result("bucket", "OK", `s3://${bucket} accesible en ${region}`, details);
}

async function checkImageGate(aws: DoctorAws, context: DoctorContext): Promise<CheckResult> {
  const arn = await aws.plane.resolveTemplateArn(context.template);
  context.templateArn = arn;
  const image = await aws.getImage(arn);
  if (image === undefined) {
    return result(
      "image-gate",
      "FAIL",
      `la imagen ${context.template} no existe: publica con \`rayito image publish\``,
      { template_arn: arn, error: NOT_FOUND },
    );
  }
  const version = context.templateVersion ?? image.latestActiveVersion;
  const details: Record<string, unknown> = {
    template_arn: arn,
    image_state: image.state,
    latest_active_version: image.latestActiveVersion ?? null,
    latest_failed_version: image.latestFailedVersion ?? null,
  };
  if (!version) {
    return result(
      "image-gate",
      "FAIL",
      `${context.template} sin versión activa: publica con \`rayito image publish\``,
      details,
    );
  }
  context.resolvedVersion = version;
  const detail = await aws.getImageVersion(arn, version);
  const gate = new VersionGate(image.state, detail.state, detail.status, detail.stateReason);
  const build = await aws.latestBuild(arn, version);
  Object.assign(details, {
    version,
    gate: {
      imageState: gate.imageState,
      versionState: gate.versionState,
      versionStatus: gate.versionStatus,
      stateReason: gate.stateReason ?? null,
      launchable: gate.launchable,
    },
    chipset_generation: build.chipsetGeneration ?? null,
    snapshot_build: build.snapshotBuild ?? {},
  });
  if (!gate.launchable) {
    return result(
      "image-gate",
      "FAIL",
      `${context.template} ${version} no lanzable: imagen ${gate.imageState}, ` +
        `versión ${gate.versionState}/${gate.versionStatus} (${reasonText(gate.stateReason)})`,
      details,
    );
  }
  if (image.latestFailedVersion) {
    return result(
      "image-gate",
      "WARN",
      `${context.template} ${version} lanzable; un build fallido ` +
        `(${image.latestFailedVersion}) sigue listado: \`rayito image prune\``,
      details,
    );
  }
  return result("image-gate", "OK", `${context.template} ${version} lanzable`, details);
}

function reasonText(reason: string | undefined): string {
  return reason === undefined ? "None" : pythonStringRepr(reason);
}

function runningItems(items: readonly SandboxListItem[]): SandboxListItem[] {
  return items
    .filter((item) => item.state === RUNNING_STATE)
    .sort((left, right) => left.startedAt.getTime() - right.startedAt.getTime());
}

async function checkSandboxes(aws: DoctorAws, context: DoctorContext): Promise<CheckResult> {
  const arn = context.templateArn ?? (await aws.plane.resolveTemplateArn(context.template));
  context.templateArn = arn;
  const ofTemplate: SandboxListItem[] = [];
  for await (const item of aws.plane.listMicrovms({ imageArn: arn })) {
    ofTemplate.push(item);
  }
  const counts: Record<string, number> = {};
  for (const item of ofTemplate) {
    counts[item.state] = (counts[item.state] ?? 0) + 1;
  }
  const running = runningItems(ofTemplate);
  const newest = running.at(-1);
  const details = {
    template_arn: arn,
    counts,
    running: running.length,
    oldest_running_started_at: isoUtc(running[0]?.startedAt),
    running_ids: running.slice(0, MAX_LISTED_IDS).map((item) => item.sandboxId),
  };
  if (newest !== undefined) {
    context.targetSandbox = await aws.plane.getMicrovm(newest.sandboxId);
  }
  if (running.length > MAX_RUNNING_BEFORE_FAIL) {
    return result(
      "sandboxes",
      "FAIL",
      `${running.length} MicroVMs RUNNING de ${context.template} (más de ` +
        `${MAX_RUNNING_BEFORE_FAIL}): \`rayito sandbox kill --all --template ${context.template}\``,
      details,
    );
  }
  if (running.length > 0) {
    return result(
      "sandboxes",
      "WARN",
      `${running.length} MicroVMs vivos facturando: \`rayito sandbox list\`, \`rayito sandbox kill --all\``,
      details,
    );
  }
  return result("sandboxes", "OK", `ningún MicroVM RUNNING de ${context.template}`, details);
}

function tokenDetails(sandboxId: string, source: string): Record<string, unknown> {
  return { sandbox_id: sandboxId, port: DEFAULT_PORT, ttl_minutes: TOKEN_TTL_MINUTES, source };
}

async function checkToken(aws: DoctorAws, context: DoctorContext): Promise<CheckResult> {
  if (context.launch) {
    const launched = await aws.launch({
      template: context.template,
      templateVersion: context.templateVersion,
      timeoutMs: LAUNCH_TIMEOUT_SECONDS * MS_PER_SECOND,
      metadata: LAUNCH_METADATA,
    });
    context.launched = launched;
    context.targetSandbox = launched.info;
    return result(
      "token",
      "OK",
      `token acuñado por create() para ${launched.sandboxId}`,
      tokenDetails(launched.sandboxId, "create()"),
    );
  }
  if (context.targetSandbox === undefined) {
    return result("token", "SKIP", "sin sandbox RUNNING: usa --launch para probar uno efímero");
  }
  const sandboxId = context.targetSandbox.sandboxId;
  context.mintedToken = await aws.plane.createAuthToken(sandboxId, [PortSpec.single(DEFAULT_PORT)]);
  return result(
    "token",
    "OK",
    `token del proxy acuñado para ${sandboxId} (puerto ${DEFAULT_PORT})`,
    tokenDetails(sandboxId, "create-microvm-auth-token"),
  );
}

function healthDetails(health: AgentHealth): Record<string, unknown> {
  return {
    agent_version: health.agentVersion,
    agent_ready: health.agentReady,
    kernel_ready: health.kernelReady,
    imds_blocked: health.imdsBlocked,
    hook_anomalies: health.hookAnomalies,
    resume_generation: health.resumeGeneration,
    uptime_ms: health.uptimeMs,
  };
}

async function readHealth(
  aws: DoctorAws,
  context: DoctorContext,
): Promise<AgentHealth | undefined> {
  if (context.launched !== undefined) {
    return aws.probeHealth(context.launched.info, undefined);
  }
  if (context.targetSandbox === undefined || context.mintedToken === undefined) {
    return undefined;
  }
  return aws.probeHealth(context.targetSandbox, context.mintedToken);
}

function pythonBool(value: boolean): string {
  return value ? "True" : "False";
}

function agentWarnings(health: AgentHealth, context: DoctorContext): string[] {
  const warnings: string[] = [];
  if (!health.kernelReady) {
    warnings.push("kernel_ready=false (el sidecar aún rota el kernel)");
  }
  if (health.hookAnomalies > 0) {
    warnings.push(`hook_anomalies=${health.hookAnomalies} (alguien posee un token allPorts)`);
  }
  const capsImage = (context.template.split(":").at(-1) ?? "").endsWith(CAPS_IMAGE_SUFFIX);
  if (capsImage && !health.imdsBlocked) {
    warnings.push("imds_blocked=false en una imagen -caps");
  }
  return warnings;
}

async function checkAgent(aws: DoctorAws, context: DoctorContext): Promise<CheckResult> {
  const health = await readHealth(aws, context);
  if (health === undefined) {
    return result("agent", "SKIP", "sin token (comprobación 8)");
  }
  context.health = health;
  const details = healthDetails(health);
  if (!health.agentReady) {
    return result("agent", "FAIL", "rayd responde pero no está agent_ready", details);
  }
  const warnings = agentWarnings(health, context);
  if (warnings.length > 0) {
    return result("agent", "WARN", `rayd ${health.agentVersion}: ${warnings.join("; ")}`, details);
  }
  return result(
    "agent",
    "OK",
    `rayd ${health.agentVersion} agent_ready, kernel_ready=${pythonBool(health.kernelReady)}, ` +
      `imds_blocked=${pythonBool(health.imdsBlocked)}`,
    details,
  );
}

function withImageVersion(reason: string, imageVersion: string | undefined): string {
  return imageVersion === undefined
    ? reason
    : `${reason}; imagen ${imageVersion} (contador de builds, informativo)`;
}

/** La versión de imagen sólo se informa: es el contador de builds de esa imagen en esta cuenta, no un criterio. */
async function checkCompatibility(_aws: DoctorAws, context: DoctorContext): Promise<CheckResult> {
  const imageVersion = context.targetSandbox?.templateVersion ?? context.resolvedVersion;
  if (context.health === undefined) {
    return result(
      "compatibility",
      "SKIP",
      `sin agent_version (comprobaciones 8-9): SDK ${VERSION} no comprobado`,
      { sdk_version: VERSION, image_version: imageVersion ?? null },
    );
  }
  const assessment = assess(VERSION, context.health.agentVersion);
  const row = assessment.row;
  return result(
    "compatibility",
    assessment.status,
    withImageVersion(assessment.reason, imageVersion),
    {
      sdk_version: VERSION,
      agent_version: context.health.agentVersion,
      image_version: imageVersion ?? null,
      row:
        row === undefined
          ? null
          : { sdk_series: row.sdkSeries, min_agent_version: row.minAgentVersion, note: row.note },
    },
  );
}

/**
 * La comprobación previa de `EfsVolumes.check()` sobre la VPC y las subredes
 * de `--efs-vpc-id`/`--efs-subnet-ids`: sólo `ec2:Describe*`, nunca crea
 * nada. Informa de lo que `rayito stack deploy efs-volumes` crearía y de su
 * coste.
 */
async function checkEfsNetwork(aws: DoctorAws, context: DoctorContext): Promise<CheckResult> {
  const report = await aws.inspectEfsNetwork(context.efsVpcId ?? "", context.efsSubnetIds ?? "");
  const worst = report.findings
    .filter((finding) => finding.level === report.status && report.status !== "OK")
    .map((finding) => finding.message);
  const plan = report.ok
    ? `crearía ${report.cost.creates.join(", ")}; en reposo ${report.cost.idleMonthly}`
    : "no crearía nada hasta corregir lo anterior";
  const summary =
    worst.length > 0
      ? worst
      : [
          `VPC apta para efs-volumes: ${report.subnetIds.length} subred(es) en ` +
            `${report.availabilityZones.length} AZ(s)`,
        ];
  return result(EFS_NETWORK_CHECK_NAME, report.status, `${summary.join("; ")}; ${plan}`, {
    availability_zones: [...report.availabilityZones],
    findings: report.findings.map((finding) => ({ ...finding })),
    creates: [...report.cost.creates],
    idle_monthly: report.cost.idleMonthly,
    per_use: [...(report.cost.perUse ?? [])],
    removal: report.cost.removal ?? null,
  });
}

/** Fuera de `CHECKS`: sólo corre con `--efs-vpc-id` (y sus subredes). */
export const EFS_NETWORK_CHECK = spec(EFS_NETWORK_CHECK_NAME, checkEfsNetwork, "ec2:DescribeVpcs");

export const CHECKS: readonly CheckSpec[] = [
  spec("credentials", checkCredentials, "sts:GetCallerIdentity"),
  spec("managed-images", checkManagedImages, "lambda:ListManagedMicrovmImages"),
  spec("quotas", checkQuotas, "servicequotas:ListServiceQuotas", "SKIP"),
  spec("iam-simulation", checkIamSimulation, "iam:SimulatePrincipalPolicy", "SKIP"),
  spec("bucket", checkBucket, "s3:ListBucket"),
  spec("image-gate", checkImageGate, "lambda:GetMicrovmImage"),
  spec("sandboxes", checkSandboxes, "lambda:ListMicrovms"),
  spec("token", checkToken, "lambda:CreateMicrovmAuthToken"),
  spec("agent", checkAgent, "lambda:CreateMicrovmAuthToken"),
  spec("compatibility", checkCompatibility, "-"),
];
export const PRE_LAUNCH_CHECKS = CHECKS.slice(0, PRE_LAUNCH_CHECK_COUNT);
export const LAUNCH_CHECKS = CHECKS.slice(PRE_LAUNCH_CHECK_COUNT);

/** El nombre de cada comprobación, en orden; `testdata/cli/doctor-contract.json` lo fija. */
export const CHECK_NAMES: readonly string[] = CHECKS.map((check) => check.name);

/**
 * `operation` es la llamada que AWS rechazó, que puede no ser la de
 * `deniedAction` cuando la comprobación hace varias; `action` sigue siendo el
 * permiso IAM que la comprobación necesita.
 */
function denied(check: CheckSpec, message: string, operation?: string): CheckResult {
  const named = operation ?? check.deniedAction;
  return result(check.name, check.deniedStatus, `${named} denegado: ${message}`, {
    action: check.deniedAction,
    operation: named,
    error: ACCESS_DENIED,
  });
}

function failureOf(check: CheckSpec, error: unknown): CheckResult {
  if (error instanceof AwsOperationError) {
    if (error.code === ACCESS_DENIED) {
      return denied(check, error.message, error.operation);
    }
    return result(
      check.name,
      "FAIL",
      `AWS error ${error.code} en ${error.operation}: ${error.message}`,
      { error: error.code, operation: error.operation },
    );
  }
  if (error instanceof MissingPeerError) {
    return result(check.name, check.deniedStatus, error.message, { error: error.name });
  }
  if (error instanceof AuthenticationError) {
    return denied(check, error.message);
  }
  if (error instanceof SandboxNotFoundError) {
    return result(check.name, "FAIL", error.message, { error: error.name });
  }
  const name = error instanceof Error ? error.name : typeof error;
  const message = error instanceof Error ? error.message : String(error);
  return result(check.name, "FAIL", `${name}: ${message}`, { error: name });
}

/**
 * Nunca relanza: `AccessDeniedException` es `FAIL` (o `SKIP`) nombrando la
 * operación rechazada; cualquier otra excepción, `FAIL` con su clase y mensaje.
 */
export async function runCheck(
  check: CheckSpec,
  aws: DoctorAws,
  context: DoctorContext,
): Promise<CheckResult> {
  const started = performance.now();
  let outcome: CheckResult;
  try {
    outcome = await check.run(aws, context);
  } catch (error) {
    outcome = failureOf(check, error);
  }
  outcome.details.elapsed_ms = Math.trunc(performance.now() - started);
  return outcome;
}

async function killLaunched(
  context: DoctorContext,
  warn: (message: string) => void,
): Promise<void> {
  const launched = context.launched;
  if (launched === undefined) {
    return;
  }
  try {
    await launched.kill();
  } catch {
    warn(`no se pudo terminar el sandbox ${launched.sandboxId}`);
  }
}

/**
 * Las siete comprobaciones de sólo lectura (más `efs-network` con
 * `--efs-vpc-id`) y, dentro de un `finally` que mata el sandbox de `--launch`
 * pase lo que pase, las tres del agente.
 */
export async function runDoctor(
  aws: DoctorAws,
  context: DoctorContext,
  warn: (message: string) => void,
): Promise<CheckResult[]> {
  const results: CheckResult[] = [];
  for (const check of PRE_LAUNCH_CHECKS) {
    results.push(await runCheck(check, aws, context));
  }
  if (context.efsVpcId !== undefined || context.efsSubnetIds !== undefined) {
    results.push(await runCheck(EFS_NETWORK_CHECK, aws, context));
  }
  try {
    for (const check of LAUNCH_CHECKS) {
      results.push(await runCheck(check, aws, context));
    }
  } finally {
    await killLaunched(context, warn);
  }
  return results;
}
