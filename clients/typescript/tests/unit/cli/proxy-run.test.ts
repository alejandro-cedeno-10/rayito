/**
 * El caso de uso de `rayito sandbox proxy`: valida y reserva el socket local
 * ANTES de tocar AWS, rechaza un sandbox terminado, acuña un único JWE de un
 * solo puerto y sirve hasta que se cancele. Espejo de
 * `test_run_proxy_*` y `test_build_refresher_*` de
 * `clients/python/tests/unit/cli/test_proxy.py`.
 */

import { connect, createServer } from "node:net";
import { describe, expect, it } from "vitest";
import { ProxyListener } from "../../../src/cli/proxy-listener.js";
import { runProxy } from "../../../src/cli/proxy-run.js";
import { InvalidArgumentError, SandboxStateError } from "../../../src/errors.js";
import {
  DEFAULT_UPSTREAM_RESPONSE,
  FakeUpstream,
  freePort,
  MintingPlane,
  SANDBOX_ID,
} from "./proxy-harness.js";

const GUEST_PORT = 8080;

async function options(localPort: number, overrides: Partial<Parameters<typeof runProxy>[1]> = {}) {
  return {
    sandboxId: SANDBOX_ID,
    port: GUEST_PORT,
    localPort,
    bind: "127.0.0.1",
    allowedHosts: [],
    allowedOrigins: [],
    maxConnections: 1,
    onReady: () => undefined,
    report: () => undefined,
    signal: new AbortController().signal,
    ...overrides,
  };
}

describe("runProxy antes de tocar AWS", () => {
  it("rechaza el puerto de los hooks sin consultar el sandbox", async () => {
    const plane = new MintingPlane("RUNNING");
    await expect(runProxy(plane, await options(await freePort(), { port: 9000 }))).rejects.toThrow(
      InvalidArgumentError,
    );
    expect(plane.lookups).toBe(0);
  });

  it.each([0, 65_536])("rechaza el --local-port %s sin consultar el sandbox", async (localPort) => {
    const plane = new MintingPlane("RUNNING");
    await expect(runProxy(plane, await options(localPort))).rejects.toThrow(InvalidArgumentError);
    expect(plane.lookups).toBe(0);
  });

  it("un --local-port ocupado falla limpio sin consultar el sandbox", async () => {
    const busy = createServer();
    await new Promise<void>((resolve) => busy.listen(0, "127.0.0.1", resolve));
    const { port } = busy.address() as { port: number };
    const plane = new MintingPlane("RUNNING");
    try {
      await expect(runProxy(plane, await options(port))).rejects.toThrow(InvalidArgumentError);
      expect(plane.lookups).toBe(0);
    } finally {
      busy.close();
    }
  });

  it("un --allowed-host mal formado falla antes de consultar el sandbox y libera el puerto", async () => {
    const plane = new MintingPlane("RUNNING");
    const port = await freePort();
    await expect(
      runProxy(plane, await options(port, { allowedHosts: ["http://devbox"] })),
    ).rejects.toThrow(InvalidArgumentError);
    expect(plane.lookups).toBe(0);
    const again = await ProxyListener.bind("127.0.0.1", port);
    await again.close();
  });

  it("un sandbox terminado es SandboxStateError y libera el puerto local", async () => {
    const plane = new MintingPlane("TERMINATED");
    const port = await freePort();
    await expect(runProxy(plane, await options(port))).rejects.toThrow(SandboxStateError);
    expect(plane.minted).toHaveLength(0);
    const again = await ProxyListener.bind("127.0.0.1", port);
    await again.close();
  });
});

describe("runProxy sirviendo", () => {
  it("acuña un solo JWE de un solo puerto, lo reutiliza y para al cancelar", async () => {
    const upstream = new FakeUpstream();
    await upstream.start();
    const plane = new MintingPlane("RUNNING");
    const port = await freePort();
    const abort = new AbortController();
    const announced: string[] = [];
    const served = runProxy(
      plane,
      await options(port, {
        signal: abort.signal,
        onReady: (line) => announced.push(line),
        connectorFactory: () => upstream.connector(),
      }),
    );
    await waitUntil(() => announced.length === 1);
    expect(announced[0]).toContain(`http://127.0.0.1:${port} → ${SANDBOX_ID}:${GUEST_PORT}`);
    expect(announced[0]).toContain(`http://${SANDBOX_ID}.localhost:${port}`);

    for (let request = 0; request < 2; request += 1) {
      const response = await requestOnce(port);
      expect(response).toBe(DEFAULT_UPSTREAM_RESPONSE);
    }
    expect(upstream.received.toString("latin1")).toContain("X-aws-proxy-auth: jwe-1");
    expect(plane.minted).toHaveLength(1);
    expect(plane.minted[0]?.map((spec) => [spec.start, spec.end])).toEqual([
      [GUEST_PORT, GUEST_PORT],
    ]);

    abort.abort();
    await served;
    await upstream.close();
    const again = await ProxyListener.bind("127.0.0.1", port);
    await again.close();
  });
});

async function requestOnce(port: number): Promise<string> {
  const socket = connect({ host: "127.0.0.1", port, allowHalfOpen: true });
  socket.on("error", () => undefined);
  const chunks: Buffer[] = [];
  const closed = new Promise<void>((resolve) => socket.on("close", () => resolve()));
  socket.on("data", (chunk) => chunks.push(chunk));
  socket.write(`GET / HTTP/1.1\r\nHost: localhost:${port}\r\n\r\n`);
  socket.end();
  await closed;
  return Buffer.concat(chunks).toString("latin1");
}

async function waitUntil(condition: () => boolean): Promise<void> {
  const deadline = Date.now() + 3000;
  while (!condition()) {
    if (Date.now() > deadline) {
      throw new Error("la condición no se cumplió a tiempo");
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}
