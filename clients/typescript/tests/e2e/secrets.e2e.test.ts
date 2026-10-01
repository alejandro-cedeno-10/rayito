/**
 * M13a contra AWS real (`RAYITO_E2E=1`): CRUD por el `Secret` de
 * `rayito/e2b` sobre Secrets Manager → `Sandbox.create({ secrets })` →
 * `printenv` tres veces con UNA sola `GetSecretValue` (contada con un
 * middleware del cliente del SDK v3) → `destroy`. SEC-10: el valor no aparece
 * en los logs del SDK. Coste: un secreto durante < 5 min (≈ $0,0005) + ~10
 * llamadas a Secrets Manager + un sandbox (~$0,03).
 */

import { randomBytes } from "node:crypto";
import { SecretsManager } from "@aws-sdk/client-secrets-manager";
import { describe, expect, test } from "vitest";
import { Secret } from "../../src/e2b/index.js";
import { Sandbox, SecretCache, SecretStore } from "../../src/index.js";
import { e2eEnabled, TEST_SANDBOX_TIMEOUT_MS, useE2E } from "./helpers.js";

describe.runIf(e2eEnabled())("M13a secrets (AWS real)", () => {
  const e2e = useE2E();

  test("CRUD por el shim, inyección con una sola lectura, sin el valor en los logs", async () => {
    const region = e2e.controlPlane.region;
    const client = new SecretsManager({ region });
    let reads = 0;
    client.middlewareStack.add(
      (next, context) => async (args) => {
        if (context.commandName === "GetSecretValueCommand") {
          reads += 1;
        }
        return next(args);
      },
      { step: "initialize", name: "rayitoCountReads" },
    );
    const name = `e2e-${randomBytes(6).toString("hex")}`;
    const value = `sentinel-${randomBytes(16).toString("hex")}`;
    const logged: string[] = [];
    const logger = {
      debug: (message: string, fields?: unknown) => logged.push(message, JSON.stringify(fields)),
      info: (message: string, fields?: unknown) => logged.push(message, JSON.stringify(fields)),
      warn: (message: string, fields?: unknown) => logged.push(message, JSON.stringify(fields)),
      error: (message: string, fields?: unknown) => logged.push(message, JSON.stringify(fields)),
    };
    const info = await Secret.create(name, value, { region, client, metadata: { suite: "m13a" } });
    try {
      expect(info.version).toBe(1);
      const secretCache = new SecretCache({ store: new SecretStore({ region, client }) });
      const sandbox = await Sandbox.create({
        template: e2e.templateArn,
        timeoutMs: TEST_SANDBOX_TIMEOUT_MS,
        idle: null,
        executionRoleArn: e2e.settings.executionRoleArn,
        logging: e2e.settings.logging,
        controlPlane: e2e.controlPlane,
        secrets: { RAYITO_E2E_SECRET: name },
        secretCache,
        logger,
      });
      e2e.created.push(sandbox);
      for (let index = 0; index < 3; index += 1) {
        const result = await sandbox.commands.run("printenv RAYITO_E2E_SECRET");
        expect(result.stdout.trim()).toBe(value);
      }
      expect(reads).toBe(1);
      await sandbox.kill();
      expect((await Secret.update(name, `${value}-2`, { region, client })).version).toBe(2);
    } finally {
      expect(await Secret.destroy(name, { region, client })).toBe(true);
    }
    const everything = logged.join("\n");
    expect(everything).not.toContain(value);
    expect(everything).not.toContain(name);
  });
});
