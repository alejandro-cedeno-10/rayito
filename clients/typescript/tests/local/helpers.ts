/**
 * Ajustes de los tests `local` (`make local-e2e`), espejo de
 * `clients/python/tests/local/conftest.py`: el SDK contra el `rayd` del
 * contenedor `guest` y contra Floci, sin AWS. Sin `RAYITO_LOCAL_GUEST` (la
 * define el runner de `dev/local/compose.yaml`) los tests se saltan. Las
 * llamadas a AWS van a Floci por `AWS_ENDPOINT_URL`, que el SDK v3 lee del
 * entorno, con las credenciales ficticias del compose.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  CreateMicrovmImageCommand,
  GetMicrovmImageCommand,
  LambdaMicrovmsClient,
} from "@aws-sdk/client-lambda-microvms";
import { CreateBucketCommand, PutObjectCommand, S3Client } from "@aws-sdk/client-s3";
import {
  LambdaMicrovmsControlPlane,
  Sandbox,
  type SandboxCreateOptions,
  SandboxNotFoundError,
} from "../../src/index.js";
import { writeZip } from "../../src/templates/zip-node.js";
import { GuestAddress, LOCAL_GUEST_VAR, LocalGuestControlPlane } from "./guest.js";

export const ARTIFACT_BUCKET_VAR = "RAYITO_LOCAL_ARTIFACT_BUCKET";
export const DEFAULT_ARTIFACT_BUCKET = "rayito-local-artifacts";
export const LOCAL_IMAGE_NAME = "rayito-local";
/** La imagen gestionada que lista Floci y la `BASE_IMAGE_VERSION` del Makefile. */
const MANAGED_BASE_IMAGE = "al2023-1";
const BASE_IMAGE_VERSION = "1";
/** Floci no asume el rol: basta un ARN con forma de rol. */
export const LOCAL_BUILD_ROLE_ARN = "arn:aws:iam::000000000000:role/rayito-local-build";
const LOCAL_ARTIFACT_KEY = "rayito-local/image.zip";
export const TEST_SANDBOX_TIMEOUT_MS = 900_000;
const PRODUCT_DOCKERFILE = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../../../image/Dockerfile",
);
const DEFAULT_REGION = "us-east-1";

export function localEnabled(): boolean {
  return Boolean(process.env[LOCAL_GUEST_VAR]);
}

export interface LocalSettings {
  readonly address: GuestAddress;
  readonly region: string;
  readonly artifactBucket: string;
}

export function localSettings(): LocalSettings {
  const raw = process.env[LOCAL_GUEST_VAR];
  if (!raw) {
    throw new Error(`los tests local requieren ${LOCAL_GUEST_VAR} (make local-up)`);
  }
  return {
    address: GuestAddress.parse(raw),
    region: process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || DEFAULT_REGION,
    artifactBucket: process.env[ARTIFACT_BUCKET_VAR] || DEFAULT_ARTIFACT_BUCKET,
  };
}

/** El zip de la imagen con el Dockerfile de producto: `Template.build` lo lee para componer el suyo, y Floci no construye nada. */
function imageArtifact(): Uint8Array {
  return writeZip([["Dockerfile", readFileSync(PRODUCT_DOCKERFILE)]]);
}

/** El bucket de artefactos y el registro de la imagen `rayito-local` en Floci; idempotente. */
export async function seedLocalImage(settings: LocalSettings): Promise<string> {
  const s3 = new S3Client({ region: settings.region, forcePathStyle: true });
  try {
    await s3.send(new CreateBucketCommand({ Bucket: settings.artifactBucket }));
  } catch (error) {
    if ((error as { name?: string }).name !== "BucketAlreadyOwnedByYou") {
      throw error;
    }
  }
  await s3.send(
    new PutObjectCommand({
      Bucket: settings.artifactBucket,
      Key: LOCAL_ARTIFACT_KEY,
      Body: imageArtifact(),
    }),
  );
  const microvms = new LambdaMicrovmsClient({ region: settings.region });
  try {
    const existing = await microvms.send(
      new GetMicrovmImageCommand({ imageIdentifier: LOCAL_IMAGE_NAME }),
    );
    return existing.imageArn as string;
  } catch (error) {
    if ((error as { name?: string }).name !== "ResourceNotFoundException") {
      throw error;
    }
  }
  const created = await microvms.send(
    new CreateMicrovmImageCommand({
      name: LOCAL_IMAGE_NAME,
      codeArtifact: { uri: `s3://${settings.artifactBucket}/${LOCAL_ARTIFACT_KEY}` },
      buildRoleArn: LOCAL_BUILD_ROLE_ARN,
      baseImageArn: `arn:aws:lambda:${settings.region}:aws:microvm-image:${MANAGED_BASE_IMAGE}`,
      baseImageVersion: BASE_IMAGE_VERSION,
    }),
  );
  return created.imageArn as string;
}

export interface LocalHarness {
  readonly settings: LocalSettings;
  readonly controlPlane: LocalGuestControlPlane;
  readonly templateArn: string;
}

export async function localHarness(): Promise<LocalHarness> {
  const settings = localSettings();
  const inner = LambdaMicrovmsControlPlane.fromRegion(settings.region);
  return {
    settings,
    controlPlane: new LocalGuestControlPlane(inner, settings.address),
    templateArn: await seedLocalImage(settings),
  };
}

/** `create()` como lo llama un e2e, con el plano y el transporte locales. */
export function createLocalSandbox(
  harness: LocalHarness,
  options: SandboxCreateOptions = {},
): Promise<Sandbox> {
  return Sandbox.create({
    timeoutMs: TEST_SANDBOX_TIMEOUT_MS,
    ...options,
    template: harness.templateArn,
    controlPlane: harness.controlPlane,
    transport: harness.settings.address.transport(),
  });
}

export async function killQuietly(sandbox: Sandbox): Promise<void> {
  try {
    await sandbox.kill();
  } catch (error) {
    if (!(error instanceof SandboxNotFoundError)) {
      throw error;
    }
  }
}

export async function releaseGuest(harness: LocalHarness): Promise<void> {
  const live = harness.controlPlane.liveSandboxId;
  if (live !== undefined) {
    await harness.controlPlane.terminateMicrovm(live);
  }
}
