/**
 * `rayito sandbox proxy` de extremo a extremo sobre sockets de loopback
 * frente a un upstream falso sin TLS: reescritura de cabeceras, upgrade,
 * `Host`/`Origin` (421/403), tope de conexiones (503), un solo mensaje por
 * conexión, delimitación ambigua (400), 502 sin JWE o sin upstream, y que
 * ningún informe lleve el JWE ni la ruta. Espejo de
 * `clients/python/tests/unit/cli/test_proxy.py`.
 */

import { afterEach, describe, expect, it } from "vitest";
import { parseHttpHead, rewriteHead } from "../../../src/cli/http-head.js";
import {
  BAD_GATEWAY_RESPONSE,
  BAD_REQUEST_RESPONSE,
  FORBIDDEN_RESPONSE,
  MISDIRECTED_REQUEST_RESPONSE,
  SERVICE_UNAVAILABLE_RESPONSE,
} from "../../../src/cli/proxy-server.js";
import {
  DEFAULT_UPSTREAM_RESPONSE,
  ENDPOINT,
  FakeUpstream,
  JWE,
  PROXY_PORT_VALUE,
  ProxyWorld,
  SANDBOX_ID,
  type UpstreamScript,
} from "./proxy-harness.js";

const SMUGGLED_REQUEST = "POST /terminate HTTP/1.1\r\nHost: x\r\nX-aws-proxy-port: 9000\r\n\r\n";
const PIPELINE_SETTLE_MS = 60;
const SHORT_TIMEOUT_SECONDS = 0.05;
const OVERSIZED_HEAD_BYTES = 70_000;

const cleanups: Array<() => Promise<void>> = [];

afterEach(async () => {
  for (const cleanup of cleanups.splice(0)) {
    await cleanup();
  }
});

async function startWorld(
  script: UpstreamScript = {},
  options: Parameters<typeof ProxyWorld.start>[0] = {},
): Promise<{ world: ProxyWorld; upstream: FakeUpstream }> {
  const upstream = new FakeUpstream(script);
  await upstream.start();
  const world = await ProxyWorld.start({ upstream, ...options });
  cleanups.push(async () => {
    await world.stop();
    await upstream.close();
  });
  return { world, upstream };
}

function rewritten(raw: string): string {
  return rewriteHead(parseHttpHead(raw), { endpoint: ENDPOINT, jwe: JWE, port: PROXY_PORT_VALUE });
}

function upgradeRequest(host: string): string {
  return `GET /ws HTTP/1.1\r\nHost: ${host}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n`;
}

describe("reescritura y reenvío", () => {
  it("reescribe las cabeceras y deja pasar un GET", async () => {
    const { world, upstream } = await startWorld();
    const response = await world.send(
      `GET /status HTTP/1.1\r\nHost: ${world.localHost}\r\nX-aws-proxy-auth: forged\r\n\r\n`,
    );
    expect(response.toString("latin1")).toBe(DEFAULT_UPSTREAM_RESPONSE);
    const head = upstream.received.toString("latin1");
    expect(head).not.toContain("forged");
    expect(head).toContain(`X-aws-proxy-auth: ${JWE}`);
    expect(head).toContain(`X-aws-proxy-port: ${PROXY_PORT_VALUE}`);
    expect(head).toContain(`Host: ${ENDPOINT}`);
    expect(head).toContain("Connection: close");
  });

  it("mantiene abierta una conexión de upgrade en los dos sentidos", async () => {
    const { world, upstream } = await startWorld({
      response:
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n",
      echo: true,
    });
    const socket = await world.open();
    const incoming: Buffer[] = [];
    socket.on("data", (chunk) => incoming.push(chunk));
    socket.write(upgradeRequest(world.localHost));
    await waitFor(() => Buffer.concat(incoming).includes("\r\n\r\n"));
    socket.write("ping-frame");
    await waitFor(() => Buffer.concat(incoming).toString("latin1").endsWith("ping-frame"));
    const head = upstream.received.toString("latin1");
    expect(head).toContain("Connection: Upgrade");
    expect(head).toContain("Upgrade: websocket");
    expect(head).not.toContain("Connection: close");
    socket.destroy();
  });

  it("no reenvía nada con una cabecera con un LF suelto: 400 y sin tocar el upstream", async () => {
    const { world, upstream } = await startWorld();
    const response = await world.send("GET / HTTP/1.1\r\nFoo: a\nX-aws-proxy-port: 9000\r\n\r\n");
    expect(response.toString("latin1")).toBe(BAD_REQUEST_RESPONSE);
    expect(upstream.connections).toBe(0);
  });
});

describe("Host y Origin", () => {
  it.each([["127.0.0.1"], ["localhost"], ["[::1]"], ["LocalHost"]])(
    "admite el nombre de loopback %s con el puerto local",
    async (name) => {
      const { world, upstream } = await startWorld();
      const response = await world.send(`GET / HTTP/1.1\r\nHost: ${name}:${world.port}\r\n\r\n`);
      expect(response.toString("latin1")).toBe(DEFAULT_UPSTREAM_RESPONSE);
      expect(upstream.connections).toBe(1);
    },
  );

  it("un Host ajeno es 421 y nunca llega al upstream (DNS rebinding)", async () => {
    const { world, upstream } = await startWorld();
    const response = await world.send(
      `GET / HTTP/1.1\r\nHost: attacker.example:${world.port}\r\n\r\n`,
    );
    expect(response.toString("latin1")).toBe(MISDIRECTED_REQUEST_RESPONSE);
    expect(upstream.connections).toBe(0);
  });

  it("un Host ausente es 421", async () => {
    const { world, upstream } = await startWorld();
    const response = await world.send("GET / HTTP/1.1\r\nAccept: */*\r\n\r\n");
    expect(response.toString("latin1")).toBe(MISDIRECTED_REQUEST_RESPONSE);
    expect(upstream.connections).toBe(0);
  });

  it("un Host de loopback con otro puerto es 421", async () => {
    const { world, upstream } = await startWorld();
    const response = await world.send("GET / HTTP/1.1\r\nHost: 127.0.0.1:9\r\n\r\n");
    expect(response.toString("latin1")).toBe(MISDIRECTED_REQUEST_RESPONSE);
    expect(upstream.connections).toBe(0);
  });

  it("un Origin ajeno en un POST simple es 403 (CSRF a ciegas)", async () => {
    const { world, upstream } = await startWorld();
    const response = await world.send(
      `POST /api/run HTTP/1.1\r\nHost: ${world.localHost}\r\nOrigin: http://attacker.example\r\nContent-Length: 0\r\n\r\n`,
    );
    expect(response.toString("latin1")).toBe(FORBIDDEN_RESPONSE);
    expect(upstream.connections).toBe(0);
  });

  it("un Origin ajeno en un WebSocket entre orígenes es 403", async () => {
    const { world, upstream } = await startWorld();
    const response = await world.send(
      `GET /ws HTTP/1.1\r\nHost: ${world.localHost}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nOrigin: http://attacker.example:${world.port}\r\n\r\n`,
    );
    expect(response.toString("latin1")).toBe(FORBIDDEN_RESPONSE);
    expect(upstream.connections).toBe(0);
  });

  it("Origin: null es 403", async () => {
    const { world, upstream } = await startWorld();
    const response = await world.send(
      `POST / HTTP/1.1\r\nHost: ${world.localHost}\r\nOrigin: null\r\n\r\n`,
    );
    expect(response.toString("latin1")).toBe(FORBIDDEN_RESPONSE);
    expect(upstream.connections).toBe(0);
  });

  it("una petición del mismo origen pasa", async () => {
    const { world } = await startWorld();
    const response = await world.send(
      `POST / HTTP/1.1\r\nHost: ${world.localHost}\r\nOrigin: http://${world.localHost}\r\n\r\n`,
    );
    expect(response.toString("latin1")).toBe(DEFAULT_UPSTREAM_RESPONSE);
  });

  it("--allow-origin admite un origen explícito", async () => {
    const { world, upstream } = await startWorld(
      {},
      { allowedOrigins: ["https://notebook.example.com/"] },
    );
    const response = await world.send(
      `GET / HTTP/1.1\r\nHost: ${world.localHost}\r\nOrigin: https://notebook.example.com\r\n\r\n`,
    );
    expect(response.toString("latin1")).toBe(DEFAULT_UPSTREAM_RESPONSE);
    expect(upstream.connections).toBe(1);
  });

  it("--allowed-host admite el nombre con y sin el puerto del listener", async () => {
    const { world } = await startWorld({}, { allowedHosts: ["devbox.example.com"] });
    for (const host of [`devbox.example.com:${world.port}`, "devbox.example.com"]) {
      const response = await world.send(`GET / HTTP/1.1\r\nHost: ${host}\r\n\r\n`);
      expect(response.toString("latin1"), host).toBe(DEFAULT_UPSTREAM_RESPONSE);
    }
  });

  it("el nombre <id>.localhost se admite con un bind de loopback", async () => {
    const { world } = await startWorld();
    const response = await world.send(
      `GET / HTTP/1.1\r\nHost: ${SANDBOX_ID}.localhost:${world.port}\r\n\r\n`,
    );
    expect(response.toString("latin1")).toBe(DEFAULT_UPSTREAM_RESPONSE);
  });
});

describe("tope de conexiones", () => {
  it("con todas las conexiones ocupadas, la siguiente es 503 sin abrir el upstream", async () => {
    const { world, upstream } = await startWorld({
      response: DEFAULT_UPSTREAM_RESPONSE,
      closeAfterMs: 400,
    });
    const first = await world.open();
    first.write(`GET / HTTP/1.1\r\nHost: ${world.localHost}\r\n\r\n`);
    await waitFor(() => upstream.connections === 1);
    const response = await world.send(`GET / HTTP/1.1\r\nHost: ${world.localHost}\r\n\r\n`);
    expect(response.toString("latin1")).toBe(SERVICE_UNAVAILABLE_RESPONSE);
    expect(upstream.connections).toBe(1);
    first.destroy();
  });

  it("libera la conexión al terminar", async () => {
    const { world, upstream } = await startWorld();
    await world.send(`GET / HTTP/1.1\r\nHost: ${world.localHost}\r\n\r\n`);
    const second = await world.send(`GET / HTTP/1.1\r\nHost: ${world.localHost}\r\n\r\n`);
    expect(second.toString("latin1")).toBe(DEFAULT_UPSTREAM_RESPONSE);
    expect(upstream.connections).toBe(2);
  });
});

describe("un solo mensaje por conexión", () => {
  it("un upgrade rechazado con 200 nunca abre un túnel para una segunda petición", async () => {
    const { world, upstream } = await startWorld({
      response: "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nno",
      closeAfterMs: PIPELINE_SETTLE_MS,
    });
    const upgrade = upgradeRequest(world.localHost);
    const response = await world.send(upgrade + SMUGGLED_REQUEST);
    expect(upstream.received.toString("latin1")).toBe(rewritten(upgrade));
    expect(upstream.received.toString("latin1")).not.toContain("9000");
    expect(response.toString("latin1")).toBe(
      "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nno",
    );
  });

  it("un upgrade rechazado con un cuerpo chunked reenvía sólo ese cuerpo", async () => {
    const body = "3\r\nnop\r\n0\r\n\r\n";
    const { world, upstream } = await startWorld({
      response: `HTTP/1.1 426 Upgrade Required\r\nTransfer-Encoding: chunked\r\n\r\n${body}HTTP/1.1 200 OK\r\n\r\n`,
      closeAfterMs: PIPELINE_SETTLE_MS,
    });
    const upgrade = upgradeRequest(world.localHost);
    const response = await world.send(upgrade + SMUGGLED_REQUEST);
    expect(upstream.received.toString("latin1")).toBe(rewritten(upgrade));
    expect(response.toString("latin1")).toBe(
      `HTTP/1.1 426 Upgrade Required\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n${body}`,
    );
  });

  it("un upgrade aceptado con 101 hace de tubería en los dos sentidos", async () => {
    const switching = "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n";
    const { world, upstream } = await startWorld({
      response: `${switching}server-frame`,
      closeAfterMs: PIPELINE_SETTLE_MS,
    });
    const upgrade = upgradeRequest(world.localHost);
    const response = await world.send(`${upgrade}client-frame`);
    expect(upstream.received.toString("latin1")).toBe(`${rewritten(upgrade)}client-frame`);
    expect(response.toString("latin1")).toBe(`${switching}server-frame`);
  });

  it("las respuestas informativas anteriores al 101 pasan tal cual", async () => {
    const answer = "HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 101 Switching Protocols\r\n\r\nok";
    const { world } = await startWorld({ response: answer });
    const response = await world.send(upgradeRequest(world.localHost));
    expect(response.toString("latin1")).toBe(answer);
  });

  it.each([
    ["basura", "garbage\r\n\r\n"],
    ["cabecera inválida", "HTTP/1.1 200 OK\r\nBad Header\r\n\r\n"],
    ["sin respuesta", ""],
  ])("una respuesta de upgrade %s es 502", async (_name, answer) => {
    const { world, upstream } = await startWorld(
      answer === "" ? { silent: true } : { response: answer },
    );
    const upgrade = upgradeRequest(world.localHost);
    const response = await world.send(upgrade + SMUGGLED_REQUEST);
    expect(response.toString("latin1")).toBe(BAD_GATEWAY_RESPONSE);
    expect(upstream.received.toString("latin1")).toBe(rewritten(upgrade));
  });

  it("una petición encadenada tras un cuerpo Content-Length nunca llega al upstream", async () => {
    const { world, upstream } = await startWorld({ closeAfterMs: PIPELINE_SETTLE_MS });
    const first = `POST /a HTTP/1.1\r\nHost: ${world.localHost}\r\nContent-Length: 5\r\n\r\n`;
    const response = await world.send(`${first}hello${SMUGGLED_REQUEST}`);
    expect(upstream.received.toString("latin1")).toBe(`${rewritten(first)}hello`);
    expect(response.toString("latin1")).toBe(DEFAULT_UPSTREAM_RESPONSE);
  });

  it("una petición encadenada tras un cuerpo chunked nunca llega al upstream", async () => {
    const { world, upstream } = await startWorld({ closeAfterMs: PIPELINE_SETTLE_MS });
    const first = `POST /a HTTP/1.1\r\nHost: ${world.localHost}\r\nTransfer-Encoding: chunked\r\n\r\n`;
    const body = "5;ext=1\r\nhello\r\n0\r\nX-Trailer: t\r\n\r\n";
    await world.send(first + body + SMUGGLED_REQUEST);
    expect(upstream.received.toString("latin1")).toBe(rewritten(first) + body);
  });

  it("una petición sin cuerpo reenvía sólo su cabecera", async () => {
    const { world, upstream } = await startWorld({ closeAfterMs: PIPELINE_SETTLE_MS });
    const first = `GET /a HTTP/1.1\r\nHost: ${world.localHost}\r\n\r\n`;
    await world.send(first + SMUGGLED_REQUEST);
    expect(upstream.received.toString("latin1")).toBe(rewritten(first));
  });

  it.each([
    ["Transfer-Encoding: chunked\r\nContent-Length: 5"],
    ["Transfer-Encoding: gzip"],
    ["Transfer-Encoding: chunked, gzip"],
    ["Content-Length: 5\r\nContent-Length: 6"],
    ["Content-Length: 5, 6"],
    ["Content-Length: -1"],
    ["Content-Length: abc"],
    ["Content-Length: 5\u0085"],
  ])("una delimitación ambigua (%s) es 400 y nunca llega al upstream", async (framing) => {
    const { world, upstream } = await startWorld();
    const response = await world.send(
      `POST / HTTP/1.1\r\nHost: ${world.localHost}\r\n${framing}\r\n\r\nhello`,
    );
    expect(response.toString("latin1")).toBe(BAD_REQUEST_RESPONSE);
    expect(upstream.connections).toBe(0);
  });

  it("un Content-Length fuera de lo que Number representa es 400", async () => {
    const { world, upstream } = await startWorld();
    const response = await world.send(
      `POST / HTTP/1.1\r\nHost: ${world.localHost}\r\nContent-Length: 99999999999999999999\r\n\r\n`,
    );
    expect(response.toString("latin1")).toBe(BAD_REQUEST_RESPONSE);
    expect(upstream.connections).toBe(0);
  });

  it("un trozo mal formado deja de reenviar sin pasar al upstream el tamaño inválido", async () => {
    const { world, upstream } = await startWorld({ closeAfterMs: PIPELINE_SETTLE_MS });
    const first = `POST /a HTTP/1.1\r\nHost: ${world.localHost}\r\nTransfer-Encoding: chunked\r\n\r\n`;
    await world.send(`${first}zz\r\nhello\r\n0\r\n\r\n${SMUGGLED_REQUEST}`);
    const received = upstream.received.toString("latin1");
    expect(received).not.toContain("hello");
    expect(received).not.toContain("zz");
    expect(received).not.toContain("9000");
  });
});

describe("fallos del upstream y del cliente", () => {
  it("sin JWE vigente responde 502 e informa sin JWE, ruta ni Host", async () => {
    const { world, upstream } = await startWorld({}, { jwe: undefined });
    const response = await world.send(
      `GET /private-path HTTP/1.1\r\nHost: ${world.localHost}\r\n\r\n`,
    );
    expect(response.toString("latin1")).toBe(BAD_GATEWAY_RESPONSE);
    expect(upstream.connections).toBe(0);
    const reported = world.reports.join("\n");
    expect(reported).toContain("JWE");
    expect(reported).not.toContain("private-path");
    expect(reported).not.toContain("localhost");
  });

  it("si la conexión al upstream falla responde 502 e informa sin JWE ni ruta", async () => {
    const { world } = await startWorld(
      {},
      { connector: () => Promise.reject(new Error("ECONNREFUSED")), jwe: "JWE-REFUSED" },
    );
    const response = await world.send(
      `GET /private-path HTTP/1.1\r\nHost: ${world.localHost}\r\n\r\n`,
    );
    expect(response.toString("latin1")).toBe(BAD_GATEWAY_RESPONSE);
    const reported = world.reports.join("\n");
    expect(reported).toContain("upstream");
    expect(reported).not.toContain("JWE-REFUSED");
    expect(reported).not.toContain("private-path");
  });

  it("si la conexión al upstream no se abre a tiempo responde 502", async () => {
    const { world } = await startWorld(
      {},
      {
        connector: () => new Promise(() => undefined),
        timeouts: { headRead: 5, upstreamConnect: SHORT_TIMEOUT_SECONDS, upstreamResponseHead: 5 },
      },
    );
    const response = await world.send(`GET / HTTP/1.1\r\nHost: ${world.localHost}\r\n\r\n`);
    expect(response.toString("latin1")).toBe(BAD_GATEWAY_RESPONSE);
    expect(world.reports.join("\n")).toContain("a tiempo");
  });

  it("cierra a un cliente ocioso tras el plazo de la cabecera", async () => {
    const { world, upstream } = await startWorld(
      {},
      {
        timeouts: { headRead: SHORT_TIMEOUT_SECONDS, upstreamConnect: 5, upstreamResponseHead: 5 },
      },
    );
    const socket = await world.open();
    socket.write("GET / HTTP/1.1\r\n");
    await new Promise<void>((resolve) => socket.once("end", () => resolve()));
    expect(upstream.connections).toBe(0);
    socket.destroy();
  });

  it("cierra sin reenviar una cabecera que supera el tope de bytes", async () => {
    const { world, upstream } = await startWorld();
    const response = await world.send(
      `GET / HTTP/1.1\r\nHost: ${world.localHost}\r\nX-Pad: ${"a".repeat(OVERSIZED_HEAD_BYTES)}\r\n\r\n`,
    );
    expect(response.length).toBe(0);
    expect(upstream.connections).toBe(0);
  });

  it("si el upstream responde con un cuerpo hasta el cierre, lo entrega entero", async () => {
    const answer = `HTTP/1.1 200 OK\r\n\r\n${"x".repeat(200_000)}`;
    const { world } = await startWorld({ response: answer });
    const response = await world.send(`GET / HTTP/1.1\r\nHost: ${world.localHost}\r\n\r\n`);
    expect(response.toString("latin1")).toBe(answer);
  });
});

async function waitFor(condition: () => boolean, timeoutMs = 3000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!condition()) {
    if (Date.now() > deadline) {
      throw new Error("la condición no se cumplió a tiempo");
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}
