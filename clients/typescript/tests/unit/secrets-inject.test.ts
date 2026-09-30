/**
 * `secrets` del SDK TypeScript contra el `rayd` falso: el valor llega a
 * `StartRequest.envs`, `PtyStart.envs`, `ExecuteRequest.envs` y
 * `CreateContextRequest.envs`; tres comandos hacen una sola lectura; nunca
 * viaja en el `runHookPayload`; sin `secrets` no se carga el peer opcional.
 * Espejo de `test_secrets_inject_sync.py` y `test_secrets_pool.py`.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { InvalidArgumentError, SecretNotFoundError } from "../../src/errors.js";
import * as optional from "../../src/optional.js";
import { SandboxPool } from "../../src/pool/pool.js";
import { S3Prefix } from "../../src/sandbox/persistence.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { SecretCache } from "../../src/secrets/cache.js";
import {
  codeSecretsScope,
  normalizeSecrets,
  resetVisibilityWarningForTests,
  SECRET_VISIBILITY_WARNING,
  sharedSecretCache,
  sharedSecretCacheCount,
} from "../../src/secrets/inject.js";
import { SecretStore } from "../../src/secrets/store.js";
import { IMAGE_ARN } from "./fake/control-plane.js";
import { FakeClock, FakePoolControlPlane } from "./fake/pool-plane.js";
import { createTestSandbox, waitUntil } from "./helpers.js";
import { FakeSecretsManager, SENTINEL_NAME, SENTINEL_VALUE } from "./secrets-fake.js";

const GH_VALUE = "ghp-SECOND-SENTINEL";

function secretRig() {
  const api = new FakeSecretsManager();
  api.put(`rayito/${SENTINEL_NAME}`, SENTINEL_VALUE);
  api.put("rayito/gh", GH_VALUE);
  const cache = new SecretCache({ store: new SecretStore({ client: api }) });
  return { api, cache };
}

async function sandboxWithSecrets() {
  const { api, cache } = secretRig();
  const test = await createTestSandbox({
    create: {
      envs: { PLAIN: "1" },
      secrets: { OPENAI_API_KEY: SENTINEL_NAME },
      secretCache: cache,
    },
  });
  return { ...test, api, cache };
}

beforeEach(() => {
  vi.spyOn(process, "emitWarning").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("secrets on the native Sandbox", () => {
  test("three commands make one Secrets Manager read", async () => {
    const { sandbox, rayd, api } = await sandboxWithSecrets();
    for (let index = 0; index < 3; index += 1) {
      const result = await sandbox.commands.run("env");
      expect(result.stdout).toContain(`OPENAI_API_KEY=${SENTINEL_VALUE}\n`);
    }
    expect(api.count("GetSecretValue")).toBe(1);
    for (const request of rayd.process.startRequests.slice(-3)) {
      expect(request.process?.envs.OPENAI_API_KEY).toBe(SENTINEL_VALUE);
    }
  });

  test("the runHookPayload never carries the value, the name or the variable", async () => {
    const { plane } = await sandboxWithSecrets();
    const payload = plane.launches[0]?.runHookPayload ?? "";
    expect(payload).toContain('"PLAIN"');
    expect(payload).not.toContain(SENTINEL_VALUE);
    expect(payload).not.toContain(SENTINEL_NAME);
    expect(payload).not.toContain("OPENAI_API_KEY");
  });

  test("call secrets merge with the handle's and the call wins", async () => {
    const { sandbox, rayd } = await sandboxWithSecrets();
    await sandbox.commands.run("env", { secrets: { GH_TOKEN: "gh" } });
    const envs = rayd.process.startRequests.at(-1)?.process?.envs ?? {};
    expect(envs.GH_TOKEN).toBe(GH_VALUE);
    expect(envs.OPENAI_API_KEY).toBe(SENTINEL_VALUE);
    await sandbox.commands.run("env", { secrets: { OPENAI_API_KEY: "gh" } });
    expect(rayd.process.startRequests.at(-1)?.process?.envs.OPENAI_API_KEY).toBe(GH_VALUE);
  });

  test("a key in both envs and secrets is rejected before starting", async () => {
    const { sandbox, rayd } = await sandboxWithSecrets();
    const before = rayd.process.startRequests.length;
    const error = await sandbox.commands
      .run("env", { envs: { OPENAI_API_KEY: "shadow" } })
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(InvalidArgumentError);
    expect(String(error)).toContain("OPENAI_API_KEY");
    expect(String(error)).not.toContain(SENTINEL_NAME);
    expect(rayd.process.startRequests.length).toBe(before);
  });

  test("pty, runCode and contexts carry the secrets", async () => {
    const { sandbox, rayd, api } = await sandboxWithSecrets();
    const terminal = await sandbox.pty.create({ timeoutMs: 0, secrets: { GH: "gh" } });
    try {
      const envs = rayd.pty.createRequests.at(-1)?.envs ?? {};
      expect([envs.OPENAI_API_KEY, envs.GH]).toEqual([SENTINEL_VALUE, GH_VALUE]);
    } finally {
      await terminal.kill();
    }
    await sandbox.runCode("1+1");
    expect(rayd.code.executeRequests.at(-1)?.envs).toEqual({ OPENAI_API_KEY: SENTINEL_VALUE });
    const context = await sandbox.createCodeContext({ secrets: { GH: "gh" } });
    expect(rayd.code.createRequests.at(-1)?.envs).toEqual({
      OPENAI_API_KEY: SENTINEL_VALUE,
      GH: GH_VALUE,
    });
    await sandbox.removeCodeContext(context);
    expect(api.count("GetSecretValue")).toBe(2);
  });

  test("runCode secrets on a non-Python language point to createCodeContext", async () => {
    const { sandbox } = await sandboxWithSecrets();
    await expect(
      sandbox.runCode("echo $GH", { language: "bash", secrets: { GH: "gh" } }),
    ).rejects.toThrow(/createCodeContext/);
    await expect(
      sandbox.runCode("echo $GH", {
        context: { id: "ctx-bash", language: "bash", cwd: "/home/user" },
        secrets: { GH: "gh" },
      }),
    ).rejects.toThrow(/createCodeContext/);
    expect(codeSecretsScope(undefined, "bash", undefined)).toBe(false);
    expect(codeSecretsScope("python", undefined, { A: "a" })).toBe(true);
  });

  test("connect rebinds the handle secrets; no options keep them", async () => {
    const { sandbox, rayd, cache } = await sandboxWithSecrets();
    await sandbox.connect({ secrets: { OPENAI_API_KEY: "gh" }, secretCache: cache });
    await sandbox.commands.run("env");
    expect(rayd.process.startRequests.at(-1)?.process?.envs.OPENAI_API_KEY).toBe(GH_VALUE);
    await sandbox.connect();
    await sandbox.commands.run("env");
    expect(rayd.process.startRequests.at(-1)?.process?.envs.OPENAI_API_KEY).toBe(GH_VALUE);
  });

  test("connect with only a secretCache keeps the handle secrets", async () => {
    const { sandbox, rayd, api } = await sandboxWithSecrets();
    const other = new SecretCache({ ttlSeconds: 60, store: new SecretStore({ client: api }) });
    await sandbox.connect({ secretCache: other });
    await sandbox.commands.run("env");
    expect(rayd.process.startRequests.at(-1)?.process?.envs.OPENAI_API_KEY).toBe(SENTINEL_VALUE);
  });

  test("reincarnate relaunches with the secrets bound now, not the create-time ones", async () => {
    const { api, cache } = secretRig();
    const { sandbox } = await createTestSandbox({
      create: {
        executionRoleArn: "arn:aws:iam::123456789012:role/rayito-persist",
        persist: new S3Prefix({ bucket: "my-bucket" }),
        secrets: { OPENAI_API_KEY: SENTINEL_NAME },
        secretCache: cache,
      },
    });
    await sandbox.connect({ secrets: { GH_TOKEN: "gh" }, secretCache: cache });
    const successor = {} as Sandbox;
    const create = vi.spyOn(Sandbox, "create").mockResolvedValue(successor);
    vi.spyOn(sandbox, "checkpointFiles").mockResolvedValue(
      undefined as unknown as Awaited<ReturnType<Sandbox["checkpointFiles"]>>,
    );
    vi.spyOn(sandbox, "kill").mockResolvedValue(true);
    expect(await sandbox.reincarnate()).toBe(successor);
    const relaunch = create.mock.calls[0]?.[0];
    expect(relaunch?.secrets).toEqual({ GH_TOKEN: expect.objectContaining({ name: "gh" }) });
    expect(relaunch?.secretCache).toBe(cache);
    expect(api.count("GetSecretValue")).toBeGreaterThan(0);
  });

  test("a missing secret fails before runMicrovm", async () => {
    const { cache } = secretRig();
    let launches = -1;
    const error = await createTestSandbox({
      create: { secrets: { MISSING: "does-not-exist" }, secretCache: cache },
      beforeCreate: (_rayd, plane) => {
        launches = plane.launches.length;
      },
    }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(SecretNotFoundError);
    expect(String(error)).not.toContain("does-not-exist");
    expect(launches).toBe(0);
  });

  test("without secrets the optional peer is never loaded and no cache is created", async () => {
    const loader = vi.spyOn(optional, "loadOptionalPeer");
    const cachesBefore = sharedSecretCacheCount();
    const { sandbox } = await createTestSandbox();
    await sandbox.commands.run("env");
    await sandbox.runCode("1+1");
    expect(loader).not.toHaveBeenCalled();
    expect(sharedSecretCacheCount()).toBe(cachesBefore);
  });

  test("constructing a SecretStore or SecretCache loads nothing", () => {
    const loader = vi.spyOn(optional, "loadOptionalPeer");
    new SecretStore({ region: "us-east-1" });
    new SecretCache({ region: "us-east-1" });
    expect(loader).not.toHaveBeenCalled();
  });

  test("the shared cache is one per region and credentials", () => {
    const alice = { accessKeyId: "AKIAALICE", secretAccessKey: "a" };
    const bob = { accessKeyId: "AKIABOB", secretAccessKey: "b" };
    expect(sharedSecretCache("us-east-1", alice)).toBe(sharedSecretCache("us-east-1", alice));
    expect(sharedSecretCache("us-east-1", alice)).not.toBe(sharedSecretCache("us-east-1", bob));
    expect(sharedSecretCache("us-east-1", alice)).not.toBe(sharedSecretCache("eu-west-1", alice));
  });

  test("the first use warns once that the value is visible to sandbox code", () => {
    const warn = vi.mocked(process.emitWarning);
    resetVisibilityWarningForTests();
    normalizeSecrets({ A: "a" });
    normalizeSecrets({ B: "b" });
    normalizeSecrets(undefined);
    const visible = warn.mock.calls.filter(([message]) => message === SECRET_VISIBILITY_WARNING);
    expect(visible).toHaveLength(1);
    expect(visible[0]?.[1]).toEqual({ type: "RayitoCompatWarning" });
  });

  test("invalid secrets arguments name the variable, never the secret", () => {
    expect(() => normalizeSecrets({ "": "a" })).toThrow(InvalidArgumentError);
    expect(() => normalizeSecrets({ "A=B": "a" })).toThrow(InvalidArgumentError);
    expect(() => normalizeSecrets({ KEY: 42 as never })).toThrow(/'KEY'/);
  });
});

describe("secrets with a SandboxPool", () => {
  test("take({ secrets }) binds after resolving; slots never carry them", async () => {
    const { api, cache } = secretRig();
    const clock = new FakeClock();
    const plane = new FakePoolControlPlane({ clock });
    const pool = new SandboxPool(
      {
        size: 1,
        template: IMAGE_ARN,
        timeoutMs: 7_200_000,
        minRemainingMs: 3_600_000,
        readyTimeoutMs: 10_000,
      },
      {
        controlPlane: plane,
        transport: { scheme: "http" as const, pingIdleConnection: false },
        monotonic: () => clock.seconds() * 1000,
        sleep: async () => undefined,
        random: () => 0.5,
      },
    );
    try {
      await pool.start();
      await waitUntil(() => pool.stats().ready === 1, 15_000, "el pool no se llenó");
      await expect(
        pool.take({ secrets: { MISSING: "nope" }, secretCache: cache }),
      ).rejects.toBeInstanceOf(SecretNotFoundError);
      expect(pool.stats().ready).toBe(1);
      const readsBefore = api.count("GetSecretValue");
      const sandbox = await pool.take({
        secrets: { OPENAI_API_KEY: SENTINEL_NAME },
        secretCache: cache,
      });
      try {
        for (let index = 0; index < 3; index += 1) {
          expect((await sandbox.commands.run("env")).stdout).toContain(
            `OPENAI_API_KEY=${SENTINEL_VALUE}`,
          );
        }
        expect(api.count("GetSecretValue") - readsBefore).toBe(1);
        for (const record of await pool.backend.load()) {
          expect(JSON.stringify(record)).not.toContain(SENTINEL_VALUE);
          expect(JSON.stringify(record)).not.toContain(SENTINEL_NAME);
        }
        expect(JSON.stringify(plane.calls)).not.toContain(SENTINEL_VALUE);
        expect(JSON.stringify(plane.calls)).not.toContain(SENTINEL_NAME);
      } finally {
        await sandbox.kill();
      }
    } finally {
      plane.releaseAll();
      await pool.close();
      await plane.close();
    }
  });
});
