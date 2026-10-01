/**
 * `Secret` de `rayito/e2b` frente a `src/secret.ts` de E2B JS 2.51.0 (npm
 * `e2b@2.51.0`, `dist/index.d.ts`, descargado y leído el 2026-09-30): mismos
 * estáticos con la misma aridad, `SecretInfo` con los mismos campos,
 * `SecretPaginator` con `hasNext`/`nextToken`/`nextItems`, `SecretError` y
 * `SecretNotFoundError`, y las divergencias de docs/site/docs/e2b-compat.md.
 */

import { afterEach, describe, expect, test, vi } from "vitest";
import {
  E2B,
  InvalidArgumentError,
  Secret,
  SecretError,
  SecretNotFoundError,
  SecretPaginator,
  UnimplementedError,
} from "../../src/e2b/index.js";
import * as native from "../../src/index.js";
import { FakeSecretsManager, SENTINEL_NAME, SENTINEL_VALUE } from "./secrets-fake.js";

// Aridad (`Function.length`: parámetros antes del primero opcional) de E2B 2.51.0.
const E2B_ARITY: Readonly<Record<string, number>> = {
  create: 2,
  update: 2,
  getInfo: 1,
  list: 0,
  exists: 1,
  destroy: 1,
  fill: 1,
  iamToken: 1,
};
/** `${e2b.secrets.<name>}`, la cadena literal de E2B (sin interpolar). */
function placeholder(name: string): string {
  return ["$", "{e2b.secrets.", name, "}"].join("");
}

const E2B_SECRET_INFO_FIELDS = [
  "secretId",
  "name",
  "version",
  "metadata",
  "createdAt",
  "updatedAt",
];

afterEach(() => {
  vi.restoreAllMocks();
});

describe("rayito/e2b Secret", () => {
  test.each(Object.entries(E2B_ARITY))("%s exists with the E2B arity", (method, arity) => {
    const member = (Secret as unknown as Record<string, unknown>)[method];
    expect(typeof member).toBe("function");
    expect((member as (...args: unknown[]) => unknown).length).toBe(arity);
  });

  test("errors keep the E2B hierarchy and are the native classes", () => {
    expect(new SecretNotFoundError("x")).toBeInstanceOf(SecretError);
    expect(SecretError).toBe(native.SecretError);
    expect(SecretNotFoundError).toBe(native.SecretNotFoundError);
  });

  test("fill is the literal E2B placeholder without any call", () => {
    expect(Secret.fill("openai-api-key")).toBe(placeholder("openai-api-key"));
    expect(() => Secret.fill("bad}name")).toThrow(InvalidArgumentError);
  });

  test("CRUD round-trips over Secrets Manager with E2B's SecretInfo shape", async () => {
    const client = new FakeSecretsManager();
    const info = await Secret.create("OpenAI-Key", SENTINEL_VALUE, {
      client,
      metadata: { team: "ml" },
    });
    expect(Object.keys(info).sort()).toEqual([...E2B_SECRET_INFO_FIELDS].sort());
    expect(info.name).toBe("openai-key");
    expect(info.secretId).toMatch(/^arn:aws:secretsmanager:/);
    expect(client.secrets.has("rayito/openai-key")).toBe(true);
    expect(await Secret.exists("openai-key", { client })).toBe(true);
    const updated = await Secret.update(info.secretId, "rotated", { client });
    expect(updated.version).toBe(2);
    expect(updated.metadata).toEqual({ team: "ml" });
    expect((await Secret.getInfo("openai-key", { client })).version).toBe(2);
    expect(await Secret.destroy("openai-key", { client })).toBe(true);
    expect(await Secret.destroy("openai-key", { client })).toBe(false);
  });

  test("not found is SecretNotFoundError without the name", async () => {
    const client = new FakeSecretsManager();
    const error = await Secret.getInfo(SENTINEL_NAME, { client }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(SecretNotFoundError);
    expect(String(error)).not.toContain(SENTINEL_NAME);
  });

  test.each(["", "x".repeat(129), "has space", "sec_reserved", "SEC_upper"])(
    "name %j is validated like the E2B API before calling AWS",
    async (name) => {
      const client = new FakeSecretsManager();
      const error = await Secret.create(name, "v", { client }).catch((e: unknown) => e);
      expect(error).toBeInstanceOf(InvalidArgumentError);
      if (name) {
        expect(String(error)).not.toContain(name);
      }
      expect(client.calls.size).toBe(0);
    },
  );

  test("the paginator walks pages like E2B", async () => {
    const client = new FakeSecretsManager();
    for (let index = 0; index < 5; index += 1) {
      await Secret.create(`s${index}`, "v", { client });
    }
    const paginator = Secret.list({ client, limit: 2 });
    expect(paginator).toBeInstanceOf(SecretPaginator);
    const pages: string[][] = [];
    while (paginator.hasNext) {
      pages.push((await paginator.nextItems()).map((info) => info.name));
    }
    expect(pages).toEqual([["s0", "s1"], ["s2", "s3"], ["s4"]]);
    expect(paginator.nextToken).toBeUndefined();
    await expect(paginator.nextItems()).rejects.toThrow("No more items to fetch");
  });

  test("iamToken stays UnimplementedError with the iam reason", () => {
    expect(() => Secret.iamToken({ audience: "sts.amazonaws.com" })).toThrow(UnimplementedError);
    try {
      Secret.iamToken({});
    } catch (error) {
      expect((error as UnimplementedError).feature).toBe("iam");
    }
  });

  test("E2B connection options warn and are ignored, naming the option only", async () => {
    const warn = vi.spyOn(process, "emitWarning").mockImplementation(() => undefined);
    const client = new FakeSecretsManager();
    await Secret.create("a", "v", { client, apiKey: "e2b_live_key", domain: "e2b.app" });
    const messages = warn.mock.calls.map(([message]) => String(message));
    expect(messages.map((m) => m.split(" ")[0]).sort()).toEqual(["apiKey", "domain"]);
    expect(messages.join("\n")).not.toContain("e2b_live_key");
  });

  test("new E2B({ region }).Secret is bound to the client's region", async () => {
    vi.spyOn(process, "emitWarning").mockImplementation(() => undefined);
    const client = new FakeSecretsManager();
    const e2b = new E2B({ region: "eu-west-1" });
    expect(e2b.Secret.boundOpts).toEqual({ region: "eu-west-1" });
    await e2b.Secret.create("bound", "v", { client });
    expect(client.secrets.has("rayito/bound")).toBe(true);
    expect(e2b.Secret.fill("x")).toBe(placeholder("x"));
  });

  test("secretPrefix and kmsKeyId are Rayito options", async () => {
    const client = new FakeSecretsManager();
    await Secret.create("k", "v", { client, secretPrefix: "team/", kmsKeyId: "alias/x" });
    expect(client.secrets.has("team/k")).toBe(true);
    expect(client.requests[0]?.[1]).toMatchObject({ KmsKeyId: "alias/x" });
  });
});
