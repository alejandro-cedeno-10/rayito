/**
 * Eventos de ciclo de vida y webhooks contra Floci (`make local-e2e`), espejo
 * de `clients/python/tests/local/test_local_events.py`: `LifecycleEvents`
 * despliega la pila `events-webhooks` con CloudFormation, gestiona los
 * webhooks en DynamoDB y `create({ events })` entrega la clave derivada al
 * `rayd` del guest. El forwarder y el deliverer son código Lambda compartido
 * (`infra/lambdas/events_webhooks`, en Python) y su camino firmado se prueba
 * una sola vez en el test de Python; aquí se cubre lo que es código propio
 * del SDK TypeScript.
 */

import { randomBytes } from "node:crypto";
import { CloudWatchLogsClient, CreateLogGroupCommand } from "@aws-sdk/client-cloudwatch-logs";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { LifecycleEvents, SecretStore } from "../../src/index.js";
import { WEBHOOK_SECRET_PREFIX } from "../../src/secrets/names.js";
import {
  createLocalSandbox,
  killQuietly,
  type LocalHarness,
  localEnabled,
  localHarness,
  releaseGuest,
} from "./helpers.js";

const KILLED_TYPE = "sandbox.lifecycle.killed";
const LOG_GROUP = "/aws/lambda-microvms/rayito-local";
/** `events` exige logs de CloudWatch y, con ellos, un rol de ejecución; Floci no lo asume. */
const LOCAL_EXECUTION_ROLE_ARN = "arn:aws:iam::000000000000:role/rayito-local-sandbox";
const WEBHOOK_URL = "https://receiver.example.com/hook";
const RANDOM_SUFFIX_BYTES = 4;
const SECRET_BYTES = 16;

async function ensureLogGroup(region: string): Promise<void> {
  const logs = new CloudWatchLogsClient({ region });
  try {
    await logs.send(new CreateLogGroupCommand({ logGroupName: LOG_GROUP }));
  } catch (error) {
    if ((error as { name?: string }).name !== "ResourceAlreadyExistsException") {
      throw error;
    }
  }
}

describe.skipIf(!localEnabled())("eventos de ciclo de vida contra Floci", () => {
  let harness: LocalHarness;

  beforeAll(async () => {
    harness = await localHarness();
  });

  afterAll(async () => {
    if (harness !== undefined) {
      await releaseGuest(harness);
    }
  });

  it("despliega la pila, registra el webhook y lanza un sandbox con events", async () => {
    await ensureLogGroup(harness.settings.region);
    const suffix = randomBytes(RANDOM_SUFFIX_BYTES).toString("hex");
    const events = new LifecycleEvents({
      stackName: `rayito-local-ts-events-${suffix}`,
      region: harness.settings.region,
    });
    await events.deploy({
      artifactBucket: harness.settings.artifactBucket,
      logGroupName: LOG_GROUP,
    });
    const secrets = new SecretStore({
      region: harness.settings.region,
      prefix: WEBHOOK_SECRET_PREFIX,
    });
    const secretName = `local-${suffix}`;
    await secrets.create(secretName, randomBytes(SECRET_BYTES).toString("hex"));
    try {
      const webhook = await events.registerWebhook(WEBHOOK_URL, {
        secretName,
        types: [KILLED_TYPE],
      });
      expect((await events.listWebhooks()).map((item) => item.webhookId)).toContain(
        webhook.webhookId,
      );

      const sandbox = await createLocalSandbox(harness, {
        events,
        logging: "cloudwatch",
        executionRoleArn: LOCAL_EXECUTION_ROLE_ARN,
      });
      await killQuietly(sandbox);

      expect(await events.getEvents({ sandboxId: sandbox.sandboxId })).toEqual([]);
      await events.deleteWebhook(webhook.webhookId);
      expect((await events.listWebhooks()).map((item) => item.webhookId)).not.toContain(
        webhook.webhookId,
      );
    } finally {
      await secrets.destroy(secretName);
      await events.destroy();
    }
    expect(await events.status()).toBeUndefined();
  });
});
