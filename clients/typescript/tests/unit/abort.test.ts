/**
 * `signal` (D18): un `AbortSignal` cancela unarios, streams y el arranque.
 * Abortar rechaza con `signal.reason` sin envolverlo; un `Execute` cancelado
 * interrumpe la celda; en `create()` llega a cada `send` del plano de
 * control (`abortSignal`) y al sondeo de readiness, y el MicroVM lanzado se
 * termina salvo `keepOnFailure`.
 */

import { describe, expect, test } from "vitest";
import { abortReasonOr, raceAbort } from "../../src/abort.js";
import {
  type CommandSender,
  type ControlPlane,
  type ControlPlaneCallOptions,
  LambdaMicrovmsControlPlane,
} from "../../src/aws/control-plane.js";
import { Sandbox } from "../../src/index.js";
import { FakeControlPlane, IMAGE_ARN, REGION, SANDBOX_ID } from "./fake/control-plane.js";
import { managedLifecycle } from "./fake/lifecycle.js";
import {
  ACCESS_TOKEN,
  createTestSandbox,
  sleep,
  startRayd,
  waitUntil,
  withTimeout,
} from "./helpers.js";

const BUDGET_MS = 3000;

class Stop extends Error {}

function abortLater(ms: number, reason: unknown = new Stop("parado")): AbortSignal {
  const controller = new AbortController();
  setTimeout(() => controller.abort(reason), ms);
  return controller.signal;
}

/** Espera al `signal` que le pasen y rechaza con su `reason`; sin `signal` se queda colgado. */
function untilAborted(options: ControlPlaneCallOptions | undefined): Promise<never> {
  return new Promise<never>((_, reject) => {
    const signal = options?.signal;
    if (signal === undefined) {
      return;
    }
    if (signal.aborted) {
      reject(signal.reason);
      return;
    }
    signal.addEventListener("abort", () => reject(signal.reason), { once: true });
  });
}

/** El plano falso con un método sustituido; el resto delega con `this` en el original. */
function overriding(fake: FakeControlPlane, overrides: Partial<ControlPlane>): ControlPlane {
  return new Proxy(fake, {
    get(target, property, receiver) {
      if (Object.hasOwn(overrides, property)) {
        return overrides[property as keyof ControlPlane];
      }
      const value = Reflect.get(target, property, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

describe("signal on sandbox calls", () => {
  test("aborting a long runCode rejects with the reason and the agent interrupts the cell", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const reason = new Stop("basta");
    const controller = new AbortController();
    const pending = sandbox.runCode("print-then-sleep", { signal: controller.signal });
    await waitUntil(() => rayd.code.executeRequests.length === 1);
    controller.abort(reason);
    await expect(withTimeout(pending, BUDGET_MS)).rejects.toBe(reason);
    await waitUntil(() => rayd.code.executions[0]?.interrupted === true);
  });

  test("an already-aborted signal rejects before any request", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const controller = new AbortController();
    const reason = new Stop("antes");
    controller.abort(reason);
    const listed = rayd.process.listHeaders.length;
    await expect(sandbox.runCode("x = 1", { signal: controller.signal })).rejects.toBe(reason);
    await expect(sandbox.commands.list({ signal: controller.signal })).rejects.toBe(reason);
    await expect(sandbox.commands.run("echo hi", { signal: controller.signal })).rejects.toBe(
      reason,
    );
    expect(rayd.code.executeRequests).toHaveLength(0);
    expect(rayd.process.listHeaders).toHaveLength(listed);
    expect(rayd.process.startRequests).toHaveLength(0);
  });

  test("the default reason is a DOMException AbortError, not a SandboxError", async () => {
    const { sandbox } = await createTestSandbox();
    const controller = new AbortController();
    const pending = sandbox.commands.run("sleep 30", { signal: controller.signal });
    await sleep(50);
    controller.abort();
    const error = await withTimeout(pending, BUDGET_MS).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(DOMException);
    expect((error as DOMException).name).toBe("AbortError");
  });
});

describe("signal on create and connect", () => {
  test("an abort during the readiness poll rejects with the reason well before readyTimeoutMs", async () => {
    const reason = new Stop("readiness");
    const started = performance.now();
    const error = await withTimeout(
      createTestSandbox({
        beforeCreate: (rayd) => {
          rayd.health.notReadyCalls = 1_000_000;
        },
        create: { signal: abortLater(300, reason), readyTimeoutMs: 60_000 },
      }),
      BUDGET_MS,
    ).catch((caught: unknown) => caught);
    expect(error).toBe(reason);
    expect(performance.now() - started).toBeLessThan(BUDGET_MS);
  });

  test("with keepOnFailure the aborted VM is kept", async () => {
    const rayd = await startRayd();
    const plane = new FakeControlPlane({ endpoint: rayd.host });
    rayd.health.notReadyCalls = 1_000_000;
    const reason = new Stop("keep");
    try {
      await expect(
        Sandbox.create({
          template: IMAGE_ARN,
          idle: null,
          accessToken: ACCESS_TOKEN,
          controlPlane: plane,
          transport: rayd.transport,
          readyTimeoutMs: 60_000,
          keepOnFailure: true,
          signal: abortLater(300, reason),
        }),
      ).rejects.toBe(reason);
      expect(plane.callsTo("terminateMicrovm")).toEqual([]);
      const terminating = new FakeControlPlane({ endpoint: rayd.host });
      await expect(
        Sandbox.create({
          template: IMAGE_ARN,
          idle: null,
          accessToken: ACCESS_TOKEN,
          controlPlane: terminating,
          transport: rayd.transport,
          readyTimeoutMs: 60_000,
          signal: abortLater(300, reason),
        }),
      ).rejects.toBe(reason);
      expect(terminating.callsTo("terminateMicrovm")).toEqual([
        { operation: "terminateMicrovm", sandboxId: SANDBOX_ID },
      ]);
    } finally {
      await rayd.close();
    }
  });

  test("the control plane receives the signal: a hung resolveTemplateArn is cut and nothing launches", async () => {
    const rayd = await startRayd();
    const fake = new FakeControlPlane({ endpoint: rayd.host });
    const hung = overriding(fake, {
      resolveTemplateArn: (_template: string, options?: ControlPlaneCallOptions) =>
        untilAborted(options),
    });
    const reason = new Stop("plano");
    try {
      await expect(
        withTimeout(
          Sandbox.create({
            template: IMAGE_ARN,
            accessToken: ACCESS_TOKEN,
            controlPlane: hung,
            transport: rayd.transport,
            signal: abortLater(100, reason),
          }),
          BUDGET_MS,
        ),
      ).rejects.toBe(reason);
      expect(fake.launches).toEqual([]);
    } finally {
      await rayd.close();
    }
  });

  test("connect passes the signal to get-microvm", async () => {
    const rayd = await startRayd();
    const fake = new FakeControlPlane({ endpoint: rayd.host });
    const hung = overriding(fake, {
      getMicrovm: (_id: string, options?: ControlPlaneCallOptions) => untilAborted(options),
    });
    const reason = new Stop("connect");
    try {
      await expect(
        withTimeout(
          Sandbox.connect(SANDBOX_ID, {
            accessToken: ACCESS_TOKEN,
            controlPlane: hung,
            transport: rayd.transport,
            signal: abortLater(100, reason),
          }),
          BUDGET_MS,
        ),
      ).rejects.toBe(reason);
    } finally {
      await rayd.close();
    }
  });

  test("the AWS adapter hands abortSignal to send and rejects with the reason", async () => {
    const seen: Array<{ abortSignal?: AbortSignal } | undefined> = [];
    const client: CommandSender = {
      send: (_command: unknown, options?: { abortSignal?: AbortSignal }) => {
        seen.push(options);
        return untilAborted({ signal: options?.abortSignal });
      },
    };
    const plane = new LambdaMicrovmsControlPlane({ client, region: REGION });
    const reason = new Stop("aws");
    const signal = abortLater(50, reason);
    await expect(withTimeout(plane.getMicrovm(SANDBOX_ID, { signal }), BUDGET_MS)).rejects.toBe(
      reason,
    );
    expect(seen[0]?.abortSignal).toBe(signal);
    const aborted = new AbortController();
    aborted.abort(reason);
    await expect(plane.resumeMicrovm(SANDBOX_ID, { signal: aborted.signal })).rejects.toBe(reason);
    expect(seen).toHaveLength(1);
  });
});

describe("signal on lifecycle, metrics and network calls", () => {
  const heldCalls: ReadonlyArray<
    readonly [string, string, (sandbox: Sandbox, signal: AbortSignal) => Promise<unknown>]
  > = [
    [
      "setTimeout",
      "/rayito.v1.LifecycleService/SetTimeout",
      (sandbox, signal) => sandbox.setTimeout(90_000, { signal }),
    ],
    [
      "connect({ timeoutMs })",
      "/rayito.v1.LifecycleService/SetTimeout",
      (sandbox, signal) => sandbox.connect({ timeoutMs: 90_000, signal }),
    ],
    [
      "getHealth",
      "/rayito.v1.HealthService/Health",
      (sandbox, signal) => sandbox.getHealth({ signal }),
    ],
    [
      "isRunning",
      "/rayito.v1.HealthService/Health",
      (sandbox, signal) => sandbox.isRunning({ signal }),
    ],
    [
      "getMetrics",
      "/rayito.v1.HealthService/Metrics",
      (sandbox, signal) => sandbox.getMetrics({ signal }),
    ],
    [
      "getMetricsHistory",
      "/rayito.v1.HealthService/MetricsHistory",
      (sandbox, signal) => sandbox.getMetricsHistory({ signal }),
    ],
    [
      "getNetwork",
      "/rayito.v1.NetworkService/GetNetwork",
      (sandbox, signal) => sandbox.getNetwork({ signal }),
    ],
    [
      "updateNetwork",
      "/rayito.v1.NetworkService/UpdateNetwork",
      (sandbox, signal) => sandbox.updateNetwork({ denyOut: ["10.0.0.0/8"] }, { signal }),
    ],
  ];

  test.each(heldCalls)(
    "%s passes the signal to the call and rejects with its reason",
    async (_name, path, call) => {
      const { sandbox, rayd, plane } = await createTestSandbox({
        beforeCreate: (fake) => {
          fake.health.lifecycle = managedLifecycle();
        },
      });
      plane.setStates(["RUNNING"]);
      rayd.held.add(path);
      const reason = new Stop("colgada");
      await expect(withTimeout(call(sandbox, abortLater(100, reason)), BUDGET_MS)).rejects.toBe(
        reason,
      );
    },
  );

  test.each(heldCalls)(
    "%s with an already-aborted signal rejects before any request",
    async (_name, _path, call) => {
      const { sandbox, rayd } = await createTestSandbox({
        beforeCreate: (fake) => {
          fake.health.lifecycle = managedLifecycle();
        },
      });
      const controller = new AbortController();
      const reason = new Stop("antes");
      controller.abort(reason);
      const requests = rayd.requests;
      await expect(call(sandbox, controller.signal)).rejects.toBe(reason);
      expect(rayd.requests).toBe(requests);
    },
  );

  test("the static setTimeout rejects an aborted signal before any AWS call or mint", async () => {
    const rayd = await startRayd();
    try {
      rayd.health.lifecycle = managedLifecycle();
      const plane = new FakeControlPlane({ endpoint: rayd.host, states: ["RUNNING"] });
      const controller = new AbortController();
      const reason = new Stop("estático");
      controller.abort(reason);
      await expect(
        Sandbox.setTimeout(SANDBOX_ID, 90_000, {
          accessToken: ACCESS_TOKEN,
          controlPlane: plane,
          transport: rayd.transport,
          signal: controller.signal,
        }),
      ).rejects.toBe(reason);
      expect(plane.calls).toHaveLength(0);
      rayd.held.add("/rayito.v1.LifecycleService/SetTimeout");
      const held = new Stop("en vuelo");
      await expect(
        withTimeout(
          Sandbox.setTimeout(SANDBOX_ID, 90_000, {
            accessToken: ACCESS_TOKEN,
            controlPlane: plane,
            transport: rayd.transport,
            signal: abortLater(100, held),
          }),
          BUDGET_MS,
        ),
      ).rejects.toBe(held);
    } finally {
      await rayd.close();
    }
  });
});

describe("the abort policy helpers", () => {
  test("abortReasonOr: an aborted signal wins with its reason, unwrapped", () => {
    const error = new Error("rpc");
    expect(abortReasonOr(undefined, error)).toBe(error);
    expect(abortReasonOr(new AbortController().signal, error)).toBe(error);
    const controller = new AbortController();
    const reason = new Stop("parado");
    controller.abort(reason);
    expect(abortReasonOr(controller.signal, error)).toBe(reason);
  });

  test("raceAbort settles with the promise, or rejects with the reason once aborted", async () => {
    await expect(raceAbort(Promise.resolve(1), undefined)).resolves.toBe(1);
    const live = new AbortController();
    await expect(raceAbort(Promise.resolve(2), live.signal)).resolves.toBe(2);
    const failure = new Error("fallo");
    await expect(raceAbort(Promise.reject(failure), live.signal)).rejects.toBe(failure);
    const reason = new Stop("ya abortado");
    const aborted = new AbortController();
    aborted.abort(reason);
    await expect(raceAbort(Promise.reject(new Error("ignorado")), aborted.signal)).rejects.toBe(
      reason,
    );
    const pending = new Promise<never>(() => undefined);
    await expect(raceAbort(pending, abortLater(10))).rejects.toBeInstanceOf(Stop);
  });
});
