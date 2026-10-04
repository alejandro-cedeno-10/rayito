/**
 * `SecretCache`: nunca traer el secreto en cada llamada. TTL con reloj falso,
 * `refresh`/`invalidate`, una sola promesa en vuelo por clave, claves
 * distintas por región y credenciales, `ttlSeconds: 0` rechazado, los no
 * encontrados sin cachear y ningún valor en `JSON.stringify` ni `inspect`.
 * Espejo de `test_secrets_cache.py`.
 */

import { inspect } from "node:util";
import { describe, expect, test } from "vitest";
import { InvalidArgumentError, SecretNotFoundError } from "../../src/errors.js";
import { SecretCache } from "../../src/secrets/cache.js";
import { SecretRef } from "../../src/secrets/names.js";
import { SecretStore } from "../../src/secrets/store.js";
import { FakeSecretsManager, SENTINEL_NAME, SENTINEL_VALUE } from "./secrets-fake.js";

function rig(options: { ttlSeconds?: number; region?: string } = {}) {
  const api = new FakeSecretsManager();
  api.put("rayito/openai", SENTINEL_VALUE);
  const clock = { now: 1_000_000 };
  const cache = new SecretCache({
    ttlSeconds: options.ttlSeconds ?? 300,
    store: new SecretStore({ client: api, region: options.region ?? "us-east-1" }),
    now: () => clock.now,
  });
  return { api, clock, cache };
}

describe("SecretCache", () => {
  test("N reads within the TTL make one AWS call", async () => {
    const { api, clock, cache } = rig();
    for (let index = 0; index < 50; index += 1) {
      expect(await cache.get("openai")).toBe(SENTINEL_VALUE);
      clock.now += 5_000;
    }
    expect(api.count("GetSecretValue")).toBe(1);
  });

  test("after the TTL the next read refetches", async () => {
    const { api, clock, cache } = rig({ ttlSeconds: 300 });
    await cache.get("openai");
    clock.now += 299_900;
    await cache.get("openai");
    expect(api.count("GetSecretValue")).toBe(1);
    clock.now += 200;
    await cache.get("openai");
    expect(api.count("GetSecretValue")).toBe(2);
  });

  test("refresh forces a new read and sees the rotated value", async () => {
    const { api, cache } = rig();
    await cache.get("openai");
    api.put("rayito/openai", "rotated", "v2");
    expect(await cache.get("openai")).toBe(SENTINEL_VALUE);
    await cache.refresh("openai");
    expect(api.count("GetSecretValue")).toBe(2);
    expect(await cache.get("openai")).toBe("rotated");
    expect(api.count("GetSecretValue")).toBe(2);
  });

  test("invalidate drops one or all", async () => {
    const { api, cache } = rig();
    api.put("rayito/other", "o");
    await cache.get("openai");
    await cache.get("other");
    cache.invalidate("openai");
    await cache.get("other");
    expect(api.count("GetSecretValue")).toBe(2);
    await cache.get("openai");
    expect(api.count("GetSecretValue")).toBe(3);
    cache.invalidate();
    await cache.get("openai");
    await cache.get("other");
    expect(api.count("GetSecretValue")).toBe(5);
  });

  test("invalidate keeps the in-flight read of other secrets shared", async () => {
    const { api, cache } = rig();
    api.put("rayito/other", "o");
    api.getDelayMs = 50;
    const first = cache.get("other");
    cache.invalidate("openai");
    const second = cache.get("other");
    expect(await Promise.all([first, second])).toEqual(["o", "o"]);
    expect(api.count("GetSecretValue")).toBe(1);
  });

  test("a read forgotten by invalidate does not drop its replacement", async () => {
    const { api, cache } = rig();
    api.getDelayMs = 20;
    const stale = cache.get("openai");
    cache.invalidate("openai");
    api.getDelayMs = 60;
    const fresh = cache.get("openai");
    await stale;
    const joined = cache.get("openai");
    expect(await Promise.all([fresh, joined])).toEqual([SENTINEL_VALUE, SENTINEL_VALUE]);
    expect(api.count("GetSecretValue")).toBe(2);
  });

  test("versions are separate keys; AWSCURRENT is the default", async () => {
    const { api, cache } = rig();
    api.put("rayito/openai", "v2-value", "v2");
    expect(await cache.get(new SecretRef("openai", { versionId: "v1" }))).toBe(SENTINEL_VALUE);
    expect(await cache.get(new SecretRef("openai", { versionId: "v2" }))).toBe("v2-value");
    expect(await cache.get("openai")).toBe("v2-value");
    await cache.get(new SecretRef("openai", { versionStage: "AWSCURRENT" }));
    expect(api.count("GetSecretValue")).toBe(3);
  });

  test("ten concurrent reads share one promise and one call", async () => {
    const { api, cache } = rig();
    api.getDelayMs = 100;
    const values = await Promise.all(Array.from({ length: 10 }, () => cache.get("openai")));
    expect(values).toEqual(Array(10).fill(SENTINEL_VALUE));
    expect(api.count("GetSecretValue")).toBe(1);
  });

  test("different regions and credentials never share values", async () => {
    const api = new FakeSecretsManager();
    api.put("rayito/openai", SENTINEL_VALUE);
    const east = new SecretCache({ store: new SecretStore({ client: api, region: "us-east-1" }) });
    const west = new SecretCache({ store: new SecretStore({ client: api, region: "eu-west-1" }) });
    await east.get("openai");
    await west.get("openai");
    expect(api.count("GetSecretValue")).toBe(2);
    const alice = { accessKeyId: "AKIAALICE", secretAccessKey: "a" };
    const bob = { accessKeyId: "AKIABOB", secretAccessKey: "b" };
    const shared = new FakeSecretsManager();
    shared.put("rayito/openai", SENTINEL_VALUE);
    const first = new SecretStore({ client: shared, region: "us-east-1", credentials: alice });
    const second = new SecretStore({ client: shared, region: "us-east-1", credentials: bob });
    expect(first.cacheIdentity).not.toEqual(second.cacheIdentity);
  });

  test.each([0, -1, 86_401, Number.NaN])("ttlSeconds %s is rejected", (ttl) => {
    expect(() => new SecretCache({ ttlSeconds: ttl })).toThrow(InvalidArgumentError);
  });

  test("store and client options are exclusive", () => {
    expect(() => new SecretCache({ store: new SecretStore(), region: "us-east-1" })).toThrow(
      InvalidArgumentError,
    );
  });

  test("a missing secret is never cached", async () => {
    const { api, cache } = rig();
    await expect(cache.get(SENTINEL_NAME)).rejects.toBeInstanceOf(SecretNotFoundError);
    await expect(cache.get(SENTINEL_NAME)).rejects.toBeInstanceOf(SecretNotFoundError);
    expect(api.count("GetSecretValue")).toBe(2);
    api.put(`rayito/${SENTINEL_NAME}`, "now-exists");
    expect(await cache.get(SENTINEL_NAME)).toBe("now-exists");
  });

  test("JSON, String and inspect never show a value", async () => {
    const { cache } = rig();
    await cache.get("openai");
    for (const rendered of [
      JSON.stringify(cache),
      String(cache),
      inspect(cache),
      JSON.stringify(new SecretRef("openai")),
    ]) {
      expect(rendered).not.toContain(SENTINEL_VALUE);
    }
    expect(JSON.stringify(cache)).toContain("***");
  });
});

describe("SecretCache and webhook signing secrets", () => {
  // `secrets` injects into an untrusted sandbox: with a webhook's signing
  // secret it could forge signed deliveries to the receiver.
  for (const secret of [
    "webhooks/prod",
    new SecretRef("webhooks/prod"),
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:rayito/webhooks/prod-AbCdEf",
  ]) {
    test(`never reads ${String(secret)}`, async () => {
      const { api, cache } = rig();
      api.put("rayito/webhooks/prod", SENTINEL_VALUE);
      const failure = cache.get(secret);
      await expect(failure).rejects.toThrow(InvalidArgumentError);
      await expect(failure).rejects.not.toThrow(/prod/);
      expect(api.count("GetSecretValue")).toBe(0);
    });
  }

  test("a secret merely named like webhooks elsewhere is still read", async () => {
    const { api, cache } = rig();
    api.put("rayito/team/webhooks/prod", SENTINEL_VALUE);
    expect(await cache.get("team/webhooks/prod")).toBe(SENTINEL_VALUE);
  });
});
