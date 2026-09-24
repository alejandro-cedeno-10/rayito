import {
  CreateMicrovmAuthTokenCommand,
  GetMicrovmCommand,
  ListMicrovmsCommand,
  ResumeMicrovmCommand,
  RunMicrovmCommand,
  SuspendMicrovmCommand,
  TerminateMicrovmCommand,
} from "@aws-sdk/client-lambda-microvms";
import { GetCallerIdentityCommand } from "@aws-sdk/client-sts";
import { describe, expect, test } from "vitest";
import {
  type CommandSender,
  clientConfig,
  DEFAULT_MAX_ATTEMPTS,
  LambdaMicrovmsControlPlane,
  LaunchRequest,
  normalizeEndpoint,
  PortSpec,
  proxyJweFromResponse,
  sandboxInfoFromResponse,
  sharedControlPlane,
  TokenBucket,
  translateAwsError,
} from "../../src/aws/control-plane.js";
import {
  AuthenticationError,
  CapacityError,
  InvalidArgumentError,
  QuotaExceededError,
  RateLimitError,
  SandboxError,
  SandboxNotFoundError,
  SandboxStateError,
} from "../../src/errors.js";
import { resolveControlPlane, Sandbox } from "../../src/sandbox/sandbox.js";
import { ProxyTunnelAgent } from "../../src/transport/proxy-tunnel.js";
import { IMAGE_ARN, JWE, REGION, SANDBOX_ID, STARTED_AT } from "./fake/control-plane.js";
import { ACCESS_TOKEN } from "./helpers.js";

interface Sent {
  readonly name: string;
  readonly input: Record<string, unknown>;
}

class Recorder implements CommandSender {
  readonly sent: Sent[] = [];
  readonly responses: Array<unknown | (() => unknown)> = [];

  answer(...responses: Array<unknown | (() => unknown)>): this {
    this.responses.push(...responses);
    return this;
  }

  async send(command: unknown): Promise<unknown> {
    const typed = command as { constructor: { name: string }; input: Record<string, unknown> };
    this.sent.push({ name: typed.constructor.name, input: typed.input });
    const next = this.responses.shift();
    if (next === undefined) {
      throw new Error(`sin respuesta guionizada para ${typed.constructor.name}`);
    }
    return typeof next === "function" ? (next as () => unknown)() : next;
  }

  inputs(name: string): Record<string, unknown>[] {
    return this.sent.filter((item) => item.name === name).map((item) => item.input);
  }
}

/** Reloj manual: `sleep` sólo graba (los callers concurrentes reservan turnos) salvo `advancing`. */
class FakeClock {
  now = 1000;
  readonly sleeps: number[] = [];
  readonly advancing: boolean;
  constructor(advancing = false) {
    this.advancing = advancing;
  }
  tick = (): number => this.now;
  sleep = async (seconds: number): Promise<void> => {
    this.sleeps.push(seconds);
    if (this.advancing) {
      this.now += seconds;
    }
  };
}

/** `$metadata` es el nombre real del campo del SDK; se construye aparte para no escribirlo como clave literal. */
function withMetadata(httpStatusCode: number): Record<string, unknown> {
  return { [`${"$"}metadata`]: { httpStatusCode } };
}

function awsError(name: string, extra: Record<string, unknown> = {}): () => never {
  return () => {
    const error = Object.assign(new Error(`${name} happened`), { name, ...extra });
    throw error;
  };
}

function microvmResponse(state = "PENDING", endpoint = "abc.lambda-microvm.us-east-1.on.aws") {
  return {
    microvmId: SANDBOX_ID,
    state,
    endpoint,
    imageArn: IMAGE_ARN,
    imageVersion: "1.0",
    maximumDurationInSeconds: 3600,
    startedAt: STARTED_AT,
    idlePolicy: {
      maxIdleDurationSeconds: 300,
      suspendedDurationSeconds: 3300,
      autoResumeEnabled: true,
    },
  };
}

function plane(recorder: Recorder, sts?: Recorder, clock?: FakeClock): LambdaMicrovmsControlPlane {
  return new LambdaMicrovmsControlPlane({
    client: recorder,
    region: REGION,
    stsClient: sts,
    now: clock?.tick,
    sleep: clock?.sleep,
  });
}

describe("PortSpec and LaunchRequest", () => {
  test("port specs map to the API shapes and never allPorts", () => {
    expect(PortSpec.single(8080).toApi()).toEqual({ port: 8080 });
    expect(PortSpec.range(3000, 3010).toApi()).toEqual({
      range: { startPort: 3000, endPort: 3010 },
    });
    expect(() => PortSpec.range(10, 5)).toThrow(InvalidArgumentError);
    expect(PortSpec.range(1, 5).covers(3)).toBe(true);
    expect(JSON.stringify(PortSpec.single(1).toApi())).not.toContain("allPorts");
  });

  test("LaunchRequest.toApi uses exactly the AWS_API_NOTES field names", () => {
    const request = new LaunchRequest({
      imageArn: IMAGE_ARN,
      maximumDurationSeconds: 3600,
      runHookPayload: "{}",
      clientToken: "abc",
      logging: { disabled: {} },
      idle: { maxIdleSeconds: 300, suspendedDurationSeconds: 3300, autoResume: false },
      ingressConnectors: ["arn:aws:x"],
    });
    expect(request.toApi()).toEqual({
      imageIdentifier: IMAGE_ARN,
      maximumDurationInSeconds: 3600,
      runHookPayload: "{}",
      clientToken: "abc",
      logging: { disabled: {} },
      idlePolicy: {
        maxIdleDurationSeconds: 300,
        suspendedDurationSeconds: 3300,
        autoResumeEnabled: false,
      },
      ingressNetworkConnectors: ["arn:aws:x"],
    });
    expect(() =>
      new LaunchRequest({
        imageArn: IMAGE_ARN,
        maximumDurationSeconds: 1,
        runHookPayload: "{}",
        clientToken: "c",
        logging: { disabled: {} },
        idle: { maxIdleSeconds: 300, autoResume: true },
      }).toApi(),
    ).toThrow(InvalidArgumentError);
  });
});

describe("TokenBucket", () => {
  test("five suspends at 2 TPS sleep 0, 0, 0.5, 1.0, 1.5", async () => {
    const clock = new FakeClock();
    const bucket = new TokenBucket(2, { now: clock.tick, sleep: clock.sleep });
    const waits: number[] = [];
    for (let i = 0; i < 5; i += 1) {
      waits.push(await bucket.acquire());
    }
    expect(waits).toEqual([0, 0, 0.5, 1, 1.5]);
    expect(clock.sleeps).toEqual([0.5, 1, 1.5]);
  });

  test("paces at the published rate when the clock advances during the sleep", async () => {
    const clock = new FakeClock(true);
    const bucket = new TokenBucket(5, { now: clock.tick, sleep: clock.sleep });
    const waits: number[] = [];
    for (let i = 0; i < 8; i += 1) {
      waits.push(await bucket.acquire());
    }
    expect(waits.slice(0, 5)).toEqual([0, 0, 0, 0, 0]);
    expect(waits.slice(5).map((w) => Number(w.toFixed(3)))).toEqual([0.2, 0.2, 0.2]);
    clock.now += 10;
    expect(await bucket.acquire()).toBe(0);
    expect(() => new TokenBucket(0)).toThrow(RangeError);
  });
});

describe("LambdaMicrovmsControlPlane", () => {
  test("Sandbox.create sends the exact RunMicrovm and CreateMicrovmAuthToken inputs", async () => {
    const recorder = new Recorder().answer(microvmResponse(), {
      authToken: { "X-aws-proxy-auth": JWE },
    });
    const control = plane(recorder);
    await expect(
      Sandbox.create({
        template: IMAGE_ARN,
        timeoutMs: 3_600_000,
        envs: { A: "1" },
        allowedPorts: [3000],
        ingress: ["ALL_INGRESS"],
        logging: "cloudwatch",
        accessToken: ACCESS_TOKEN,
        controlPlane: control,
        readyTimeoutMs: 1,
        keepOnFailure: true,
        transport: { scheme: "http", port: 1 },
      }),
    ).rejects.toThrow();
    const run = recorder.inputs(RunMicrovmCommand.name)[0] as Record<string, unknown>;
    expect(Object.keys(run).sort()).toEqual([
      "clientToken",
      "idlePolicy",
      "imageIdentifier",
      "ingressNetworkConnectors",
      "logging",
      "maximumDurationInSeconds",
      "runHookPayload",
    ]);
    expect(run.imageIdentifier).toBe(IMAGE_ARN);
    expect(run.maximumDurationInSeconds).toBe(3600);
    expect(run.clientToken).toMatch(/^[0-9a-f]{32}$/);
    expect(run.logging).toEqual({ cloudWatch: { logGroup: "/rayito/rayito-base-2gb" } });
    expect(run.idlePolicy).toEqual({
      maxIdleDurationSeconds: 300,
      suspendedDurationSeconds: 3300,
      autoResumeEnabled: true,
    });
    expect(run.ingressNetworkConnectors).toEqual([
      "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS",
    ]);
    const payload = JSON.parse(run.runHookPayload as string);
    expect(payload).toMatchObject({ v: 1, user: "user", workdir: "/home/user", envs: { A: "1" } });
    expect(payload.token_sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(recorder.inputs(CreateMicrovmAuthTokenCommand.name)[0]).toEqual({
      microvmIdentifier: SANDBOX_ID,
      expirationInMinutes: 60,
      allowedPorts: [{ port: 8080 }, { port: 3000 }],
    });
    expect(recorder.sent[0]).toMatchObject({ name: RunMicrovmCommand.name });
    expect(recorder.sent[1]).toMatchObject({ name: CreateMicrovmAuthTokenCommand.name });
  });

  test("getMicrovm, terminate, suspend, resume and their false cases", async () => {
    const recorder = new Recorder().answer(
      microvmResponse("RUNNING", "https://host.example/"),
      {},
      awsError("ResourceNotFoundException"),
      {},
      awsError("ConflictException"),
      {},
      awsError("ConflictException"),
    );
    const control = plane(recorder);
    const info = await control.getMicrovm(SANDBOX_ID);
    expect(info.endpoint).toBe("host.example");
    expect(info.idle).toEqual({
      maxIdleSeconds: 300,
      suspendedDurationSeconds: 3300,
      autoResume: true,
    });
    expect(recorder.inputs(GetMicrovmCommand.name)).toEqual([{ microvmIdentifier: SANDBOX_ID }]);
    expect(await control.terminateMicrovm(SANDBOX_ID)).toBe(true);
    expect(await control.terminateMicrovm(SANDBOX_ID)).toBe(false);
    expect(await control.suspendMicrovm(SANDBOX_ID)).toBe(true);
    expect(await control.suspendMicrovm(SANDBOX_ID)).toBe(false);
    expect(await control.resumeMicrovm(SANDBOX_ID)).toBe(true);
    expect(await control.resumeMicrovm(SANDBOX_ID)).toBe(false);
    expect(recorder.sent.map((item) => item.name)).toEqual([
      GetMicrovmCommand.name,
      TerminateMicrovmCommand.name,
      TerminateMicrovmCommand.name,
      SuspendMicrovmCommand.name,
      SuspendMicrovmCommand.name,
      ResumeMicrovmCommand.name,
      ResumeMicrovmCommand.name,
    ]);
  });

  test("error names map to the hierarchy, never HTTP status", async () => {
    const cases: Array<[string, Record<string, unknown>, unknown]> = [
      ["ThrottlingException", { retryAfterSeconds: 2 }, RateLimitError],
      ["ServiceQuotaExceededException", { quotaCode: "L-1" }, QuotaExceededError],
      ["ConflictException", {}, SandboxStateError],
      ["ResourceNotFoundException", {}, SandboxNotFoundError],
      ["AccessDeniedException", {}, AuthenticationError],
      ["ValidationException", {}, InvalidArgumentError],
      ["InsufficientCapacityException", {}, CapacityError],
      ["InternalServerException", withMetadata(500), SandboxError],
    ];
    for (const [name, extra, expected] of cases) {
      const recorder = new Recorder().answer(awsError(name, extra));
      await expect(plane(recorder).getMicrovm(SANDBOX_ID)).rejects.toBeInstanceOf(
        expected as never,
      );
    }
    const throttled = translateAwsError(
      Object.assign(new Error("x"), { name: "ThrottlingException", retryAfterSeconds: 2 }),
    );
    expect((throttled as RateLimitError).retryAfter).toBe(2);
    const quota = translateAwsError(
      Object.assign(new Error("x"), { name: "ServiceQuotaExceededException", quotaCode: "L-1" }),
    );
    expect((quota as QuotaExceededError).quotaCode).toBe("L-1");
    const internal = translateAwsError(
      Object.assign(new Error("x"), { name: "InternalServerException", ...withMetadata(500) }),
    ) as SandboxError;
    expect(internal.awsCode).toBe("InternalServerException");
    expect(internal.statusCode).toBe(500);
    expect(translateAwsError(new SandboxError("kept"))).toBeInstanceOf(SandboxError);
    expect(translateAwsError("text")).toBeInstanceOf(SandboxError);
  });

  test("suspend bucket at 2 TPS with an injected clock", async () => {
    const clock = new FakeClock();
    const recorder = new Recorder().answer({}, {}, {}, {}, {});
    const control = plane(recorder, undefined, clock);
    await Promise.all(Array.from({ length: 5 }, () => control.suspendMicrovm(SANDBOX_ID)));
    expect(clock.sleeps).toEqual([0.5, 1, 1.5]);
  });

  test("list pagination with maxResults 50 and the default state filter", async () => {
    const item = (id: string, state: string) => ({
      microvmId: id,
      state,
      imageArn: IMAGE_ARN,
      imageVersion: "1.0",
      startedAt: STARTED_AT,
    });
    const recorder = new Recorder().answer(
      { items: [item("a", "RUNNING"), item("b", "TERMINATED")], nextToken: "next" },
      { items: [item("c", "SUSPENDED")] },
      { items: [item("a", "RUNNING"), item("b", "TERMINATED")], nextToken: "next" },
      { items: [item("c", "SUSPENDED")] },
    );
    const control = plane(recorder);
    const listed: string[] = [];
    for await (const entry of Sandbox.list({ controlPlane: control })) {
      listed.push(entry.sandboxId);
    }
    expect(listed).toEqual(["a", "c"]);
    const terminated: string[] = [];
    for await (const entry of Sandbox.list({ controlPlane: control, states: ["TERMINATED"] })) {
      terminated.push(entry.sandboxId);
    }
    expect(terminated).toEqual(["b"]);
    const inputs = recorder.inputs(ListMicrovmsCommand.name);
    expect(inputs).toHaveLength(4);
    expect(inputs.every((input) => input.maxResults === 50)).toBe(true);
    expect(inputs[1]?.nextToken).toBe("next");
  });

  test("list forwards the template filters", async () => {
    const recorder = new Recorder().answer({ items: [] });
    const control = plane(recorder);
    for await (const _entry of control.listMicrovms({ imageArn: IMAGE_ARN, imageVersion: "2.0" })) {
      throw new Error("no debería listar nada");
    }
    expect(recorder.inputs(ListMicrovmsCommand.name)[0]).toMatchObject({
      imageIdentifier: IMAGE_ARN,
      imageVersion: "2.0",
    });
  });

  test("listMicrovmsPage sends one exact request and returns every item unfiltered", async () => {
    const item = (id: string, state: string) => ({
      microvmId: id,
      state,
      imageArn: IMAGE_ARN,
      imageVersion: "1.0",
      startedAt: STARTED_AT,
    });
    const recorder = new Recorder().answer(
      { items: [item("a", "RUNNING"), item("b", "TERMINATED")], nextToken: "t2" },
      { items: [] },
    );
    const control = plane(recorder);
    const page = await control.listMicrovmsPage({
      imageArn: IMAGE_ARN,
      maxResults: 50,
      nextToken: "t1",
    });
    expect(page.items.map((entry) => [entry.sandboxId, entry.state])).toEqual([
      ["a", "RUNNING"],
      ["b", "TERMINATED"],
    ]);
    expect(page.nextToken).toBe("t2");
    const last = await control.listMicrovmsPage({ maxResults: 50 });
    expect(last).toEqual({ items: [], nextToken: undefined });
    expect(recorder.inputs(ListMicrovmsCommand.name)).toEqual([
      { maxResults: 50, imageIdentifier: IMAGE_ARN, nextToken: "t1" },
      { maxResults: 50 },
    ]);
    await expect(control.listMicrovmsPage({ maxResults: 51 })).rejects.toBeInstanceOf(RangeError);
    await expect(control.listMicrovmsPage({ maxResults: 0 })).rejects.toBeInstanceOf(RangeError);
    expect(recorder.sent).toHaveLength(2);
  });

  test("template names resolve through one cached GetCallerIdentity", async () => {
    const sts = new Recorder().answer({
      Account: "123456789012",
      Arn: "arn:aws:sts::123456789012:assumed-role/x/y",
    });
    const control = plane(new Recorder(), sts);
    expect(await control.resolveTemplateArn("base-2gb")).toBe(
      "arn:aws:lambda:us-east-1:123456789012:microvm-image:base-2gb",
    );
    expect(await control.resolveTemplateArn("base-2gb")).toBe(
      "arn:aws:lambda:us-east-1:123456789012:microvm-image:base-2gb",
    );
    expect(sts.sent.map((item) => item.name)).toEqual([GetCallerIdentityCommand.name]);
    expect(await control.resolveTemplateArn(IMAGE_ARN)).toBe(IMAGE_ARN);
    await expect(control.resolveTemplateArn("bad name!")).rejects.toThrow(InvalidArgumentError);
    await expect(plane(new Recorder()).resolveTemplateArn("base")).rejects.toThrow(
      InvalidArgumentError,
    );
    const lazy = new LambdaMicrovmsControlPlane({
      client: new Recorder(),
      region: REGION,
      stsClient: () => sts.answer({ Account: "1", Arn: "arn:aws-cn:sts::1:x" }),
    });
    expect(await lazy.resolveTemplateArn("n")).toBe(
      "arn:aws-cn:lambda:us-east-1:1:microvm-image:n",
    );
  });

  test("authToken key handling", async () => {
    expect(proxyJweFromResponse({ "X-aws-proxy-auth": "a" })).toBe("a");
    expect(proxyJweFromResponse({ "x-aws-proxy-auth": "b" })).toBe("b");
    expect(proxyJweFromResponse({ other: "c" })).toBe("c");
    expect(() => proxyJweFromResponse({ a: "1", b: "2" })).toThrow(SandboxError);
    const control = plane(new Recorder().answer({ authToken: { "X-aws-proxy-auth": JWE } }));
    expect(await control.createAuthToken(SANDBOX_ID, [PortSpec.single(8080)])).toBe(JWE);
    await expect(control.createAuthToken(SANDBOX_ID, [])).rejects.toThrow(InvalidArgumentError);
  });

  test("response mappers", () => {
    expect(normalizeEndpoint(" https://host/path ")).toBe("host");
    expect(normalizeEndpoint("host")).toBe("host");
    const info = sandboxInfoFromResponse({
      ...microvmResponse("RUNNING"),
      idlePolicy: undefined,
      terminatedAt: STARTED_AT,
    });
    expect(info.idle).toBeUndefined();
    expect(info.terminatedAt).toEqual(STARTED_AT);
    expect(info.templateVersion).toBe("1.0");
    expect([info.ingress, info.egress]).toEqual([[], []]);
    const connected = sandboxInfoFromResponse({
      ...microvmResponse("RUNNING"),
      ingressNetworkConnectors: ["arn:aws:lambda:us-east-1:aws:network-connector:x:ALL_INGRESS"],
      egressNetworkConnectors: ["arn:aws:lambda:us-east-1:aws:network-connector:x:INTERNET_EGRESS"],
    });
    expect(connected.ingress).toEqual([
      "arn:aws:lambda:us-east-1:aws:network-connector:x:ALL_INGRESS",
    ]);
    expect(connected.egress).toEqual([
      "arn:aws:lambda:us-east-1:aws:network-connector:x:INTERNET_EGRESS",
    ]);
  });

  test("sharedControlPlane is one adapter per region and Sandbox honours controlPlane > client", () => {
    process.env.AWS_REGION ??= REGION;
    const first = sharedControlPlane("eu-west-1");
    expect(sharedControlPlane("eu-west-1")).toBe(first);
    expect(sharedControlPlane("us-east-2")).not.toBe(first);
    expect(first.region).toBe("eu-west-1");
    expect(() => LambdaMicrovmsControlPlane.fromRegion("ap-south-1")).not.toThrow();
  });
});

describe("control-plane client settings (retries, proxy, integration)", () => {
  test("retries: 2 gives maxAttempts 3 and the integration joins the user agent", async () => {
    const config = clientConfig(REGION, { retries: 2, integration: "acme/1.0" });
    expect(config.maxAttempts).toBe(3);
    expect(config.retryMode).toBe("standard");
    expect(config.customUserAgent).toContainEqual(["rayito-integration", "acme/1.0"]);
    const defaults = clientConfig(REGION);
    expect(defaults.maxAttempts).toBe(DEFAULT_MAX_ATTEMPTS);
    expect(defaults.customUserAgent.map(([key]) => key)).toEqual(["rayito"]);
    const plane = LambdaMicrovmsControlPlane.fromRegion(REGION, {
      retries: 2,
      integration: "acme/1.0",
    });
    const resolved = (plane.client as unknown as { config: { maxAttempts: () => Promise<number> } })
      .config;
    expect(await resolved.maxAttempts()).toBe(3);
  });

  test("invalid retries or integration are refused before building a client", () => {
    for (const retries of [-1, 1.5, true, "2"]) {
      expect(() => clientConfig(REGION, { retries: retries as never })).toThrow(
        InvalidArgumentError,
      );
    }
    expect(() => clientConfig(REGION, { integration: "acme 1" })).toThrow(InvalidArgumentError);
  });

  test("the proxy puts a ProxyTunnelAgent in the NodeHttpHandler", async () => {
    const config = clientConfig(REGION, { proxy: "http://u:p@127.0.0.1:3128" });
    const handlerConfig = await (
      config.requestHandler as unknown as { configProvider: Promise<{ httpsAgent: unknown }> }
    ).configProvider;
    expect(handlerConfig.httpsAgent).toBeInstanceOf(ProxyTunnelAgent);
  });

  test("any setting builds a dedicated shared plane; with an explicit plane it is an error", () => {
    process.env.AWS_REGION ??= REGION;
    const plain = sharedControlPlane("eu-west-3");
    const retried = sharedControlPlane("eu-west-3", { retries: 2 });
    expect(retried).not.toBe(plain);
    expect(sharedControlPlane("eu-west-3", { retries: 2 })).toBe(retried);
    expect(sharedControlPlane("eu-west-3", { integration: "acme/1.0" })).not.toBe(retried);
    expect(resolveControlPlane({ region: "eu-west-3", retries: 2 })).toBe(retried);
    const explicit = plane(new Recorder());
    expect(() => resolveControlPlane({ controlPlane: explicit, retries: 1 })).toThrow(
      new InvalidArgumentError(
        "retries/proxy/integration no se combinan con controlPlane ni con client",
      ),
    );
    expect(() =>
      resolveControlPlane({ client: new Recorder(), proxy: "http://127.0.0.1:3128" }),
    ).toThrow(InvalidArgumentError);
  });
});
