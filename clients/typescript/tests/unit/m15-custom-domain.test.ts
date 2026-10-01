/**
 * `src/custom-domain/{domain,kvs,service}.ts` (m15-custom-domain, ADR-024):
 * espejo de `test_m15_custom_domain_domain.py`/`test_m15_custom_domain_service.py`.
 * Los casos de hostname vienen de `testdata/custom-domain/hostnames.json`
 * (compartido con Python).
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { beforeEach, describe, expect, test } from "vitest";
import {
  checkKvsValueSize,
  decodeRouteMetadata,
  encodeRouteMetadata,
  kvsJsonKey,
  kvsMetaKey,
  MAX_KVS_VALUE_BYTES,
  routeHost,
  routeLabel,
  trafficTokenDigest,
  validateAlias,
  validatePublicDomain,
  validateRoutePort,
} from "../../src/custom-domain/domain.js";
import {
  CustomDomain,
  type CustomDomainRoute,
  STACK_COMPONENT,
} from "../../src/custom-domain/service.js";
import { CustomDomainError, InvalidArgumentError, StackError } from "../../src/errors.js";
import type { StackStatus } from "../../src/stacks/model.js";
import { FakeKeyValueStoreWriter } from "./m15-fake-custom-domain.js";
import { FakeStackProvisioner } from "./m15-fake-stacks.js";

const TESTDATA = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../../../../testdata/custom-domain/hostnames.json", import.meta.url)),
    "utf8",
  ),
) as {
  valid: Array<{ alias: string; port: number; publicDomain: string; host: string }>;
  invalidAlias: string[];
  invalidPort: number[];
  invalidPublicDomain: string[];
};

const PUBLIC_DOMAIN = "sbx.example.com";
const KVS_ARN = "arn:aws:cloudfront::111122223333:key-value-store/abc123";
const CERTIFICATE_ARN = "arn:aws:acm:us-east-1:111122223333:certificate/abc";

function deployedDomain(clock = () => 1_000_000): {
  domain: CustomDomain;
  stacks: FakeStackProvisioner;
  kvs: FakeKeyValueStoreWriter;
} {
  const stacks = new FakeStackProvisioner();
  const kvs = new FakeKeyValueStoreWriter();
  const domain = new CustomDomain({
    publicDomain: PUBLIC_DOMAIN,
    kvsArn: KVS_ARN,
    provisioner: stacks,
    kvsWriter: kvs,
    clock,
  });
  kvs.seed(KVS_ARN);
  return { domain, stacks, kvs };
}

describe("custom-domain/domain", () => {
  test.each(TESTDATA.valid)("routeHost matches the shared fixture: $host", (c) => {
    expect(routeHost(c.alias, c.port, c.publicDomain)).toBe(c.host);
  });

  test.each(TESTDATA.invalidAlias)("invalid alias %j is rejected", (alias) => {
    expect(() => validateAlias(alias)).toThrow(InvalidArgumentError);
  });

  test.each(TESTDATA.invalidPort)("invalid port %j is rejected", (port) => {
    expect(() => validateRoutePort(port)).toThrow(InvalidArgumentError);
  });

  test.each(TESTDATA.invalidPublicDomain)("invalid publicDomain %j is rejected", (domain) => {
    expect(() => validatePublicDomain(domain)).toThrow(InvalidArgumentError);
  });

  test("routeLabel is port-dash-alias", () => {
    expect(routeLabel("ws-7", 8000)).toBe("8000-ws-7");
  });

  test("kvs keys are prefixed and distinct", () => {
    const label = routeLabel("ws-7", 8000);
    expect(kvsJsonKey(label)).toBe("j:8000-ws-7");
    expect(kvsMetaKey(label)).toBe("m:8000-ws-7");
  });

  test("trafficTokenDigest is sha256 hex or empty", () => {
    expect(trafficTokenDigest(undefined)).toBe("");
    const digest = trafficTokenDigest("un-token");
    expect(digest).toHaveLength(64);
    expect(digest).toBe(trafficTokenDigest("un-token"));
  });

  test("route metadata round-trips through JSON", () => {
    const metadata = { endpoint: "e", trafficTokenSha256: "", expiresAt: 1234 };
    expect(decodeRouteMetadata(encodeRouteMetadata(metadata))).toEqual(metadata);
  });

  test("route metadata over the KVS limit is rejected", () => {
    expect(() =>
      encodeRouteMetadata({
        endpoint: "e".repeat(1100),
        trafficTokenSha256: "t".repeat(64),
        expiresAt: 1,
      }),
    ).toThrow(CustomDomainError);
  });

  test("checkKvsValueSize accepts a real-JWE-sized value (DOM-1: 823 B)", () => {
    expect(() => checkKvsValueSize("x".repeat(823))).not.toThrow();
    expect(MAX_KVS_VALUE_BYTES).toBe(1024);
  });

  test("checkKvsValueSize rejects oversized values", () => {
    expect(() => checkKvsValueSize("x".repeat(MAX_KVS_VALUE_BYTES + 1))).toThrow(CustomDomainError);
  });
});

describe("custom-domain/service", () => {
  test("constructing CustomDomain with fakes makes no call", () => {
    const stacks = new FakeStackProvisioner();
    const kvs = new FakeKeyValueStoreWriter();
    new CustomDomain({ publicDomain: PUBLIC_DOMAIN, provisioner: stacks, kvsWriter: kvs });
    expect(stacks.calls).toEqual([]);
    expect(kvs.calls).toEqual([]);
  });

  test("an invalid publicDomain is rejected at construction", () => {
    expect(() => new CustomDomain({ publicDomain: "-not-valid-" })).toThrow(InvalidArgumentError);
  });

  test("deploy delegates to OptionalStacks with the right parameters", async () => {
    const stacks = new FakeStackProvisioner();
    const kvs = new FakeKeyValueStoreWriter();
    const domain = new CustomDomain({
      publicDomain: PUBLIC_DOMAIN,
      provisioner: stacks,
      kvsWriter: kvs,
    });
    const status = await domain.deploy({ certificateArn: CERTIFICATE_ARN });
    expect(status.state).toBe("CREATE_COMPLETE");
    expect(stacks.calls.map((c) => c[0])).toEqual(["describe", "create", "wait", "describe"]);
  });

  test("deploy caches the KvsArn from the stack outputs", async () => {
    const stacks = new FakeStackProvisioner();
    stacks.stacks.set(`rayito-${STACK_COMPONENT}`, {
      name: `rayito-${STACK_COMPONENT}`,
      state: "CREATE_COMPLETE",
      outputs: { KvsArn: KVS_ARN },
    });
    const domain = new CustomDomain({ publicDomain: PUBLIC_DOMAIN, provisioner: stacks });
    await domain.deploy({ certificateArn: CERTIFICATE_ARN });
    expect(domain.kvsArn()).toBe(KVS_ARN);
  });

  test("status also caches the KvsArn", async () => {
    const stacks = new FakeStackProvisioner();
    const status: StackStatus = {
      name: `rayito-${STACK_COMPONENT}`,
      state: "CREATE_COMPLETE",
      outputs: { KvsArn: KVS_ARN },
    };
    stacks.stacks.set(status.name, status);
    const domain = new CustomDomain({ publicDomain: PUBLIC_DOMAIN, provisioner: stacks });
    await domain.status();
    expect(domain.kvsArn()).toBe(KVS_ARN);
  });

  test("kvsArn without a deploy or status call throws", () => {
    const domain = new CustomDomain({
      publicDomain: PUBLIC_DOMAIN,
      provisioner: new FakeStackProvisioner(),
    });
    expect(() => domain.kvsArn()).toThrow(/KvsArn/);
  });

  test("an explicit kvsArn needs no status call", () => {
    const kvs = new FakeKeyValueStoreWriter();
    kvs.seed(KVS_ARN);
    const domain = new CustomDomain({
      publicDomain: PUBLIC_DOMAIN,
      kvsArn: KVS_ARN,
      provisioner: new FakeStackProvisioner(),
      kvsWriter: kvs,
    });
    expect(domain.kvsArn()).toBe(KVS_ARN);
  });

  test("hostFor is pure and needs no deploy", () => {
    const domain = new CustomDomain({
      publicDomain: PUBLIC_DOMAIN,
      provisioner: new FakeStackProvisioner(),
    });
    expect(domain.hostFor("ws-7", 8000)).toBe("8000-ws-7.sbx.example.com");
  });

  describe("with a deployed domain", () => {
    let domain: CustomDomain;
    let kvs: FakeKeyValueStoreWriter;

    beforeEach(() => {
      ({ domain, kvs } = deployedDomain());
    });

    test("register writes both keys with a chained etag", async () => {
      const route = await domain.register("ws-7", 8000, {
        endpoint: "10.0.0.1.lambda-url.us-east-1.on.aws",
        jwe: "a-jwe",
        public: true,
        ttlSeconds: 2400,
      });
      expect(route.host).toBe("8000-ws-7.sbx.example.com");
      expect(route.expiresAt.getTime()).toBe((1_000_000 + 2400) * 1000);
      expect(kvs.stores.get(KVS_ARN)?.get("j:8000-ws-7")).toBe("a-jwe");
      const meta = kvs.stores.get(KVS_ARN)?.get("m:8000-ws-7");
      expect(meta).toContain('"e":"10.0.0.1.lambda-url.us-east-1.on.aws"');
      expect(kvs.calls.map((c) => c[0])).toEqual(["describe", "put", "put"]);
    });

    test("register with a traffic token stores only its digest", async () => {
      await domain.register("ws-7", 8000, {
        endpoint: "e",
        jwe: "a-jwe",
        trafficToken: "secret",
        ttlSeconds: 60,
      });
      const meta = kvs.stores.get(KVS_ARN)?.get("m:8000-ws-7") ?? "";
      expect(meta).not.toContain("secret");
    });

    test("register without a trafficToken or public is rejected (SEC-T25)", async () => {
      await expect(
        domain.register("ws-7", 8000, { endpoint: "e", jwe: "a-jwe", ttlSeconds: 60 }),
      ).rejects.toThrow(InvalidArgumentError);
      expect(kvs.calls).toEqual([]);
    });

    test("register with public true and no token is allowed", async () => {
      const route = await domain.register("ws-7", 8000, {
        endpoint: "e",
        jwe: "a-jwe",
        public: true,
        ttlSeconds: 60,
      });
      expect(route.trafficTokenSha256).toBe("");
    });

    test("register rejects a non-positive ttl before touching the KVS", async () => {
      await expect(
        domain.register("ws-7", 8000, { endpoint: "e", jwe: "a-jwe", public: true, ttlSeconds: 0 }),
      ).rejects.toThrow(InvalidArgumentError);
      expect(kvs.calls).toEqual([]);
    });

    test("register rejects an oversized jwe before touching the KVS", async () => {
      await expect(
        domain.register("ws-7", 8000, {
          endpoint: "e",
          jwe: "x".repeat(MAX_KVS_VALUE_BYTES + 1),
          public: true,
          ttlSeconds: 60,
        }),
      ).rejects.toThrow(CustomDomainError);
      expect(kvs.calls).toEqual([]);
    });

    test("register rolls back the jwe if the metadata put fails", async () => {
      kvs.failPutOnce.set("m:8000-ws-7", "ValidationException");
      await expect(
        domain.register("ws-7", 8000, {
          endpoint: "e",
          jwe: "a-jwe",
          public: true,
          ttlSeconds: 60,
        }),
      ).rejects.toThrow(CustomDomainError);
      expect(kvs.stores.get(KVS_ARN)?.has("j:8000-ws-7")).toBe(false);
      expect(kvs.stores.get(KVS_ARN)?.has("m:8000-ws-7")).toBe(false);
    });

    test("register retries once on an etag conflict", async () => {
      kvs.failPutOnce.set("j:8000-ws-7", "ConflictException");
      const route = await domain.register("ws-7", 8000, {
        endpoint: "e",
        jwe: "a-jwe",
        public: true,
        ttlSeconds: 60,
      });
      expect(kvs.stores.get(KVS_ARN)?.get("j:8000-ws-7")).toBe("a-jwe");
      expect(route.host).toBe("8000-ws-7.sbx.example.com");
    });

    test("refresh rewrites both keys, keeping endpoint and token hash", async () => {
      const route = await domain.register("ws-7", 8000, {
        endpoint: "e",
        jwe: "old-jwe",
        trafficToken: "secret",
        ttlSeconds: 60,
      });
      const refreshed = await domain.refresh(route, { jwe: "new-jwe", ttlSeconds: 2400 });
      expect(kvs.stores.get(KVS_ARN)?.get("j:8000-ws-7")).toBe("new-jwe");
      expect(kvs.stores.get(KVS_ARN)?.get("m:8000-ws-7")).toContain('"x":1002400');
      expect(refreshed.expiresAt.getTime()).toBe((1_000_000 + 2400) * 1000);
      expect(refreshed.endpoint).toBe(route.endpoint);
      expect(refreshed.trafficTokenSha256).toBe(route.trafficTokenSha256);
    });

    test("unregister removes both keys", async () => {
      await domain.register("ws-7", 8000, {
        endpoint: "e",
        jwe: "a-jwe",
        public: true,
        ttlSeconds: 60,
      });
      await domain.unregister("ws-7", 8000);
      expect(kvs.stores.get(KVS_ARN)?.has("j:8000-ws-7")).toBe(false);
      expect(kvs.stores.get(KVS_ARN)?.has("m:8000-ws-7")).toBe(false);
    });

    test("unregister a route that never existed is a no-op", async () => {
      await expect(domain.unregister("never-registered", 8000)).resolves.toBeUndefined();
    });
  });

  test("destroy forgets a KvsArn learned from status", async () => {
    const stacks = new FakeStackProvisioner();
    const status: StackStatus = {
      name: `rayito-${STACK_COMPONENT}`,
      state: "CREATE_COMPLETE",
      outputs: { KvsArn: KVS_ARN },
    };
    stacks.stacks.set(status.name, status);
    const domain = new CustomDomain({ publicDomain: PUBLIC_DOMAIN, provisioner: stacks });
    await domain.status();
    expect(domain.kvsArn()).toBe(KVS_ARN);
    await domain.destroy();
    expect(stacks.calls.at(-1)?.[0]).toBe("wait");
    expect(() => domain.kvsArn()).toThrow(CustomDomainError);
  });

  test("blocked deploy surfaces StackError", async () => {
    const stacks = new FakeStackProvisioner();
    stacks.stacks.set(`rayito-${STACK_COMPONENT}`, {
      name: `rayito-${STACK_COMPONENT}`,
      state: "ROLLBACK_COMPLETE",
      outputs: {},
    });
    const domain = new CustomDomain({ publicDomain: PUBLIC_DOMAIN, provisioner: stacks });
    await expect(domain.deploy({ certificateArn: CERTIFICATE_ARN })).rejects.toThrow(StackError);
  });

  test("register returns a CustomDomainRoute shape", async () => {
    const { domain } = deployedDomain();
    const route: CustomDomainRoute = await domain.register("ws-7", 8000, {
      endpoint: "e",
      jwe: "a-jwe",
      public: true,
      ttlSeconds: 60,
    });
    expect(route.alias).toBe("ws-7");
    expect(route.port).toBe(8000);
  });
});
