/**
 * Funciones opcionales contra Floci (`make local-e2e`), espejo de
 * `clients/python/tests/local/test_local_features.py`: `OptionalStacks`
 * (CloudFormation), el índice de metadatos (DynamoDB), `SecretStore` y
 * `secrets` (Secrets Manager + el guest) y `Template.build` (S3 +
 * `create-microvm-image`). Lo que mide coste, cuotas o IAM real sigue en los
 * e2e contra AWS (`tests/e2e`).
 */

import { randomBytes } from "node:crypto";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  DynamoDbIndex,
  OptionalStacks,
  Sandbox,
  SecretCache,
  SecretStore,
  Template,
} from "../../src/index.js";
import {
  createLocalSandbox,
  killQuietly,
  LOCAL_BUILD_ROLE_ARN,
  LOCAL_IMAGE_NAME,
  type LocalHarness,
  localEnabled,
  localHarness,
  releaseGuest,
} from "./helpers.js";

const DEPLOYED = "CREATE_COMPLETE";
/** Lo mínimo que cada componente necesita; los ARN son de Floci (cuenta 000000000000). */
const COMPONENT_PARAMETERS: Readonly<Record<string, Readonly<Record<string, string>>>> = {
  "metadata-index": {},
  "secrets-access": {},
  "otlp-export": {},
  "s3-mounts": { BucketName: "rayito-local-mounts" },
  "sizes-guard": {
    ImageArns: `arn:aws:lambda:us-east-1:000000000000:microvm-image:${LOCAL_IMAGE_NAME}`,
  },
  templates: {
    ArtifactBucketArn: "arn:aws:s3:::rayito-local-artifacts",
    BuildRoleArn: LOCAL_BUILD_ROLE_ARN,
  },
};

function runSuffix(): string {
  return randomBytes(4).toString("hex");
}

describe.skipIf(!localEnabled())("funciones opcionales contra Floci", () => {
  let harness: LocalHarness;

  beforeAll(async () => {
    harness = await localHarness();
  });

  afterAll(async () => {
    if (harness !== undefined) {
      await releaseGuest(harness);
    }
  });

  it.each(Object.keys(COMPONENT_PARAMETERS))("pila %s: deploy, status y destroy", async (name) => {
    const stacks = new OptionalStacks({ region: harness.settings.region });
    const stackName = `rayito-local-ts-${name}-${runSuffix()}`;
    const deployed = await stacks.deploy(name, {
      stackName,
      parameters: COMPONENT_PARAMETERS[name] ?? {},
    });
    try {
      expect(deployed.state).toBe(DEPLOYED);
      expect((await stacks.status(name, { stackName }))?.outputs).toEqual(deployed.outputs);
    } finally {
      await stacks.destroy(name, { stackName });
    }
    expect(await stacks.status(name, { stackName })).toBeUndefined();
  });

  it("el índice de metadatos lista por metadatos", async () => {
    const stacks = new OptionalStacks({ region: harness.settings.region });
    const stackName = `rayito-local-ts-index-${runSuffix()}`;
    const table = `rayito-local-ts-${runSuffix()}`;
    await stacks.deploy("metadata-index", { stackName, parameters: { TableName: table } });
    try {
      const index = new DynamoDbIndex({ tableName: table, region: harness.settings.region });
      const run = runSuffix();
      const sandbox = await createLocalSandbox(harness, {
        metadata: { suite: "local", run },
        index,
      });
      try {
        const found = [];
        for await (const item of Sandbox.list({
          metadata: { run },
          index,
          controlPlane: harness.controlPlane,
        })) {
          found.push(item);
        }
        expect(found.map((item) => item.sandboxId)).toEqual([sandbox.sandboxId]);
      } finally {
        await killQuietly(sandbox);
      }
    } finally {
      await stacks.destroy("metadata-index", { stackName });
    }
  });

  it("SecretStore e inyección de secrets", async () => {
    const store = new SecretStore({ region: harness.settings.region });
    const name = `local-ts-${runSuffix()}`;
    const value = `sentinel-${randomBytes(8).toString("hex")}`;
    expect((await store.create(name, value)).version).toBe(1);
    try {
      const sandbox = await createLocalSandbox(harness, {
        secrets: { RAYITO_LOCAL_SECRET: name },
        secretCache: new SecretCache({ store }),
      });
      try {
        const printed = await sandbox.commands.run("printenv RAYITO_LOCAL_SECRET");
        expect(printed.stdout.trim()).toBe(value);
      } finally {
        await killQuietly(sandbox);
      }
    } finally {
      expect(await store.destroy(name)).toBe(true);
    }
  });

  it("Template.build sube el artefacto y crea la imagen", async () => {
    const template = new Template()
      .fromBaseImage(LOCAL_IMAGE_NAME)
      .pipInstall(["httpx"])
      .setEnvs({ RAYITO_LOCAL_TEMPLATE: "1" });
    const name = `rayito-local-ts-tpl-${runSuffix()}`;
    // `force` en el primero: Floci 2.1.0 responde ResourceNotFoundException a
    // ListMicrovmImageVersions de una imagen que aún no existe
    // (docs/research/2026-10-local-testing.md).
    const options = { bucket: harness.settings.artifactBucket, region: harness.settings.region };
    const first = await Template.build(template, name, { ...options, force: true });
    expect(first.templateId.endsWith(`:microvm-image:${name}`)).toBe(true);
    expect(await Template.exists(name, { region: harness.settings.region })).toBe(true);
    const reused = await Template.build(template, name, options);
    expect(reused.templateId).toBe(first.templateId);
  });
});
