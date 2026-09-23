/**
 * El mapeo puro de `rayito/e2b` (D5, D6, D10, D14 y D17 en camelCase):
 * `mapCreateOptions`, los avisos de las opciones ignoradas, la red, el
 * `lifecycle`, `infoFromNative` y la tabla de `UnimplementedError`.
 */

import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { Code } from "@connectrpc/connect";
import { afterEach, describe, expect, test, vi } from "vitest";
import {
  AVAILABLE_KERNELS_REASON,
  COMPAT_WARNING_TYPE,
  e2bDefaultMaxLifetimeMs,
  emitIgnoredWarnings,
  HTTPS_PORTS_REASON,
  HTTPS_PORTS_SUPPORTED,
  IGNORED_CONNECTION_OPTS,
  infoFromNative,
  LIST_METADATA_STATE_FEATURE,
  mapCreateOptions,
  mapLifecycle,
  mapListOptions,
  mapNetworkUpdate,
  mergeBoundOpts,
  metricsFromNative,
  normalizedLanguageOrUnimplemented,
  POLY_KERNELS_REASON,
  rejectUnsupportedNetworkKeys,
  settleAsyncCallback,
  splitConnectionOpts,
  unimplementedLanguage,
  validateKeepMemory,
  validateOnResume,
} from "../../src/e2b/compat.js";
import { ConnectionConfig } from "../../src/e2b/connection.js";
import type { SandboxLifecycle, SandboxOpts } from "../../src/e2b/types.js";
import {
  COMPAT_DOC_PATH,
  UNIMPLEMENTED_REASONS,
  unimplemented,
} from "../../src/e2b/unimplemented.js";
import {
  InvalidArgumentError,
  SandboxError,
  SandboxNotFoundError,
  UnimplementedError,
} from "../../src/errors.js";
import { PORT_MAX } from "../../src/limits.js";
import {
  ALL_TRAFFIC,
  EgressEnforcement,
  type SandboxLifecycle as NativeLifecycle,
  type NetworkState,
  sandboxInfo,
  sandboxListItem,
} from "../../src/models.js";
import {
  CAP_MARGIN_MS,
  MAX_LIFETIME_MS,
  defaultMaxLifetimeMs as nativeDefaultMaxLifetimeMs,
} from "../../src/sandbox/lifecycle.js";

const IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base-2gb";
const STARTED_AT = new Date(Date.UTC(2026, 8, 15, 14, 39, 2));
const DEADLINE = new Date(Date.UTC(2026, 8, 15, 14, 44, 2));
const SECRET = "valor-secreto";

const D14_TS_KEYS = [
  "fork",
  "createSnapshot",
  "listSnapshots",
  "deleteSnapshot",
  "connect({ onResume: 'reboot' })",
  "pause({ keepMemory: false })",
  "lifecycle.onTimeout.keepMemory=false",
  "network.rules",
  "network.maskRequestHost",
  "network.allowPublicTraffic=true",
  "iam",
  "mcp",
  "getMcpUrl",
  "getMcpToken",
  "volumeMounts",
  "Volume",
  "getSignature",
  "Secret",
  "Template",
];

const PYTHON_UNIMPLEMENTED = fileURLToPath(
  new URL("../../../python/src/rayito/e2b/_unimplemented.py", import.meta.url),
);

const PYTHON_COMPAT = fileURLToPath(
  new URL("../../../python/src/rayito/e2b/_compat.py", import.meta.url),
);

const INTERNET_EGRESS_ARN =
  "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS";

function nativeInfo(
  overrides: Partial<Parameters<typeof sandboxInfo>[0]> = {},
): ReturnType<typeof sandboxInfo> {
  return sandboxInfo({
    sandboxId: "microvm-1",
    state: "RUNNING",
    endpoint: "abc.lambda-microvm.us-east-1.on.aws",
    template: IMAGE_ARN,
    templateVersion: "1.0",
    startedAt: STARTED_AT,
    maximumDurationSeconds: 3600,
    ...overrides,
  });
}

function managed(overrides: Partial<NativeLifecycle> = {}): NativeLifecycle {
  return {
    phase: "active",
    deadline: DEADLINE,
    cap: undefined,
    timeoutMs: 300_000,
    onTimeout: "pause",
    autoResume: true,
    extensions: 0,
    ...overrides,
  };
}

function networkState(allowOut: string[], denyOut: string[]): NetworkState {
  return {
    allowOut,
    denyOut,
    egressProxyConfigured: false,
    enforcement: EgressEnforcement.GUEST_ROUTES,
    localProxyPort: undefined,
  };
}

function caught(action: () => unknown): unknown {
  try {
    action();
  } catch (error) {
    return error;
  }
  throw new Error("no lanzó");
}

function spyWarnings(): string[] {
  const seen: string[] = [];
  vi.spyOn(process, "emitWarning").mockImplementation(((
    warning: string | Error,
    options?: { type?: string },
  ) => {
    expect(options?.type).toBe(COMPAT_WARNING_TYPE);
    seen.push(String(warning));
  }) as typeof process.emitWarning);
  return seen;
}

afterEach(() => {
  vi.restoreAllMocks();
  ConnectionConfig.setIntegration(undefined);
});

describe("the D14 table", () => {
  test("every key builds an UnimplementedError with its reason, citing its source", () => {
    expect(Object.keys(UNIMPLEMENTED_REASONS).sort()).toEqual([...D14_TS_KEYS].sort());
    for (const key of D14_TS_KEYS) {
      const error = unimplemented(key);
      expect(error).toBeInstanceOf(UnimplementedError);
      expect(error.feature).toBe(key);
      expect(error.reason).toBe(UNIMPLEMENTED_REASONS[key]);
      expect(error.doc).toBe(COMPAT_DOC_PATH);
      expect(error.reason).toMatch(/AWS_API_NOTES\.md §|SPEC\.md §4/);
      expect(error.message).toContain(key);
    }
    expect(() => unimplemented("desconocido")).toThrow(RangeError);
  });

  test.skipIf(!existsSync(PYTHON_UNIMPLEMENTED))(
    "each reason is byte-identical to the Python _unimplemented.py",
    () => {
      const joined = readFileSync(PYTHON_UNIMPLEMENTED, "utf8").replace(/"\s*\n\s*"/g, "");
      for (const reason of new Set(Object.values(UNIMPLEMENTED_REASONS))) {
        expect(joined).toContain(reason);
      }
    },
  );
});

describe("mapCreateOptions", () => {
  test("create(template, opts) and create(opts) give the E2B defaults on the native options", () => {
    const positional = mapCreateOptions("tpl", { metadata: { a: "1" }, envs: { K: "v" } });
    expect(positional.native).toMatchObject({
      template: "tpl",
      metadata: { a: "1" },
      envs: { K: "v" },
      timeoutMs: 300_000,
      maxLifetimeMs: 3_600_000,
      onTimeout: "kill",
      idle: null,
      ingress: ["ALL_INGRESS"],
      egress: ["INTERNET_EGRESS"],
    });
    expect(positional.ignored).toEqual([]);
    const inside = mapCreateOptions({ template: "tpl", ingress: [], timeoutMs: 7_200_000 });
    expect(inside.native).toMatchObject({ template: "tpl", ingress: [], maxLifetimeMs: 7_260_000 });
    expect(mapCreateOptions().native.template).toBeUndefined();
    expect(mapCreateOptions(undefined, undefined).native.timeoutMs).toBe(300_000);
  });

  test("the shim's lifetime cap reuses the native caps, in milliseconds", () => {
    expect(e2bDefaultMaxLifetimeMs(10 * MAX_LIFETIME_MS)).toBe(MAX_LIFETIME_MS);
    expect(e2bDefaultMaxLifetimeMs(7_200_000)).toBe(7_200_000 + CAP_MARGIN_MS);
    expect(e2bDefaultMaxLifetimeMs(7_200_000)).not.toBe(nativeDefaultMaxLifetimeMs(7_200_000));
    expect(() => rejectUnsupportedNetworkKeys({ httpsPorts: [PORT_MAX + 1] })).toThrow(
      InvalidArgumentError,
    );
  });

  test("maxLifetimeMs follows max(3 600 000, min(timeoutMs + 60 000, 28 800 000))", () => {
    expect(e2bDefaultMaxLifetimeMs(60_000)).toBe(3_600_000);
    expect(e2bDefaultMaxLifetimeMs(3_600_000)).toBe(3_660_000);
    expect(e2bDefaultMaxLifetimeMs(3_600_500)).toBe(3_661_000);
    expect(e2bDefaultMaxLifetimeMs(28_800_000)).toBe(28_800_000);
    expect(mapCreateOptions({ maxLifetimeMs: 600_000 }).native.maxLifetimeMs).toBe(600_000);
  });

  test("lifecycle pause becomes onTimeout pause with a 300 s platform idle", () => {
    const pause = mapCreateOptions({ lifecycle: { onTimeout: "pause", autoResume: true } });
    expect(pause.native).toMatchObject({
      onTimeout: "pause",
      idle: { maxIdleSeconds: 300, autoResume: true },
    });
    const object = mapCreateOptions({
      lifecycle: { onTimeout: { action: "pause", keepMemory: true } },
    });
    expect(object.native.idle).toEqual({ maxIdleSeconds: 300, autoResume: false });
  });

  test("lifecycle is validated like E2B", () => {
    const invalid: unknown[] = [
      { onTimeout: "sleep" },
      { onTimeout: { action: "sleep" } },
      { onTimeout: { action: "kill", keepMemory: true } },
      { onTimeout: "kill", autoResume: true },
      { onTimeout: "pause", extra: 1 },
      { autoResume: "yes" },
    ];
    for (const lifecycle of invalid) {
      expect(() => mapLifecycle(lifecycle as SandboxLifecycle)).toThrow(InvalidArgumentError);
    }
    expect(mapLifecycle({ onTimeout: { action: "kill" } })).toEqual({
      onTimeout: "kill",
      autoResume: false,
    });
    const error = caught(() =>
      mapCreateOptions({ lifecycle: { onTimeout: { action: "pause", keepMemory: false } } }),
    ) as UnimplementedError;
    expect(error).toBeInstanceOf(UnimplementedError);
    expect(error.feature).toBe("lifecycle.onTimeout.keepMemory=false");
    expect(error.message).toContain("suspend-microvm");
  });

  test("mcp, iam and volumeMounts are unimplemented whatever their value", () => {
    for (const [key, feature] of [
      ["mcp", "mcp"],
      ["iam", "iam"],
      ["volumeMounts", "volumeMounts"],
    ] as const) {
      for (const value of [{}, [], "x", null]) {
        const error = caught(() => mapCreateOptions({ [key]: value } as SandboxOpts));
        expect(error).toBeInstanceOf(UnimplementedError);
        expect((error as UnimplementedError).feature).toBe(feature);
      }
    }
  });

  test("the ignored options give one warning each, naming the option and never the value", () => {
    const seen = spyWarnings();
    const opts: SandboxOpts = {
      apiKey: SECRET,
      validateApiKey: true,
      domain: SECRET,
      debug: true,
      apiUrl: SECRET,
      sandboxUrl: SECRET,
      apiHeaders: { "x-secret": SECRET },
      secure: false,
    };
    const mapping = mapCreateOptions(opts);
    expect(mapping.ignored).toEqual([...Object.keys(IGNORED_CONNECTION_OPTS).sort(), "secure"]);
    emitIgnoredWarnings(mapping.ignored);
    expect(seen).toHaveLength(8);
    for (const [index, name] of mapping.ignored.entries()) {
      expect(seen[index]).toMatch(new RegExp(`^${name} ignorado: `));
      expect(seen[index]).not.toContain(SECRET);
    }
    expect(mapCreateOptions({ secure: true }).ignored).toEqual([]);
  });
});

describe("connection options", () => {
  test("headers go to transport.extraHeaders, proxy to the transport and the plane", () => {
    ConnectionConfig.setIntegration("acme/1.0");
    const { connection } = splitConnectionOpts({
      headers: { "X-Trace": "1" },
      proxy: "http://u:p@127.0.0.1:3128",
      retries: 2,
      requestTimeoutMs: 5000,
      transport: { scheme: "http" },
    });
    expect(connection.transport).toEqual({
      scheme: "http",
      extraHeaders: { "x-trace": "1" },
      proxy: "http://u:p@127.0.0.1:3128",
    });
    expect(connection).toMatchObject({
      proxy: "http://u:p@127.0.0.1:3128",
      retries: 2,
      integration: "acme/1.0",
      requestTimeoutMs: 5000,
    });
    ConnectionConfig.setIntegration(undefined);
    expect(splitConnectionOpts({}).connection).toEqual({});
  });

  test("reserved headers, bad proxies and negative retries throw before any call", () => {
    for (const key of ["x-access-token", "X-AWS-Proxy-Port", "grpc-x", "a-bin", ":path"]) {
      const error = caught(() => splitConnectionOpts({ headers: { [key]: SECRET } })) as Error;
      expect(error).toBeInstanceOf(InvalidArgumentError);
      expect(error.message).not.toContain(SECRET);
    }
    for (const proxy of ["https://h:1", "http://h", "http://u:clave@"]) {
      const error = caught(() => splitConnectionOpts({ proxy })) as Error;
      expect(error).toBeInstanceOf(InvalidArgumentError);
      expect(error.message).not.toContain("clave");
    }
    expect(() => splitConnectionOpts({ retries: -1 })).toThrow(InvalidArgumentError);
  });

  test("bound options merge with E2B's rule: undefined falls back, headers replace", () => {
    const bound = { requestTimeoutMs: 1000, headers: { a: "1" }, region: "us-east-1" };
    expect(mergeBoundOpts(bound, { requestTimeoutMs: undefined, headers: { b: "2" } })).toEqual({
      requestTimeoutMs: 1000,
      headers: { b: "2" },
      region: "us-east-1",
    });
  });

  test("onResume and keepMemory", () => {
    expect(() => validateOnResume("restore")).not.toThrow();
    expect(() => validateOnResume(undefined)).not.toThrow();
    expect(caught(() => validateOnResume("reboot"))).toBeInstanceOf(UnimplementedError);
    expect(() => validateOnResume("later" as "restore")).toThrow(InvalidArgumentError);
    expect(() => validateKeepMemory(true)).not.toThrow();
    expect((caught(() => validateKeepMemory(false)) as UnimplementedError).feature).toBe(
      "pause({ keepMemory: false })",
    );
  });
});

describe("network", () => {
  test("rules, maskRequestHost and allowPublicTraffic true are unimplemented with the D14 reasons", () => {
    for (const [network, feature] of [
      [{ rules: {} }, "network.rules"],
      [{ maskRequestHost: "h" }, "network.maskRequestHost"],
      [{ allowPublicTraffic: true }, "network.allowPublicTraffic=true"],
    ] as const) {
      const error = caught(() => rejectUnsupportedNetworkKeys(network)) as UnimplementedError;
      expect(error).toBeInstanceOf(UnimplementedError);
      expect(error.feature).toBe(feature);
      expect(error.reason).toBe(UNIMPLEMENTED_REASONS[feature]);
    }
  });

  test("a non-empty httpsPorts follows the QE2 rule like Python", () => {
    expect(HTTPS_PORTS_SUPPORTED).toBe(false);
    const error = caught(() =>
      rejectUnsupportedNetworkKeys({ httpsPorts: [8443] }),
    ) as UnimplementedError;
    expect(error).toBeInstanceOf(UnimplementedError);
    expect(error.feature).toBe("network.httpsPorts");
    expect(error.reason).toBe(HTTPS_PORTS_REASON);
    expect(error.reason).toContain("QE2");
  });

  test("neutral keys pass, httpsPorts is validated, unknown keys are InvalidArgumentError", () => {
    expect(
      rejectUnsupportedNetworkKeys({
        allowOut: ["1.2.3.4"],
        denyOut: [ALL_TRAFFIC],
        allowPublicTraffic: false,
        httpsPorts: [],
      }),
    ).toEqual({ allowOut: ["1.2.3.4"], denyOut: [ALL_TRAFFIC] });
    expect(rejectUnsupportedNetworkKeys(undefined)).toBeUndefined();
    expect(() => rejectUnsupportedNetworkKeys({ httpsPorts: [0] })).toThrow(InvalidArgumentError);
    expect(() => rejectUnsupportedNetworkKeys({ bogus: 1 } as never)).toThrow(InvalidArgumentError);
    expect(caught(() => mapNetworkUpdate({ rules: {} }))).toBeInstanceOf(UnimplementedError);
    expect(mapNetworkUpdate({ denyOut: ["10.0.0.0/8"], allowInternetAccess: false })).toEqual({
      policy: { denyOut: ["10.0.0.0/8"] },
      allowInternetAccess: false,
    });
  });
});

describe("infoFromNative", () => {
  test("a managed sandbox fills every D10 field", () => {
    const info = infoFromNative(
      nativeInfo({
        lifecycle: managed(),
        cpuCount: 2,
        memoryMb: 2048,
        agentVersion: "0.9.0",
        metadata: { a: "1" },
      }),
      { network: networkState([], [ALL_TRAFFIC]), networkRead: true },
    );
    expect(info).toEqual({
      sandboxId: "microvm-1",
      templateId: IMAGE_ARN,
      name: "rayito-base-2gb",
      metadata: { a: "1" },
      startedAt: STARTED_AT,
      endAt: DEADLINE,
      state: "running",
      cpuCount: 2,
      memoryMB: 2048,
      envdVersion: "0.9.0",
      allowInternetAccess: false,
      network: { allowOut: [], denyOut: [ALL_TRAFFIC] },
      lifecycle: { onTimeout: "pause", autoResume: true },
      volumeMounts: [],
      sandboxDomain: "abc.lambda-microvm.us-east-1.on.aws",
    });
  });

  test("metadata comes from the native info, and is {} when it was not read", () => {
    expect(infoFromNative(nativeInfo({ metadata: { a: "1" } })).metadata).toEqual({ a: "1" });
    expect(infoFromNative(nativeInfo({ metadata: {} })).metadata).toEqual({});
    expect(infoFromNative(nativeInfo()).metadata).toEqual({});
  });

  test("unmanaged, suspended and without a guest policy", () => {
    const info = infoFromNative(
      nativeInfo({ state: "SUSPENDED", lifecycle: managed({ phase: "unmanaged" }) }),
    );
    expect(info.state).toBe("paused");
    expect(info.lifecycle).toBeUndefined();
    expect(info.endAt).toEqual(new Date(STARTED_AT.getTime() + 3_600_000));
    expect(info.network).toBeUndefined();
    expect(info.allowInternetAccess).toBeUndefined();
    expect(info.cpuCount).toBeUndefined();
    expect(info.metadata).toEqual({});
  });

  test("allowInternetAccess truth table", () => {
    const withPolicy = (allowOut: string[], denyOut: string[]) =>
      infoFromNative(nativeInfo(), { network: networkState(allowOut, denyOut), networkRead: true })
        .allowInternetAccess;
    expect(withPolicy([], [ALL_TRAFFIC])).toBe(false);
    expect(withPolicy(["1.2.3.4"], [ALL_TRAFFIC])).toBe(true);
    expect(withPolicy([], ["10.0.0.0/8"])).toBe(true);
    const none = { ...networkState([], []), enforcement: EgressEnforcement.NONE };
    const unenforced = infoFromNative(nativeInfo(), { network: none, networkRead: true });
    expect(unenforced.network).toEqual({ allowOut: [], denyOut: [] });
    expect(unenforced.allowInternetAccess).toBe(true);
  });

  test("without a readable guest policy the egress connectors decide, like Python", () => {
    const internet = nativeInfo({ egress: [INTERNET_EGRESS_ARN] });
    expect(infoFromNative(internet, { networkRead: true }).allowInternetAccess).toBe(true);
    expect(infoFromNative(internet, { networkRead: true }).network).toBeUndefined();
    expect(infoFromNative(nativeInfo(), { networkRead: true }).allowInternetAccess).toBeUndefined();
    expect(infoFromNative(internet).allowInternetAccess).toBeUndefined();
    expect(internet.egress).toEqual([INTERNET_EGRESS_ARN]);
  });

  test("list items, terminal states and metrics", () => {
    const item = infoFromNative(
      sandboxListItem({
        sandboxId: "microvm-2",
        state: "PENDING",
        template: IMAGE_ARN,
        templateVersion: "1.0",
        startedAt: STARTED_AT,
        metadata: { b: "2" },
      }),
    );
    expect(item).toMatchObject({
      state: "running",
      endAt: undefined,
      sandboxDomain: undefined,
      metadata: { b: "2" },
      volumeMounts: [],
    });
    expect(() => infoFromNative(nativeInfo({ state: "TERMINATED" }))).toThrow(SandboxNotFoundError);
    const timestamp = new Date();
    expect(
      metricsFromNative({
        timestamp,
        cpuUsedPct: 12.5,
        cpuCount: 2,
        memUsedBytes: 10,
        memTotalBytes: 20,
        memCacheBytes: 3,
        diskUsedBytes: 30,
        diskTotalBytes: 40,
      }),
    ).toEqual({
      timestamp,
      cpuUsedPct: 12.5,
      cpuCount: 2,
      memUsed: 10,
      memTotal: 20,
      memCache: 3,
      diskUsed: 30,
      diskTotal: 40,
    });
  });
});

describe("mapListOptions", () => {
  test("E2B states map to AWS states; metadata narrows running to RUNNING", () => {
    expect(
      mapListOptions({ query: { state: ["running", "paused"] }, limit: 5, order: "asc" }),
    ).toMatchObject({
      states: ["PENDING", "RUNNING", "SUSPENDING", "SUSPENDED"],
      limit: 5,
      order: "asc",
    });
    expect(
      mapListOptions({ query: { metadata: { a: "1" }, state: ["running"] }, nextToken: "t" }),
    ).toMatchObject({ metadata: { a: "1" }, states: ["RUNNING"], nextToken: "t" });
    expect(() => mapListOptions({ query: { state: ["gone" as "running"] } })).toThrow(
      InvalidArgumentError,
    );
  });

  test("metadata with a paused state is the same UnimplementedError as Python, before AWS", () => {
    for (const state of [["paused"], ["running", "paused"]] as const) {
      const error = (() => {
        try {
          mapListOptions({ query: { metadata: { a: "1" }, state: [...state] } });
        } catch (caught) {
          return caught;
        }
        return undefined;
      })();
      expect(error).toBeInstanceOf(UnimplementedError);
      expect(error).toMatchObject({
        feature: LIST_METADATA_STATE_FEATURE,
        reason: "los metadatos viven en el agente; leerlos despertaría el sandbox",
      });
    }
  });
});

describe("the kernel gate", () => {
  test("forwards the three extra kernels and refuses the rest, like Python", () => {
    expect(normalizedLanguageOrUnimplemented(undefined, "runCode")).toBeUndefined();
    expect(normalizedLanguageOrUnimplemented("python", "runCode")).toBeUndefined();
    expect(normalizedLanguageOrUnimplemented("Python", "runCode")).toBeUndefined();
    expect(normalizedLanguageOrUnimplemented("", "runCode")).toBeUndefined();
    expect(normalizedLanguageOrUnimplemented("bash", "runCode")).toBe("bash");
    expect(normalizedLanguageOrUnimplemented("JS", "runCode")).toBe("javascript");
    expect(normalizedLanguageOrUnimplemented("ts", "runCode")).toBe("typescript");
    expect(normalizedLanguageOrUnimplemented("TypeScript", "runCode")).toBe("typescript");
    const r = caught(() => normalizedLanguageOrUnimplemented("r", "runCode")) as UnimplementedError;
    expect(r).toBeInstanceOf(UnimplementedError);
    expect(r.feature).toBe('runCode({ language: "r" })');
    expect(r.reason).toBe(AVAILABLE_KERNELS_REASON);
    expect(r.doc).toBe(COMPAT_DOC_PATH);
    expect(r.cause).toBeInstanceOf(InvalidArgumentError);
    expect(
      (caught(() => normalizedLanguageOrUnimplemented("java", "createCodeContext")) as Error)
        .message,
    ).toContain("createCodeContext");
  });

  test("unimplementedLanguage maps only an agent Unimplemented", () => {
    const notShipped = new InvalidArgumentError(
      "language typescript is not installed in this image; use rayito-base-poly",
      { grpcCode: Code.Unimplemented },
    );
    const mapped = unimplementedLanguage(notShipped, "runCode", "typescript");
    expect(mapped).toBeInstanceOf(UnimplementedError);
    expect(mapped?.feature).toBe('runCode({ language: "typescript" })');
    expect(mapped?.reason).toBe(POLY_KERNELS_REASON);
    expect(mapped?.cause).toBe(notShipped);
    const rejected = new InvalidArgumentError("language must be one of python, bash, javascript", {
      grpcCode: Code.InvalidArgument,
    });
    expect(unimplementedLanguage(rejected, "runCode", "typescript")).toBeUndefined();
    expect(unimplementedLanguage(new SandboxError("x"), "runCode", "bash")).toBeUndefined();
    expect(unimplementedLanguage(new Error("x"), "runCode", "bash")).toBeUndefined();
  });

  test.skipIf(!existsSync(PYTHON_COMPAT))(
    "the kernel reasons are byte-identical to Python's",
    () => {
      const joined = readFileSync(PYTHON_COMPAT, "utf8").replace(/"\s*\n\s*"/g, "");
      expect(joined).toContain(AVAILABLE_KERNELS_REASON);
      expect(joined).toContain(POLY_KERNELS_REASON);
    },
  );
});

describe("settleAsyncCallback", () => {
  test("a rejection goes to onRejected; a sync throw still reaches the caller", async () => {
    const rejected: unknown[] = [];
    const failure = new Error("async");
    const wrapped = settleAsyncCallback(
      async (_value: number) => {
        throw failure;
      },
      (error) => rejected.push(error),
    );
    expect(wrapped(1)).toBeUndefined();
    await new Promise((resolve) => setImmediate(resolve));
    expect(rejected).toEqual([failure]);
    const seen: number[] = [];
    settleAsyncCallback(
      (value: number) => {
        seen.push(value);
      },
      (error) => rejected.push(error),
    )(2);
    await new Promise((resolve) => setImmediate(resolve));
    expect(seen).toEqual([2]);
    expect(rejected).toHaveLength(1);
    const sync = settleAsyncCallback(
      (_value: number) => {
        throw new Error("sync");
      },
      (error) => rejected.push(error),
    );
    expect(() => sync(3)).toThrow("sync");
  });
});
