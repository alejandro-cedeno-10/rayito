/**
 * Los helpers puros de la política de egress (`src/sandbox/network.ts`), el
 * `UnimplementedError` nativo y el campo `egressEnforcement` de `Health`:
 * los mismos casos que `tests/unit/test_network_base.py` del SDK Python.
 */

import { create } from "@bufbuild/protobuf";
import { Code, ConnectError } from "@connectrpc/connect";
import { describe, expect, test } from "vitest";
import {
  InvalidArgumentError,
  SandboxError,
  TimeoutError,
  UnimplementedError,
} from "../../src/errors.js";
import { HealthResponseSchema } from "../../src/gen/rayito/v1/health_pb.js";
import {
  EgressEnforcement as EnforcementMessage,
  NetworkStateSchema,
} from "../../src/gen/rayito/v1/network_pb.js";
import * as rayito from "../../src/index.js";
import { ALL_TRAFFIC, EgressEnforcement, type NetworkSelectorContext } from "../../src/models.js";
import {
  ALLOW_INTERNET_ACCESS_FEATURE,
  ALLOW_ONLY_NOTICE,
  CAPS_REASON,
  EGRESS_UNAVAILABLE_REASON,
  egressFeature,
  egressGateError,
  enforcementFromProto,
  isEmptyPolicy,
  isNetworkEntry,
  logAllowOnlyNotice,
  NETWORK_FEATURE,
  networkRpcError,
  OLD_AGENT_REASON,
  POOL_NETWORK_MESSAGE,
  policyToProto,
  type ResolvedNetworkPolicy,
  rejectNetworkWithPool,
  requiresEnforcement,
  resolveNetwork,
  resolveSelector,
  selectorContext,
  stateFromProto,
  updateNetworkRequest,
  validatePolicyShape,
} from "../../src/sandbox/network.js";
import { healthFromProto } from "../../src/sandbox/readiness.js";
import { RecordingLogger } from "./helpers.js";

const SANDBOX = "microvm-00000000-0000-0000-0000-000000000001";

function policy(fields: Partial<ResolvedNetworkPolicy> = {}): ResolvedNetworkPolicy {
  return { allowOut: [], denyOut: [], egressProxy: undefined, ...fields };
}

function caught(action: () => unknown): Error {
  try {
    action();
  } catch (error) {
    return error as Error;
  }
  throw new Error("no lanzó");
}

describe("UnimplementedError", () => {
  test("is an Error outside the SandboxError hierarchy with feature and reason", () => {
    const error = new UnimplementedError("network", "usa rayito-base-caps");
    expect(error).toBeInstanceOf(Error);
    expect(error).not.toBeInstanceOf(SandboxError);
    expect(error.name).toBe("UnimplementedError");
    expect(Object.getPrototypeOf(error)).toBe(UnimplementedError.prototype);
    expect(error.feature).toBe("network");
    expect(error.reason).toBe("usa rayito-base-caps");
    expect(error.message).toBe("network no está disponible: usa rayito-base-caps");
  });

  test("the package root exports the egress surface", () => {
    expect(rayito.UnimplementedError).toBe(UnimplementedError);
    expect(rayito.ALL_TRAFFIC).toBe("0.0.0.0/0");
    expect(rayito.EgressEnforcement.GUEST_ROUTES_AND_PROXY).toBe("guest_routes_and_proxy");
  });
});

describe("selectors", () => {
  test("a list is copied and a callable receives allTraffic and empty rules", () => {
    const source = ["10.0.0.0/8"];
    const copied = resolveSelector(source, "allowOut", selectorContext());
    expect(copied).toEqual(source);
    expect(copied).not.toBe(source);
    let seen: NetworkSelectorContext | undefined;
    const resolved = resolveSelector(
      (ctx) => {
        seen = ctx;
        return [ctx.allTraffic];
      },
      "denyOut",
      selectorContext(),
    );
    expect(resolved).toEqual([ALL_TRAFFIC]);
    expect(seen?.allTraffic).toBe("0.0.0.0/0");
    expect(seen?.rules.size).toBe(0);
    expect(resolveSelector(undefined, "allowOut", selectorContext())).toEqual([]);
  });

  test("a selector that does not yield a list of strings is InvalidArgumentError without the entry", () => {
    const notAList = caught(() =>
      resolveSelector((() => "1.2.3.4") as never, "denyOut", selectorContext()),
    );
    expect(notAList).toBeInstanceOf(InvalidArgumentError);
    expect(notAList.message).toContain("denyOut");
    const badEntry = caught(() =>
      resolveSelector(["ok.example", 7] as never, "allowOut", selectorContext()),
    );
    expect(badEntry).toBeInstanceOf(InvalidArgumentError);
    expect(badEntry.message).toContain("allowOut[1]");
    expect(badEntry.message).not.toContain("ok.example");
  });
});

describe("resolveNetwork", () => {
  test("nothing, an empty object and allowInternetAccess true are unrestricted", () => {
    for (const resolved of [
      resolveNetwork(undefined),
      resolveNetwork({}),
      resolveNetwork(undefined, { allowInternetAccess: true }),
    ]) {
      expect(resolved).toEqual(policy());
      expect(isEmptyPolicy(resolved)).toBe(true);
      expect(requiresEnforcement(resolved)).toBe(false);
    }
  });

  test("allowInternetAccess false appends ALL_TRAFFIC to denyOut once", () => {
    expect(resolveNetwork(undefined, { allowInternetAccess: false }).denyOut).toEqual([
      ALL_TRAFFIC,
    ]);
    const merged = resolveNetwork(
      { allowOut: ["1.2.3.4/32"], denyOut: ["10.0.0.0/8"] },
      { allowInternetAccess: false },
    );
    expect(merged.denyOut).toEqual(["10.0.0.0/8", ALL_TRAFFIC]);
    expect(merged.allowOut).toEqual(["1.2.3.4/32"]);
    const already = resolveNetwork({ denyOut: [ALL_TRAFFIC] }, { allowInternetAccess: false });
    expect(already.denyOut).toEqual([ALL_TRAFFIC]);
  });

  test("the egress proxy keeps only the fields that were given", () => {
    const resolved = resolveNetwork({ egressProxy: { address: "10.0.0.5:1080" } });
    expect(resolved.egressProxy).toEqual({ address: "10.0.0.5:1080" });
    expect(Object.keys(resolved.egressProxy ?? {})).toEqual(["address"]);
    expect(requiresEnforcement(resolved)).toBe(true);
  });

  test("unknown keys, wrong types and a non-boolean flag are InvalidArgumentError", () => {
    const unknown = caught(() => resolveNetwork({ rules: {} } as never));
    expect(unknown).toBeInstanceOf(InvalidArgumentError);
    expect(unknown.message).toContain("'rules'");
    const proxyKey = caught(() =>
      resolveNetwork({ egressProxy: { address: "h:1", token: "x" } } as never),
    );
    expect(proxyKey.message).toContain("'token'");
    expect(() => resolveNetwork(["1.2.3.4"] as never)).toThrow(InvalidArgumentError);
    expect(() => resolveNetwork({ egressProxy: "h:1" } as never)).toThrow(InvalidArgumentError);
    expect(() => resolveNetwork({ egressProxy: { address: 1 } } as never)).toThrow(
      InvalidArgumentError,
    );
    expect(() => resolveNetwork(undefined, { allowInternetAccess: "no" as never })).toThrow(
      InvalidArgumentError,
    );
  });

  test("only denyOut or an egress proxy require enforcement", () => {
    expect(requiresEnforcement(policy({ allowOut: ["example.com"] }))).toBe(false);
    expect(requiresEnforcement(policy({ denyOut: ["10.0.0.0/8"] }))).toBe(true);
    expect(requiresEnforcement(policy({ egressProxy: { address: "h:1080" } }))).toBe(true);
  });

  test("the gate feature names the flag only when the policy came from it", () => {
    expect(egressFeature(resolveNetwork(undefined, { allowInternetAccess: false }), false)).toBe(
      ALLOW_INTERNET_ACCESS_FEATURE,
    );
    expect(egressFeature(resolveNetwork({ denyOut: [ALL_TRAFFIC] }), undefined)).toBe(
      NETWORK_FEATURE,
    );
    expect(
      egressFeature(
        resolveNetwork({ allowOut: ["1.2.3.4"] }, { allowInternetAccess: false }),
        false,
      ),
    ).toBe(NETWORK_FEATURE);
  });

  test("a pool refuses any non-empty policy", () => {
    expect(() => rejectNetworkWithPool(policy())).not.toThrow();
    expect(() => rejectNetworkWithPool(policy({ allowOut: ["1.2.3.4"] }))).toThrow(
      POOL_NETWORK_MESSAGE,
    );
    expect(() =>
      rejectNetworkWithPool(resolveNetwork(undefined, { allowInternetAccess: false })),
    ).toThrow(InvalidArgumentError);
  });
});

describe("validatePolicyShape", () => {
  test.each([
    ["1.2.3.4", true],
    ["10.0.0.0/8", true],
    ["0.0.0.0/0", true],
    ["::/0", true],
    ["2001:db8::1/128", true],
    ["1.2.3.4/33", false],
    ["::1/129", false],
    ["1.2.3.4/", false],
    ["1.2.3.4/abc", false],
    ["[::1]", false],
    ["example.com", false],
    ["*.example.com", false],
  ])("isNetworkEntry(%j) is %s", (entry, expected) => {
    expect(isNetworkEntry(entry)).toBe(expected);
  });

  test("a valid mixed policy passes", () => {
    expect(() =>
      validatePolicyShape(
        policy({
          allowOut: ["*.example.com", "api.example.org", "1.2.3.4/32", "::1"],
          denyOut: [ALL_TRAFFIC, "::/0"],
          egressProxy: { address: "[2001:db8::5]:1080", username: "u", password: "p" },
        }),
      ),
    ).not.toThrow();
  });

  test("a hostname in denyOut names the index, never the entry", () => {
    const error = caught(() =>
      validatePolicyShape(policy({ denyOut: ["10.0.0.0/8", "secret-host.example"] })),
    );
    expect(error).toBeInstanceOf(InvalidArgumentError);
    expect(error.message).toContain("denyOut[1]");
    expect(error.message).not.toContain("secret-host");
  });

  test("list, hostname and length caps come from limits.json", () => {
    const many = Array.from(
      { length: 257 },
      (_, index) => `10.0.${Math.floor(index / 256)}.${index % 256}`,
    );
    expect(() => validatePolicyShape(policy({ denyOut: many }))).toThrow(/256/);
    expect(() => validatePolicyShape(policy({ denyOut: many.slice(0, 256) }))).not.toThrow();
    const hostnames = Array.from({ length: 65 }, (_, index) => `h${index}.example.com`);
    expect(() => validatePolicyShape(policy({ allowOut: hostnames }))).toThrow(/64/);
    expect(() => validatePolicyShape(policy({ allowOut: hostnames.slice(0, 64) }))).not.toThrow();
    const longName = `${"a".repeat(63)}.${"b".repeat(63)}.${"c".repeat(63)}.${"d".repeat(62)}.e`;
    expect(longName.length).toBe(256);
    expect(() => validatePolicyShape(policy({ allowOut: [longName] }))).toThrow(/253/);
    const fits = `${"a".repeat(63)}.${"b".repeat(63)}.${"c".repeat(63)}.${"d".repeat(61)}`;
    expect(fits.length).toBe(253);
    expect(() => validatePolicyShape(policy({ allowOut: [`*.${fits}.`] }))).not.toThrow();
  });

  test("empty entries are refused with their index", () => {
    expect(() => validatePolicyShape(policy({ allowOut: ["1.2.3.4", " "] }))).toThrow(
      /allowOut\[1\]/,
    );
  });

  test("proxy address, credential bytes and a password without username", () => {
    expect(() => validatePolicyShape(policy({ egressProxy: { address: "" } }))).toThrow(
      /egressProxy\.address/,
    );
    expect(() =>
      validatePolicyShape(policy({ egressProxy: { address: "h:1", password: "p" } })),
    ).toThrow(/username/);
    const tooLong = "ñ".repeat(128);
    expect(Buffer.byteLength(tooLong)).toBe(256);
    const error = caught(() =>
      validatePolicyShape(policy({ egressProxy: { address: "h:1", username: tooLong } })),
    );
    expect(error.message).toContain("255");
    expect(error.message).not.toContain("ñ");
    expect(() =>
      validatePolicyShape(policy({ egressProxy: { address: "h:1", username: "" } })),
    ).toThrow(InvalidArgumentError);
    expect(() =>
      validatePolicyShape(
        policy({ egressProxy: { address: "h:1", username: "u", password: "x".repeat(255) } }),
      ),
    ).not.toThrow();
  });
});

describe("proto mapping", () => {
  test("policyToProto carries the lists and the proxy fields that exist", () => {
    const message = policyToProto(
      policy({
        allowOut: ["api.example.com"],
        denyOut: [ALL_TRAFFIC],
        egressProxy: { address: "10.0.0.5:1080", username: "u" },
      }),
    );
    expect(message.allowOut).toEqual(["api.example.com"]);
    expect(message.denyOut).toEqual([ALL_TRAFFIC]);
    expect(message.egressProxy?.address).toBe("10.0.0.5:1080");
    expect(message.egressProxy?.username).toBe("u");
    expect(message.egressProxy?.password).toBeUndefined();
    const request = updateNetworkRequest(policy());
    expect(request.policy?.allowOut).toEqual([]);
    expect(request.policy?.egressProxy).toBeUndefined();
  });

  test("stateFromProto maps enforcement and treats port 0 as no proxy", () => {
    const state = stateFromProto(
      create(NetworkStateSchema, {
        allowOut: ["a.example"],
        denyOut: [ALL_TRAFFIC],
        egressProxyConfigured: true,
        enforcement: EnforcementMessage.GUEST_ROUTES_AND_PROXY,
        localProxyPort: 41_234,
      }),
    );
    expect(state).toEqual({
      allowOut: ["a.example"],
      denyOut: [ALL_TRAFFIC],
      egressProxyConfigured: true,
      enforcement: "guest_routes_and_proxy",
      localProxyPort: 41_234,
    });
    expect(Object.isFrozen(state)).toBe(true);
    expect(stateFromProto(create(NetworkStateSchema, {})).localProxyPort).toBeUndefined();
  });

  test.each([
    [EnforcementMessage.UNSPECIFIED, "unspecified"],
    [EnforcementMessage.NONE, "none"],
    [EnforcementMessage.GUEST_ROUTES, "guest_routes"],
    [EnforcementMessage.GUEST_ROUTES_AND_PROXY, "guest_routes_and_proxy"],
    [99 as EnforcementMessage, "unspecified"],
  ])("enforcement %i maps to %s", (value, expected) => {
    expect(enforcementFromProto(value)).toBe(expected);
  });

  test("healthFromProto fills egressEnforcement (unspecified on an older agent)", () => {
    expect(healthFromProto(create(HealthResponseSchema, {})).egressEnforcement).toBe("unspecified");
    expect(
      healthFromProto(
        create(HealthResponseSchema, { egressEnforcement: EnforcementMessage.GUEST_ROUTES }),
      ).egressEnforcement,
    ).toBe(EgressEnforcement.GUEST_ROUTES);
  });
});

describe("gate and errors", () => {
  test.each(["unspecified", "none"] as const)(
    "the gate refuses %s naming rayito-base-caps and the VPC connector",
    (enforcement) => {
      const error = egressGateError(SANDBOX, enforcement, ALLOW_INTERNET_ACCESS_FEATURE);
      expect(error).toBeInstanceOf(UnimplementedError);
      expect(error?.feature).toBe("allowInternetAccess: false");
      expect(error?.reason).toContain(EGRESS_UNAVAILABLE_REASON);
      expect(error?.reason).toContain("rayito-base-caps");
      expect(error?.reason).toContain("infra/egress-connector.yaml");
      expect(error?.reason).toContain(SANDBOX);
    },
  );

  test.each(["guest_routes", "guest_routes_and_proxy"] as const)(
    "the gate lets %s through",
    (enforcement) => {
      expect(egressGateError(SANDBOX, enforcement, NETWORK_FEATURE)).toBeUndefined();
    },
  );

  test("FailedPrecondition and Unimplemented become UnimplementedError", () => {
    const caps = networkRpcError(
      new ConnectError("la imagen no tiene CAP_NET_ADMIN", Code.FailedPrecondition),
      "updateNetwork",
    );
    expect(caps).toBeInstanceOf(UnimplementedError);
    expect((caps as UnimplementedError).feature).toBe("updateNetwork");
    expect((caps as UnimplementedError).reason).toBe(CAPS_REASON);
    const old = networkRpcError(new ConnectError("", Code.Unimplemented), "getNetwork");
    expect(old).toBeInstanceOf(UnimplementedError);
    expect((old as UnimplementedError).reason).toBe(OLD_AGENT_REASON);
    expect(OLD_AGENT_REASON).toContain("una imagen M9 de rayito-base-caps");
  });

  test("the rest follow the unary table", () => {
    expect(
      networkRpcError(new ConnectError("allow_out[3]", Code.InvalidArgument), "updateNetwork"),
    ).toBeInstanceOf(InvalidArgumentError);
    const internal = networkRpcError(
      new ConnectError("egress_update_failed: fill_table", Code.Internal),
      "updateNetwork",
    );
    expect(internal).toBeInstanceOf(SandboxError);
    expect(internal).not.toBeInstanceOf(InvalidArgumentError);
    expect(
      networkRpcError(new ConnectError("slow", Code.DeadlineExceeded), "updateNetwork"),
    ).toBeInstanceOf(TimeoutError);
  });
});

describe("allow-only notice", () => {
  test("logged once per logger, only for allowOut without denyOut or proxy", () => {
    const logger = new RecordingLogger();
    logAllowOnlyNotice(policy(), logger);
    logAllowOnlyNotice(policy({ allowOut: ["x.example"], denyOut: [ALL_TRAFFIC] }), logger);
    expect(logger.lines).toHaveLength(0);
    logAllowOnlyNotice(policy({ allowOut: ["secret.example"] }), logger);
    logAllowOnlyNotice(policy({ allowOut: ["secret.example"] }), logger);
    expect(logger.at("info").map((line) => line.message)).toEqual([ALLOW_ONLY_NOTICE]);
    expect(logger.dump()).not.toContain("secret.example");
    expect(() => logAllowOnlyNotice(policy({ allowOut: ["a.example"] }), undefined)).not.toThrow();
  });
});
