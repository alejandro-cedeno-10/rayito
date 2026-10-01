/**
 * `SecretStore`: codificación de versiones y metadatos, los parámetros de
 * AWS_API_NOTES.md §19, el reintento de `create` tras un borrado y que ningún
 * error repite el nombre ni el valor. Espejo de `test_secrets_store.py`.
 */

import { afterEach, describe, expect, test, vi } from "vitest";
import {
  InvalidArgumentError,
  RateLimitError,
  SandboxError,
  SecretError,
  SecretNotFoundError,
} from "../../src/errors.js";
import {
  currentVersion,
  DESCRIPTION_MAX_CHARS,
  decodeMetadata,
  encodeMetadata,
  latestVersion,
  METADATA_PREFIX,
  resolveSecretId,
  SecretRef,
  versionFromId,
  versionToken,
} from "../../src/secrets/names.js";
import {
  CREATE_RETRY_BUDGET_MS,
  CREATE_RETRY_JITTER,
  SecretStore,
  UPDATE_FREQUENCY_WARNING,
} from "../../src/secrets/store.js";
import {
  arnFor,
  awsError,
  FakeSecretsManager,
  SENTINEL_NAME,
  SENTINEL_VALUE,
} from "./secrets-fake.js";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("pure encoding", () => {
  test("the version token is 42 chars, inside ClientRequestToken's 32-64", () => {
    const token = versionToken(7);
    expect(token).toBe("rayito-secret-version-00000000000000000007");
    expect(token.length).toBe(42);
    expect(versionFromId(token)).toBe(7);
    expect(versionFromId("f3a1c2d4-uuid-from-console")).toBe(0);
    expect(() => versionToken(0)).toThrow(InvalidArgumentError);
  });

  test("the current version comes from AWSCURRENT", () => {
    expect(
      currentVersion({ [versionToken(1)]: ["AWSPREVIOUS"], [versionToken(2)]: ["AWSCURRENT"] }),
    ).toBe(2);
    expect(currentVersion(undefined)).toBe(0);
  });

  test("metadata round-trips as compact prefixed JSON and is capped at 2048 chars", () => {
    const encoded = encodeMetadata({ b: "2", a: "1" });
    expect(encoded).toBe(`${METADATA_PREFIX}{"a":"1","b":"2"}`);
    expect(decodeMetadata(encoded)).toEqual({ a: "1", b: "2" });
    expect(decodeMetadata("escrita a mano")).toEqual({});
    expect(() => encodeMetadata({ k: "x".repeat(DESCRIPTION_MAX_CHARS) })).toThrow(
      InvalidArgumentError,
    );
  });

  test("names resolve under the prefix, ARNs pass through, errors never repeat the name", () => {
    expect(resolveSecretId("openai", "rayito/")).toBe("rayito/openai");
    expect(resolveSecretId("openai", "")).toBe("openai");
    expect(resolveSecretId(arnFor("rayito/a"), "rayito/")).toBe(arnFor("rayito/a"));
    expect(() => resolveSecretId(`${SENTINEL_NAME} spaced`, "rayito/")).toThrow(
      expect.not.objectContaining({ message: expect.stringContaining(SENTINEL_NAME) }),
    );
    expect(() => new SecretRef("a", { versionId: "x", versionStage: "AWSCURRENT" })).toThrow(
      InvalidArgumentError,
    );
  });
});

describe("SecretStore over a fake client", () => {
  test("create sends exactly the documented parameters", async () => {
    const api = new FakeSecretsManager();
    const store = new SecretStore({ client: api, kmsKeyId: "alias/rayito" });
    const info = await store.create("openai", SENTINEL_VALUE, { metadata: { team: "ml" } });
    expect(api.requests).toEqual([
      [
        "CreateSecret",
        {
          Name: "rayito/openai",
          SecretString: SENTINEL_VALUE,
          Description: `${METADATA_PREFIX}{"team":"ml"}`,
          ClientRequestToken: versionToken(1),
          KmsKeyId: "alias/rayito",
        },
      ],
    ]);
    expect(info).toMatchObject({
      secretId: arnFor("rayito/openai"),
      name: "openai",
      version: 1,
      metadata: { team: "ml" },
    });
  });

  test("update describes then puts version n+1 and replaces metadata", async () => {
    vi.spyOn(process, "emitWarning").mockImplementation(() => undefined);
    const api = new FakeSecretsManager();
    const store = new SecretStore({ client: api });
    await store.create("a", "v1");
    const info = await store.update("a", "v2", { metadata: { team: "ops" } });
    expect(info.version).toBe(2);
    expect(api.requests.slice(1).map(([op]) => op)).toEqual([
      "DescribeSecret",
      "PutSecretValue",
      "UpdateSecret",
    ]);
    expect(api.requests[2]?.[1]).toEqual({
      SecretId: "rayito/a",
      SecretString: "v2",
      ClientRequestToken: versionToken(2),
    });
    expect((await store.getInfo("a")).metadata).toEqual({ team: "ops" });
  });

  test("update after an external rotation writes past the highest Rayito version", async () => {
    vi.spyOn(process, "emitWarning").mockImplementation(() => undefined);
    const api = new FakeSecretsManager();
    const store = new SecretStore({ client: api });
    await store.create("a", "v1");
    await store.update("a", "v2");
    api.put("rayito/a", "rotated-outside", "c0ffee00-0000-4000-8000-000000000000");
    const info = await store.update("a", "v3");
    expect(info.version).toBe(3);
    expect(api.requests.at(-1)?.[1]).toMatchObject({ ClientRequestToken: versionToken(3) });
  });

  test("latestVersion is the highest Rayito token with any label", () => {
    const stages = {
      "c0ffee00-0000-4000-8000-000000000000": ["AWSCURRENT"],
      [versionToken(2)]: ["AWSPREVIOUS"],
      [versionToken(1)]: [],
    };
    expect(currentVersion(stages)).toBe(0);
    expect(latestVersion(stages)).toBe(2);
    expect(latestVersion({})).toBe(0);
    expect(latestVersion(undefined)).toBe(0);
  });

  test("list filters by the prefix and paginates; destroy is a forced delete", async () => {
    const api = new FakeSecretsManager();
    const store = new SecretStore({ client: api });
    for (const name of ["a", "b", "c"]) {
      await store.create(name, "v");
    }
    const first = await store.list({ limit: 2 });
    expect(first.items.map((item) => item.name)).toEqual(["a", "b"]);
    expect(api.requests.at(-1)?.[1]).toEqual({
      IncludePlannedDeletion: false,
      Filters: [{ Key: "name", Values: ["rayito/"] }],
      MaxResults: 2,
    });
    const second = await store.list({ limit: 2, nextToken: first.nextToken });
    expect(second.items.map((item) => item.name)).toEqual(["c"]);
    expect(second.nextToken).toBeUndefined();
    expect(await store.destroy("a")).toBe(true);
    expect(api.requests.at(-1)?.[1]).toEqual({
      SecretId: "rayito/a",
      ForceDeleteWithoutRecovery: true,
    });
    expect(await store.destroy("a")).toBe(false);
    expect(await store.exists("a")).toBe(false);
  });

  test("an empty prefix lists without a name filter", async () => {
    const api = new FakeSecretsManager();
    await new SecretStore({ client: api, prefix: "" }).list();
    expect(api.requests.at(-1)?.[1]).toEqual({ IncludePlannedDeletion: false });
  });

  test("create retries while the name is scheduled for deletion, with the same token", async () => {
    const api = new FakeSecretsManager();
    let failures = 2;
    const real = api.createSecret.bind(api);
    vi.spyOn(api, "createSecret").mockImplementation(async (input) => {
      if (failures > 0) {
        failures -= 1;
        throw scheduledForDeletion();
      }
      return real(input);
    });
    const sleeps: number[] = [];
    const store = new SecretStore({
      client: api,
      random: () => 0.5,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
    });
    expect((await store.create("a", "v")).version).toBe(1);
    expect(sleeps).toEqual([500, 1000]);
  });

  test("the create retry budget is 60 s with 25 % jitter", () => {
    expect(CREATE_RETRY_BUDGET_MS).toBe(60_000);
    expect(CREATE_RETRY_JITTER).toBe(0.25);
  });

  test("create gives up after the 60 s budget without naming the secret", async () => {
    const api = new FakeSecretsManager();
    vi.spyOn(api, "createSecret").mockRejectedValue(
      awsError("InvalidRequestException", `${SENTINEL_NAME} scheduled for deletion`),
    );
    const clock = fakeClock(api);
    const error = await clock.store.create(SENTINEL_NAME, SENTINEL_VALUE).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(SecretError);
    expect((error as SecretError).awsCode).toBe("InvalidRequestException");
    expect(clock.now()).toBeCloseTo(CREATE_RETRY_BUDGET_MS);
    expect(String(error)).toContain("60 s");
    expect(String(error)).not.toContain(SENTINEL_NAME);
    expect(String((error as Error).cause)).not.toContain(SENTINEL_NAME);
  });

  // Aceptación de 0.5.0: AWS liberó el nombre tras 19,3-27,9 s; con 30 s y el
  // backoff `create` fallaba. Con 60 s y cualquier jitter lo recrea.
  for (const random of [0, 0.5, 1]) {
    for (const freedAfterMs of [19_300, 26_800, 27_900, 45_000]) {
      test(`create outlasts a name freed after ${freedAfterMs} ms (random ${random})`, async () => {
        const api = new FakeSecretsManager();
        const clock = fakeClock(api, random);
        const real = api.createSecret.bind(api);
        vi.spyOn(api, "createSecret").mockImplementation(async (input) => {
          if (clock.now() < freedAfterMs) {
            throw scheduledForDeletion();
          }
          return real(input);
        });
        expect((await clock.store.create("a", "v")).version).toBe(1);
        expect(clock.now()).toBeGreaterThanOrEqual(freedAfterMs);
        expect(clock.now()).toBeLessThanOrEqual(CREATE_RETRY_BUDGET_MS);
      });
    }
  }

  test("create retry delays are jittered by 25 % and capped", async () => {
    const api = new FakeSecretsManager();
    let failures = 7;
    const real = api.createSecret.bind(api);
    vi.spyOn(api, "createSecret").mockImplementation(async (input) => {
      if (failures > 0) {
        failures -= 1;
        throw scheduledForDeletion();
      }
      return real(input);
    });
    const randoms = [0, 1, 0, 1, 0, 1, 0];
    const sleeps: number[] = [];
    const store = new SecretStore({
      client: api,
      random: () => randoms.shift() ?? 0.5,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
    });
    await store.create("a", "v");
    expect(sleeps).toEqual([375, 1250, 1500, 5000, 6000, 10_000, 6000]);
  });

  test("destroy of a name that never existed is false without deleting", async () => {
    const api = new FakeSecretsManager();
    const store = new SecretStore({ client: api });
    expect(await store.destroy(SENTINEL_NAME)).toBe(false);
    expect(api.count("DeleteSecret")).toBe(0);
    await store.create("a", "v");
    expect(await store.destroy("a")).toBe(true);
    expect(await store.destroy("a")).toBe(false);
    expect(api.requests.map(([op]) => op).filter((op) => op !== "CreateSecret")).toEqual([
      "DescribeSecret",
      "DescribeSecret",
      "DeleteSecret",
      "DescribeSecret",
    ]);
  });

  test("destroy of a secret scheduled for deletion is false without deleting", async () => {
    const api = new FakeSecretsManager();
    vi.spyOn(api, "describeSecret").mockResolvedValue({
      ARN: arnFor("rayito/a"),
      Name: "rayito/a",
      DeletedDate: new Date(),
    });
    expect(await new SecretStore({ client: api }).destroy("a")).toBe(false);
    expect(api.count("DeleteSecret")).toBe(0);
  });

  test("destroy that loses a race with another delete is false", async () => {
    const api = new FakeSecretsManager();
    const store = new SecretStore({ client: api });
    await store.create("a", "v");
    vi.spyOn(api, "deleteSecret").mockRejectedValue(
      awsError("ResourceNotFoundException", "not found"),
    );
    expect(await store.destroy("a")).toBe(false);
  });

  test("destroy propagates other errors without the name", async () => {
    const api = new FakeSecretsManager();
    const store = new SecretStore({ client: api });
    await store.create(SENTINEL_NAME, "v");
    vi.spyOn(api, "deleteSecret").mockRejectedValue(
      awsError("AccessDeniedException", `no access to ${SENTINEL_NAME}`),
    );
    const error = await store.destroy(SENTINEL_NAME).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(SecretError);
    expect(String(error)).toContain("secretsmanager:DeleteSecret");
    expect(String(error)).not.toContain(SENTINEL_NAME);
  });

  test("not found is SecretNotFoundError (a SecretError and SandboxError) without the name", async () => {
    const store = new SecretStore({ client: new FakeSecretsManager() });
    const error = await store.getInfo(SENTINEL_NAME).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(SecretNotFoundError);
    expect(error).toBeInstanceOf(SecretError);
    expect(error).toBeInstanceOf(SandboxError);
    expect(String(error)).not.toContain(SENTINEL_NAME);
    expect(String((error as Error).cause)).not.toContain(SENTINEL_NAME);
  });

  test.each([
    ["ThrottlingException", RateLimitError],
    ["AccessDeniedException", SecretError],
    ["LimitExceededException", SecretError],
    ["ResourceExistsException", SecretError],
    ["InternalServiceError", SecretError],
  ])("%s maps by code, never by message", async (code, expected) => {
    const api = new FakeSecretsManager();
    vi.spyOn(api, "describeSecret").mockRejectedValue(awsError(code, `about ${SENTINEL_NAME}`));
    const error = await new SecretStore({ client: api })
      .getInfo(SENTINEL_NAME)
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(expected);
    expect((error as SecretError).awsCode).toBe(code);
    expect(String(error)).not.toContain(SENTINEL_NAME);
  });

  test("updating more often than every 600 s warns once, without the name", async () => {
    const warn = vi.spyOn(process, "emitWarning").mockImplementation(() => undefined);
    const api = new FakeSecretsManager();
    let now = 1_000_000;
    const store = new SecretStore({ client: api, region: "eu-west-9", now: () => now });
    await store.create(SENTINEL_NAME, "v1");
    await store.update(SENTINEL_NAME, "v2");
    now += 10_000;
    await store.update(SENTINEL_NAME, "v3");
    now += 10_000;
    await store.update(SENTINEL_NAME, "v4");
    const frequency = warn.mock.calls.filter(([message]) => message === UPDATE_FREQUENCY_WARNING);
    expect(frequency).toHaveLength(1);
    expect(String(frequency[0]?.[0])).not.toContain(SENTINEL_NAME);
    expect((await store.getInfo(SENTINEL_NAME)).version).toBe(4);
  });

  test("values and arguments are validated before calling AWS", async () => {
    const api = new FakeSecretsManager();
    const store = new SecretStore({ client: api });
    await expect(store.create("a", "")).rejects.toBeInstanceOf(InvalidArgumentError);
    await expect(store.create("a", "x".repeat(65_537))).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await expect(store.create(arnFor("rayito/a"), "v")).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await expect(store.list({ limit: 0 })).rejects.toBeInstanceOf(InvalidArgumentError);
    expect(api.calls.size).toBe(0);
  });

  test("JSON and version selectors never carry a value", async () => {
    const store = new SecretStore({ client: new FakeSecretsManager(), region: "us-east-1" });
    await store.create("a", SENTINEL_VALUE);
    expect(JSON.stringify(store)).not.toContain(SENTINEL_VALUE);
    const read = await store.readValue(new SecretRef("a"));
    expect(read).toBe(SENTINEL_VALUE);
  });
});

function scheduledForDeletion(): Error {
  return awsError(
    "InvalidRequestException",
    "You can't create this secret because a secret with this name is already scheduled for deletion.",
  );
}

function fakeClock(
  api: FakeSecretsManager,
  random = 0.5,
): { readonly store: SecretStore; now(): number } {
  let now = 0;
  const store = new SecretStore({
    client: api,
    random: () => random,
    now: () => now,
    sleep: async (ms) => {
      now += ms;
    },
  });
  return { store, now: () => now };
}
