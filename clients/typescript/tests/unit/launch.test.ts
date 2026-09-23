import { inspect } from "node:util";
import { afterEach, describe, expect, test } from "vitest";
import { PortSpec } from "../../src/aws/control-plane.js";
import {
  AuthenticationError,
  InvalidArgumentError,
  SandboxLifetimeError,
} from "../../src/errors.js";
import {
  buildLaunchPlan,
  connectorArns,
  loggingConfig,
  proxyPortSpecs,
  requireAccessToken,
  resolveAccessToken,
  resolveIdlePolicy,
  resolveTemplate,
  validateHostPort,
  validateSandboxId,
  validateTimeoutMs,
} from "../../src/sandbox/launch.js";
import { IMAGE_ARN, REGION } from "./fake/control-plane.js";
import { ACCESS_TOKEN } from "./helpers.js";

const ENV_KEYS = ["RAYITO_TEMPLATE", "RAYITO_ACCESS_TOKEN"] as const;
const saved = new Map<string, string | undefined>();

afterEach(() => {
  for (const key of ENV_KEYS) {
    const value = saved.get(key);
    if (value === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = value;
    }
  }
  saved.clear();
});

function setEnv(key: (typeof ENV_KEYS)[number], value: string | undefined): void {
  if (!saved.has(key)) {
    saved.set(key, process.env[key]);
  }
  if (value === undefined) {
    delete process.env[key];
  } else {
    process.env[key] = value;
  }
}

describe("template and access token resolution", () => {
  test("template comes from the argument or RAYITO_TEMPLATE", () => {
    setEnv("RAYITO_TEMPLATE", undefined);
    expect(resolveTemplate("base")).toBe("base");
    expect(() => resolveTemplate(undefined)).toThrow(InvalidArgumentError);
    setEnv("RAYITO_TEMPLATE", "from-env");
    expect(resolveTemplate(undefined)).toBe("from-env");
    expect(resolveTemplate("")).toBe("from-env");
  });

  test("access token is validated, taken from the environment, or generated", () => {
    setEnv("RAYITO_ACCESS_TOKEN", undefined);
    expect(resolveAccessToken(ACCESS_TOKEN)).toBe(ACCESS_TOKEN);
    expect(resolveAccessToken(undefined)).toHaveLength(43);
    expect(() => resolveAccessToken("not base64!")).toThrow(InvalidArgumentError);
    setEnv("RAYITO_ACCESS_TOKEN", ACCESS_TOKEN);
    expect(resolveAccessToken(undefined)).toBe(ACCESS_TOKEN);
    expect(requireAccessToken(undefined)).toBe(ACCESS_TOKEN);
    setEnv("RAYITO_ACCESS_TOKEN", undefined);
    expect(() => requireAccessToken(undefined)).toThrow(AuthenticationError);
  });

  test("sandbox ids are validated by length only", () => {
    expect(validateSandboxId("microvm-x")).toBe("microvm-x");
    expect(() => validateSandboxId("")).toThrow(InvalidArgumentError);
    expect(() => validateSandboxId("x".repeat(257))).toThrow(InvalidArgumentError);
    expect(() => validateSandboxId(42)).toThrow(InvalidArgumentError);
  });
});

describe("timeoutMs", () => {
  test("ceil to seconds within the API bounds", () => {
    expect(validateTimeoutMs(3_600_000)).toBe(3600);
    expect(validateTimeoutMs(1500)).toBe(2);
    expect(validateTimeoutMs(1000)).toBe(1);
    expect(validateTimeoutMs(28_800_000)).toBe(28_800);
  });

  test("above 8 h is a lifetime error, below 1 s or non-integer an argument error", () => {
    expect(() => validateTimeoutMs(28_800_001)).toThrow(SandboxLifetimeError);
    expect(() => validateTimeoutMs(999)).toThrow(InvalidArgumentError);
    expect(() => validateTimeoutMs(1000.5)).toThrow(InvalidArgumentError);
    expect(() => validateTimeoutMs("3600")).toThrow(InvalidArgumentError);
  });
});

describe("idle policy resolution", () => {
  test("fills suspendedDurationSeconds with timeout minus maxIdle", () => {
    expect(resolveIdlePolicy(undefined, 3600)).toEqual({
      maxIdleSeconds: 300,
      suspendedDurationSeconds: 3300,
      autoResume: true,
    });
    expect(resolveIdlePolicy({ maxIdleSeconds: 60, suspendedDurationSeconds: 10 }, 3600)).toEqual({
      maxIdleSeconds: 60,
      suspendedDurationSeconds: 10,
      autoResume: true,
    });
    expect(resolveIdlePolicy(null, 3600)).toBeUndefined();
  });

  test("maxIdleSeconds must be below the timeout", () => {
    expect(() => resolveIdlePolicy({ maxIdleSeconds: 3600 }, 3600)).toThrow(InvalidArgumentError);
    expect(() => resolveIdlePolicy({ maxIdleSeconds: 30 }, 3600)).toThrow(InvalidArgumentError);
  });
});

describe("ports", () => {
  test("8080 first, ranges, duplicates dropped, 9000 refused", () => {
    expect(proxyPortSpecs(undefined)).toEqual([PortSpec.single(8080)]);
    expect(proxyPortSpecs([3000, [4000, 4010], 3000, 8080])).toEqual([
      PortSpec.single(8080),
      PortSpec.single(3000),
      PortSpec.range(4000, 4010),
    ]);
    expect(() => proxyPortSpecs([9000])).toThrow(InvalidArgumentError);
    expect(() => proxyPortSpecs([[8000, 9500]])).toThrow(InvalidArgumentError);
    expect(() => proxyPortSpecs([[10, 5]])).toThrow(InvalidArgumentError);
    expect(() => proxyPortSpecs([0])).toThrow(InvalidArgumentError);
    expect(() => proxyPortSpecs([[1, 2, 3] as unknown as [number, number]])).toThrow(
      InvalidArgumentError,
    );
  });

  test("getHost refuses the hooks port", () => {
    expect(validateHostPort(3000)).toBe(3000);
    expect(() => validateHostPort(9000)).toThrow(InvalidArgumentError);
    expect(() => validateHostPort(70000)).toThrow(InvalidArgumentError);
  });
});

describe("connectors and logging", () => {
  test("managed names become ARNs, ARNs pass through, others fail", () => {
    expect(connectorArns(undefined, REGION, "ingress")).toEqual([]);
    expect(connectorArns(["ALL_INGRESS", "arn:aws:x"], REGION, "ingress")).toEqual([
      "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS",
      "arn:aws:x",
    ]);
    expect(() => connectorArns(["nope"], REGION, "egress")).toThrow(InvalidArgumentError);
    expect(() => connectorArns(Array(11).fill("arn:aws:x"), REGION, "egress")).toThrow(
      InvalidArgumentError,
    );
  });

  test("logging options", () => {
    expect(loggingConfig("disabled", "base")).toEqual({ disabled: {} });
    expect(loggingConfig("cloudwatch", "base")).toEqual({
      cloudWatch: { logGroup: "/rayito/base" },
    });
    expect(loggingConfig({ cloudWatch: { logGroup: "/x" } }, "base")).toEqual({
      cloudWatch: { logGroup: "/x" },
    });
    expect(() => loggingConfig("verbose" as never, "base")).toThrow(InvalidArgumentError);
    expect(() => loggingConfig({ other: {} } as never, "base")).toThrow(InvalidArgumentError);
  });
});

describe("buildLaunchPlan", () => {
  test("inspecting or serializing the plan never shows the token or the payload", () => {
    const envSecret = "env-secret-value-1234";
    const plan = buildLaunchPlan({
      imageArn: IMAGE_ARN,
      region: REGION,
      envs: { API_KEY: envSecret },
      accessToken: ACCESS_TOKEN,
    });
    expect(plan.accessToken).toBe(ACCESS_TOKEN);
    expect(plan.request.runHookPayload).toContain(envSecret);
    expect(plan.request.toApi().runHookPayload).toBe(plan.request.runHookPayload);
    for (const printed of [
      inspect(plan, { depth: 10 }),
      JSON.stringify(plan),
      String({ ...plan }),
    ]) {
      expect(printed).not.toContain(ACCESS_TOKEN);
      expect(printed).not.toContain(envSecret);
    }
    expect(Object.keys(plan)).not.toContain("accessToken");
    expect(Object.keys(plan.request)).not.toContain("runHookPayload");
    expect(inspect(plan, { depth: 10 })).toContain("rayito-base");
  });

  test("produces the exact run-microvm input", () => {
    const plan = buildLaunchPlan({
      imageArn: IMAGE_ARN,
      region: REGION,
      timeoutMs: 3_600_000,
      envs: { A: "1" },
      allowedPorts: [3000],
      ingress: ["ALL_INGRESS"],
      logging: "cloudwatch",
      accessToken: ACCESS_TOKEN,
    });
    const api = plan.request.toApi();
    expect(Object.keys(api).sort()).toEqual(
      [
        "clientToken",
        "idlePolicy",
        "imageIdentifier",
        "ingressNetworkConnectors",
        "logging",
        "maximumDurationInSeconds",
        "runHookPayload",
      ].sort(),
    );
    expect(api.imageIdentifier).toBe(IMAGE_ARN);
    expect(api.maximumDurationInSeconds).toBe(3600);
    expect(api.clientToken).toMatch(/^[0-9a-f]{32}$/);
    expect(api.logging).toEqual({ cloudWatch: { logGroup: "/rayito/rayito-base-2gb" } });
    expect(api.idlePolicy).toEqual({
      maxIdleDurationSeconds: 300,
      suspendedDurationSeconds: 3300,
      autoResumeEnabled: true,
    });
    expect(api.ingressNetworkConnectors).toEqual([
      "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS",
    ]);
    expect(JSON.parse(api.runHookPayload)).toMatchObject({
      v: 1,
      user: "user",
      workdir: "/home/user",
      envs: { A: "1" },
    });
    expect(plan.proxyPorts).toEqual([PortSpec.single(8080), PortSpec.single(3000)]);
    expect(plan.accessToken).toBe(ACCESS_TOKEN);
  });

  test("defaults: one hour, default idle, logging disabled, no connectors", () => {
    const plan = buildLaunchPlan({
      imageArn: IMAGE_ARN,
      region: REGION,
      accessToken: ACCESS_TOKEN,
    });
    const api = plan.request.toApi();
    expect(api.maximumDurationInSeconds).toBe(3600);
    expect(api.logging).toEqual({ disabled: {} });
    expect(api).not.toHaveProperty("ingressNetworkConnectors");
    expect(api).not.toHaveProperty("executionRoleArn");
    expect(api).not.toHaveProperty("imageVersion");
  });

  test("idle null and the optional fields", () => {
    const api = buildLaunchPlan({
      imageArn: IMAGE_ARN,
      region: REGION,
      accessToken: ACCESS_TOKEN,
      idle: null,
      templateVersion: "2.0",
      executionRoleArn: "arn:aws:iam::123456789012:role/x",
      egress: ["INTERNET_EGRESS"],
    }).request.toApi();
    expect(api).not.toHaveProperty("idlePolicy");
    expect(api.imageVersion).toBe("2.0");
    expect(api.executionRoleArn).toBe("arn:aws:iam::123456789012:role/x");
    expect(api.egressNetworkConnectors).toHaveLength(1);
  });

  test("networkEnforce puts only the network flag in the payload and keeps the connectors", () => {
    const base = { imageArn: IMAGE_ARN, region: REGION, accessToken: ACCESS_TOKEN };
    const enforced = buildLaunchPlan({ ...base, networkEnforce: true }).request.toApi();
    expect(JSON.parse(enforced.runHookPayload).network).toEqual({ enforce: true });
    expect(enforced).not.toHaveProperty("egressNetworkConnectors");
    for (const plain of [
      buildLaunchPlan(base),
      buildLaunchPlan({ ...base, networkEnforce: false }),
    ]) {
      expect(JSON.parse(plain.request.toApi().runHookPayload)).not.toHaveProperty("network");
    }
  });

  test("no lifecycle block unless maxLifetimeMs or onTimeout is given", () => {
    const plan = buildLaunchPlan({
      imageArn: IMAGE_ARN,
      region: REGION,
      accessToken: ACCESS_TOKEN,
      timeoutMs: 900_000,
    });
    const api = plan.request.toApi();
    expect(api.maximumDurationInSeconds).toBe(900);
    expect(JSON.parse(api.runHookPayload)).not.toHaveProperty("lifecycle");
    expect(plan.lifecycleRequested).toBe(false);
  });

  test("a pause launch carries the block, the cap and a platform idle that auto-resumes", () => {
    const plan = buildLaunchPlan({
      imageArn: IMAGE_ARN,
      region: REGION,
      accessToken: ACCESS_TOKEN,
      timeoutMs: 60_000,
      maxLifetimeMs: 900_000,
      onTimeout: "pause",
      idle: { autoResume: false },
    });
    const api = plan.request.toApi();
    expect(api.maximumDurationInSeconds).toBe(900);
    expect(api.idlePolicy).toEqual({
      maxIdleDurationSeconds: 300,
      suspendedDurationSeconds: 600,
      autoResumeEnabled: true,
    });
    expect(JSON.parse(api.runHookPayload).lifecycle).toEqual({
      auto_resume: false,
      cap_s: 900,
      on_timeout: "pause",
      timeout_s: 60,
    });
    expect(plan.lifecycleRequested).toBe(true);
  });

  test("a kill launch defaults maxLifetimeMs to timeout plus the margin", () => {
    const plan = buildLaunchPlan({
      imageArn: IMAGE_ARN,
      region: REGION,
      accessToken: ACCESS_TOKEN,
      timeoutMs: 1500,
      onTimeout: "kill",
      idle: null,
    });
    const api = plan.request.toApi();
    expect(api.maximumDurationInSeconds).toBe(120);
    expect(JSON.parse(api.runHookPayload).lifecycle).toEqual({
      auto_resume: false,
      cap_s: 120,
      on_timeout: "kill",
      timeout_s: 2,
    });
  });

  test("pause without idle and a cap above 8 h fail before any AWS call", () => {
    const base = { imageArn: IMAGE_ARN, region: REGION, accessToken: ACCESS_TOKEN };
    expect(() => buildLaunchPlan({ ...base, onTimeout: "pause", idle: null })).toThrow(
      InvalidArgumentError,
    );
    expect(() => buildLaunchPlan({ ...base, maxLifetimeMs: 28_801_000 })).toThrow(
      SandboxLifetimeError,
    );
  });

  test("an oversized payload fails before any AWS call", () => {
    expect(() =>
      buildLaunchPlan({
        imageArn: IMAGE_ARN,
        region: REGION,
        accessToken: ACCESS_TOKEN,
        envs: { BIG: "x".repeat(5000) },
      }),
    ).toThrow(InvalidArgumentError);
  });
});
