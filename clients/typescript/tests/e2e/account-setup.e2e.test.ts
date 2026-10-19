/**
 * `rayito setup` contra AWS real (ADR-030). No se ejecuta en CI: lo corre el
 * mantenedor en una región que aún no está preparada.
 *
 * Variables, todas de tiempo de ejecución y ninguna con valor por defecto:
 *
 * - `RAYITO_E2E=1` y `RAYITO_E2E_SETUP_REGION`: la región a preparar. Sin ellas
 *   el primer bloque se omite sin llamar a AWS.
 * - `RAYITO_E2E_SETUP_BUCKET` (opcional): `--bucket`; sin ella se usa el del
 *   bootstrap de CDK de esa región.
 * - `RAYITO_E2E_SETUP_ARTIFACT` (opcional): `--artifact` con el zip construido
 *   desde el árbol (durante la PR de una release no existe aún la release
 *   `rayd-v<versión>`); sin ella se descarga de la release.
 * - `RAYITO_TEMPLATE` (con `RAYITO_E2E=1`): activa el segundo bloque, que
 *   comprueba la cuenta ya preparada de la región e2e por defecto.
 *
 * Pre-chequeo de sólo lectura: si `rayito-m0-iam` o `rayito-base` ya existen en
 * la región elegida, el primer bloque se omite entero (nunca se toca lo que
 * existía). Pruebas del primer bloque, en orden:
 *
 * 1. `--dry-run` planea `create` y `publish` y no escribe nada.
 * 2. Reanudación: `setup --yes` en un subproceso (`dist/cli.mjs`, hace falta
 *    `pnpm build`) se mata cuando hay una versión de la imagen en build; la
 *    segunda ejecución sale 0 con `ready: true` y deja una única versión.
 * 3. Una segunda ejecución planea tres `reuse`, no escribe nada y no crea una
 *    versión nueva.
 *
 * Limpieza: borra sólo lo que creó la ejecución (la imagen con sus versiones y
 * su grupo de logs por `image-cleanup.ts`, la pila de IAM y la clave del zip
 * sólo cuando `artifactUploaded` fue `true` o el zip dado no existía antes).
 * Necesita `lambda:DeleteMicrovmImage`, `logs:DeleteLogGroup`,
 * `cloudformation:DeleteStack` y `s3:DeleteObject` sobre `rayito/images/*`.
 *
 * Coste: una versión de `rayito-base` (≈ $0,04 por semana, mínimo una semana),
 * la pila de IAM (gratis) y el zip en el bucket (céntimos); la limpieza lo borra.
 */

import { type ChildProcess, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { CloudWatchLogsClient } from "@aws-sdk/client-cloudwatch-logs";
import { LambdaMicrovmsClient } from "@aws-sdk/client-lambda-microvms";
import { DeleteObjectCommand, HeadObjectCommand, S3Client } from "@aws-sdk/client-s3";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import {
  BASE_IMAGE_NAME,
  IAM_STACK_NAME,
  type ImageObservation,
  type StackObservation,
} from "../../src/account-setup/domain.js";
import { IAM_STACK_COMPONENT } from "../../src/account-setup/iam-stack.js";
import { clientConfig } from "../../src/aws/control-plane.js";
import { awsCode } from "../../src/aws/optional-client.js";
import { createAwsServices } from "../../src/cli/aws-services.js";
import type { Terminal } from "../../src/cli/console.js";
import { runCli } from "../../src/cli/run.js";
import type { CliServices } from "../../src/cli/services.js";
import { BUILDING_VERSION_STATES } from "../../src/images/gateway.js";
import { artifactKey } from "../../src/images/publish-domain.js";
import { E2E_FLAG_VAR, e2eEnabled, e2eSettings, sleep, waitUntil } from "./helpers.js";
import { BuiltImages } from "./image-cleanup.js";

const REGION_VAR = "RAYITO_E2E_SETUP_REGION";
const BUCKET_VAR = "RAYITO_E2E_SETUP_BUCKET";
const ARTIFACT_VAR = "RAYITO_E2E_SETUP_ARTIFACT";
const CLI_ENTRY = new URL("../../dist/cli.mjs", import.meta.url);
const VERIFIED_PATTERN = /sha256 de rayito-image\.zip verificado contra SHA256SUMS: ([0-9a-f]{64})/;
const STACK_HOOK_TIMEOUT_MS = 900_000;
const BUILD_START_BUDGET_MS = 600_000;
const BUILD_START_POLL_MS = 5_000;
const KILL_SETTLE_MS = 2_000;
const NOT_FOUND_CODES: ReadonlySet<string> = new Set(["404", "NotFound", "NoSuchKey"]);

interface Outcome {
  readonly exitCode: number;
  readonly stdout: string;
  readonly stderr: string;
}

interface SetupDocument {
  readonly ready: boolean;
  readonly steps: ReadonlyArray<{ readonly resource: string; readonly action: string }>;
  readonly result: {
    readonly bucket: string;
    readonly imageVersion: string;
    readonly artifactUploaded: boolean | null;
  } | null;
  readonly exitCode: number;
}

function setupEnabled(): boolean {
  return process.env[E2E_FLAG_VAR] === "1" && Boolean(process.env[REGION_VAR]);
}

function setupArguments(region: string, ...extra: string[]): string[] {
  const bucket = process.env[BUCKET_VAR];
  const artifact = process.env[ARTIFACT_VAR];
  return [
    "--region",
    region,
    "--json",
    "setup",
    ...(bucket ? ["--bucket", bucket] : []),
    ...(artifact ? ["--artifact", artifact] : []),
    ...extra,
  ];
}

function capturingTerminal(): { terminal: Terminal; out: () => Outcome } {
  let stdout = "";
  let stderr = "";
  const terminal: Terminal = {
    stdout: {
      write: (text) => {
        stdout += String(text);
        return true;
      },
    },
    stderr: {
      write: (text) => {
        stderr += String(text);
        return true;
      },
    },
    readLine: async () => undefined,
  };
  return { terminal, out: () => ({ exitCode: 0, stdout, stderr }) };
}

async function runSetup(args: string[]): Promise<Outcome> {
  const { terminal, out } = capturingTerminal();
  const exitCode = await runCli(args, {
    terminal,
    env: process.env,
    now: () => new Date(),
    sleep: (ms) => sleep(ms),
    signal: new AbortController().signal,
    createServices: createAwsServices,
  });
  return { ...out(), exitCode };
}

function documentOf(outcome: Outcome): SetupDocument {
  return JSON.parse(outcome.stdout) as SetupDocument;
}

function actionsOf(document: SetupDocument): string[] {
  return document.steps.map((step) => step.action);
}

async function servicesIn(region: string | undefined): Promise<CliServices> {
  return createAwsServices({ profile: undefined, region, env: process.env, logger: undefined });
}

async function observe(
  services: CliServices,
): Promise<{ stack: StackObservation; image: ImageObservation }> {
  return {
    stack: await services.accountSetup.iamStack.observe(),
    image: await services.accountSetup.images.observe(BASE_IMAGE_NAME),
  };
}

function isCreating(image: ImageObservation): boolean {
  return image.versions.some((version) => BUILDING_VERSION_STATES.has(version.state));
}

function startSubprocess(region: string): ChildProcess {
  return spawn(process.execPath, [CLI_ENTRY.pathname, ...setupArguments(region, "--yes")], {
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });
}

async function headObjectExists(client: S3Client, bucket: string, key: string): Promise<boolean> {
  try {
    await client.send(new HeadObjectCommand({ Bucket: bucket, Key: key }));
    return true;
  } catch (error) {
    if (NOT_FOUND_CODES.has(awsCode(error) ?? "")) {
      return false;
    }
    throw error;
  }
}

describe.runIf(setupEnabled())("rayito setup en una región nueva (AWS real)", () => {
  const region = process.env[REGION_VAR] ?? "";
  let services: CliServices;
  let fresh = false;
  let imageCreated = false;
  let artifactOwned: { readonly bucket: string; readonly key: string } | undefined;
  let artifactAlreadyThere = false;

  beforeAll(async () => {
    services = await servicesIn(region);
    const existing = await observe(services);
    fresh = existing.stack.status === undefined && existing.image.state === undefined;
    const artifact = process.env[ARTIFACT_VAR];
    const bucket = process.env[BUCKET_VAR];
    if (fresh && artifact && bucket) {
      const sha = createHash("sha256").update(readFileSync(artifact)).digest("hex");
      artifactAlreadyThere = await headObjectExists(
        new S3Client(clientConfig(region)),
        bucket,
        artifactKey(sha, undefined),
      );
    }
  }, STACK_HOOK_TIMEOUT_MS);

  afterAll(async () => {
    if (!fresh) {
      return;
    }
    const failures: string[] = [];
    if (imageCreated) {
      const client = new LambdaMicrovmsClient(clientConfig(region));
      const images = new BuiltImages({
        client,
        resolveArn: (name) => services.controlPlane.resolveTemplateArn(name),
        logs: new CloudWatchLogsClient(clientConfig(region)),
      });
      images.track(BASE_IMAGE_NAME);
      failures.push(...(await images.deleteAll()));
    }
    await services.stacks.destroy(IAM_STACK_COMPONENT, { stackName: IAM_STACK_NAME });
    if (artifactOwned !== undefined && !artifactAlreadyThere) {
      await new S3Client(clientConfig(region)).send(
        new DeleteObjectCommand({ Bucket: artifactOwned.bucket, Key: artifactOwned.key }),
      );
    }
    if (failures.length > 0) {
      throw new Error(`sin borrar (bórralo a mano): ${failures.join("; ")}`);
    }
  }, STACK_HOOK_TIMEOUT_MS);

  test("--dry-run planea crear y publicar y no escribe nada", async (context) => {
    if (!fresh) {
      context.skip();
    }
    const outcome = await runSetup(setupArguments(region, "--dry-run"));
    const document = documentOf(outcome);
    expect(outcome.exitCode).toBe(0);
    expect(actionsOf(document)).toEqual(["reuse", "create", "publish"]);
    const after = await observe(services);
    expect(after.stack.status).toBeUndefined();
    expect(after.image.state).toBeUndefined();
  });

  test("una ejecución interrumpida se reanuda sin duplicar la versión", async (context) => {
    if (!fresh) {
      context.skip();
    }
    const child = startSubprocess(region);
    let childStderr = "";
    child.stderr?.on("data", (chunk: Buffer) => {
      childStderr += chunk.toString("utf8");
    });
    imageCreated = true;
    await waitUntil(
      async () => isCreating((await observe(services)).image),
      BUILD_START_BUDGET_MS,
      "una versión de rayito-base en build",
      BUILD_START_POLL_MS,
    );
    child.kill("SIGKILL");
    await sleep(KILL_SETTLE_MS);
    const outcome = await runSetup(setupArguments(region, "--yes"));
    const document = documentOf(outcome);
    expect(outcome.exitCode).toBe(0);
    expect(document.ready).toBe(true);
    expect((await observe(services)).image.versions).toHaveLength(1);
    const sha = VERIFIED_PATTERN.exec(`${childStderr}${outcome.stderr}`)?.[1];
    if (
      document.result !== null &&
      sha !== undefined &&
      document.result.artifactUploaded === true
    ) {
      artifactOwned = { bucket: document.result.bucket, key: artifactKey(sha, undefined) };
    }
  });

  test("una segunda ejecución planea tres reuse y no crea otra versión", async (context) => {
    if (!fresh) {
      context.skip();
    }
    const dry = documentOf(await runSetup(setupArguments(region, "--dry-run")));
    expect(actionsOf(dry)).toEqual(["reuse", "reuse", "reuse"]);
    const before = (await observe(services)).image.versions.length;
    const outcome = await runSetup(setupArguments(region, "--yes"));
    expect(outcome.exitCode).toBe(0);
    expect(documentOf(outcome).result?.artifactUploaded).toBeNull();
    expect((await observe(services)).image.versions).toHaveLength(before);
  });
});

describe.runIf(e2eEnabled())("rayito setup en la región e2e ya preparada (AWS real)", () => {
  test("--dry-run --json planea tres reuse y no escribe nada", async () => {
    const { region } = e2eSettings();
    const services = await servicesIn(region);
    const before = await observe(services);
    const outcome = await runSetup([
      ...(region === undefined ? [] : ["--region", region]),
      "--json",
      "setup",
      "--dry-run",
    ]);
    const document = documentOf(outcome);
    expect(outcome.exitCode).toBe(0);
    expect(actionsOf(document)).toEqual(["reuse", "reuse", "reuse"]);
    expect(document.ready).toBe(true);
    const after = await observe(services);
    expect(after.image.versions).toHaveLength(before.image.versions.length);
    expect(after.stack.status).toBe(before.stack.status);
  });
});
