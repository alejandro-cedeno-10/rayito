/**
 * `proxy` (D18): el túnel HTTP `CONNECT` contra un servidor `net` local que
 * hace de proxy. La línea `CONNECT`, `Proxy-Authorization`, el paso de bytes
 * tras el 200, el 407 sin credenciales en el mensaje, el `createConnection`
 * de `openTransport` y el `ProxyTunnelAgent` del plano de control.
 */

import net from "node:net";
import { afterEach, describe, expect, test } from "vitest";
import { clientConfig, requestHandlerOptions } from "../../src/aws/control-plane.js";
import { InvalidArgumentError, SandboxError } from "../../src/errors.js";
import {
  connectRequest,
  PROXY_URL_MESSAGE,
  ProxyTunnelAgent,
  ProxyTunnelSocket,
  parseProxyUrl,
  validateProxyUrl,
} from "../../src/transport/proxy-tunnel.js";
import { resolveTransportSettings, sessionOptions } from "../../src/transport/transport.js";
import { createTestSandbox, withTimeout } from "./helpers.js";

const PROXY_PASSWORD = "clave-secreta";
const BUDGET_MS = 5000;

interface ProxyOptions {
  readonly status?: number;
  /** Sin `echo`, un 200 abre un túnel real hacia el destino pedido. */
  readonly echo?: boolean;
}

interface LocalProxy {
  readonly port: number;
  readonly heads: string[];
  readonly received: Buffer[];
  url(userinfo?: string): string;
  close(): Promise<void>;
}

const proxies: LocalProxy[] = [];

afterEach(async () => {
  for (const proxy of proxies.splice(0)) {
    await proxy.close();
  }
});

async function startProxy(options: ProxyOptions = {}): Promise<LocalProxy> {
  const heads: string[] = [];
  const received: Buffer[] = [];
  const sockets = new Set<net.Socket>();
  const server = net.createServer((client) => {
    sockets.add(client);
    client.on("close", () => sockets.delete(client));
    client.on("error", () => undefined);
    let buffered = Buffer.alloc(0);
    const onHead = (data: Buffer) => {
      buffered = Buffer.concat([buffered, data]);
      const end = buffered.indexOf("\r\n\r\n");
      if (end < 0) {
        return;
      }
      client.off("data", onHead);
      const head = buffered.subarray(0, end).toString("latin1");
      const rest = buffered.subarray(end + 4);
      heads.push(head);
      const status = options.status ?? 200;
      if (status !== 200) {
        client.end(`HTTP/1.1 ${status} Proxy Authentication Required\r\n\r\n`);
        return;
      }
      if (options.echo ?? true) {
        client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
        const onTunnel = (chunk: Buffer) => {
          received.push(chunk);
          client.write(chunk);
        };
        if (rest.length > 0) {
          onTunnel(rest);
        }
        client.on("data", onTunnel);
        return;
      }
      const [, authority] = /^CONNECT (\S+) /.exec(head) ?? [];
      const colon = (authority ?? "").lastIndexOf(":");
      const upstream = net.connect(
        Number((authority ?? "").slice(colon + 1)),
        (authority ?? "").slice(0, colon).replace(/^\[(.*)\]$/, "$1"),
      );
      sockets.add(upstream);
      upstream.on("close", () => sockets.delete(upstream));
      upstream.on("error", () => client.destroy());
      upstream.once("connect", () => {
        client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
        if (rest.length > 0) {
          upstream.write(rest);
        }
        client.pipe(upstream);
        upstream.pipe(client);
      });
    };
    client.on("data", onHead);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as net.AddressInfo).port;
  const proxy: LocalProxy = {
    port,
    heads,
    received,
    url: (userinfo) => `http://${userinfo === undefined ? "" : `${userinfo}@`}127.0.0.1:${port}`,
    close: async () => {
      for (const socket of sockets) {
        socket.destroy();
      }
      await new Promise<void>((resolve) => server.close(() => resolve()));
    },
  };
  proxies.push(proxy);
  return proxy;
}

function readOnce(socket: ProxyTunnelSocket): Promise<Buffer> {
  return withTimeout(
    new Promise<Buffer>((resolve, reject) => {
      socket.once("data", (data: Buffer) => resolve(data));
      socket.once("error", reject);
    }),
    BUDGET_MS,
    "el túnel no devolvió nada",
  );
}

function failureOf(socket: ProxyTunnelSocket): Promise<Error> {
  return withTimeout(
    new Promise<Error>((resolve) => socket.once("error", resolve)),
    BUDGET_MS,
    "el túnel no falló",
  );
}

describe("proxy URL validation", () => {
  test("http with host and explicit port passes; userinfo becomes Basic auth", () => {
    expect(validateProxyUrl(undefined)).toBeUndefined();
    expect(parseProxyUrl("http://proxy.example.com:3128")).toEqual({
      host: "proxy.example.com",
      port: 3128,
      authorization: undefined,
    });
    const parsed = parseProxyUrl(`http://u:${PROXY_PASSWORD}@127.0.0.1:3128`);
    expect(parsed.authorization).toBe(
      `Basic ${Buffer.from(`u:${PROXY_PASSWORD}`).toString("base64")}`,
    );
  });

  test("https, a missing port and a non-string are refused without echoing the URL", () => {
    for (const bad of [
      `https://u:${PROXY_PASSWORD}@h:3128`,
      `http://u:${PROXY_PASSWORD}@h`,
      `socks5://u:${PROXY_PASSWORD}@h:1080`,
      42,
    ]) {
      expect(() => validateProxyUrl(bad)).toThrow(new InvalidArgumentError(PROXY_URL_MESSAGE));
    }
    expect(() => resolveTransportSettings({ proxy: "http://h" })).toThrow(InvalidArgumentError);
  });

  test("the CONNECT request carries Host and, with credentials, Proxy-Authorization", () => {
    expect(connectRequest("h", 443, undefined)).toBe(
      "CONNECT h:443 HTTP/1.1\r\nHost: h:443\r\n\r\n",
    );
    expect(connectRequest("::1", 443, "Basic x")).toBe(
      "CONNECT [::1]:443 HTTP/1.1\r\nHost: [::1]:443\r\nProxy-Authorization: Basic x\r\n\r\n",
    );
  });
});

describe("ProxyTunnelSocket", () => {
  test("sends CONNECT with Proxy-Authorization and passes bytes after the 200", async () => {
    const proxy = await startProxy();
    const socket = new ProxyTunnelSocket(
      parseProxyUrl(proxy.url(`u:${PROXY_PASSWORD}`)),
      "sandbox.example.com",
      443,
    );
    try {
      socket.write("hola túnel");
      const echoed = await readOnce(socket);
      expect(echoed.toString("utf8")).toBe("hola túnel");
      expect(socket.established).toBe(true);
      const [head] = proxy.heads;
      expect(head?.split("\r\n")[0]).toBe("CONNECT sandbox.example.com:443 HTTP/1.1");
      expect(head).toContain(
        `Proxy-Authorization: Basic ${Buffer.from(`u:${PROXY_PASSWORD}`).toString("base64")}`,
      );
      expect(Buffer.concat(proxy.received).toString("utf8")).toBe("hola túnel");
    } finally {
      socket.destroy();
    }
  });

  test("a 407 destroys the socket with the status and without the credentials", async () => {
    const proxy = await startProxy({ status: 407 });
    const url = proxy.url(`u:${PROXY_PASSWORD}`);
    const socket = new ProxyTunnelSocket(parseProxyUrl(url), "sandbox.example.com", 443);
    const pending = new Promise<Error | null | undefined>((resolve) => {
      socket.write("never-sent", (error) => resolve(error));
    });
    const error = await failureOf(socket);
    expect(error).toBeInstanceOf(SandboxError);
    expect(error.message).toBe("el proxy rechazó CONNECT con HTTP 407");
    expect(error.message).not.toContain(PROXY_PASSWORD);
    expect(error.message).not.toContain(url);
    expect(await pending).toBeInstanceOf(Error);
    expect(proxy.received).toEqual([]);
  });
});

describe("proxy wiring", () => {
  test("openTransport gets a createConnection only when proxy is set", () => {
    const plain = resolveTransportSettings({});
    expect(sessionOptions("sandbox.example.com", plain)).toBeUndefined();
    const proxied = resolveTransportSettings({ proxy: "http://127.0.0.1:3128" });
    expect(typeof sessionOptions("sandbox.example.com", proxied)?.createConnection).toBe(
      "function",
    );
  });

  test("a sandbox reaches rayd through the CONNECT tunnel", async () => {
    const proxy = await startProxy({ echo: false });
    let transport: Record<string, unknown> = {};
    const { sandbox, rayd } = await createTestSandbox({
      beforeCreate: (fake) => {
        transport = { ...fake.transport, proxy: proxy.url(`u:${PROXY_PASSWORD}`) };
      },
      create: {
        get transport() {
          return transport;
        },
      },
    });
    const result = await sandbox.commands.run("echo hi");
    expect(result.stdout).toBe("hi\n");
    expect(proxy.heads.length).toBeGreaterThan(0);
    expect(proxy.heads[0]?.split("\r\n")[0]).toBe(`CONNECT 127.0.0.1:${rayd.port} HTTP/1.1`);
  });

  test("the control plane client uses a ProxyTunnelAgent only with proxy", () => {
    expect(requestHandlerOptions(undefined).httpsAgent).toBeUndefined();
    expect(requestHandlerOptions("http://127.0.0.1:3128").httpsAgent).toBeInstanceOf(
      ProxyTunnelAgent,
    );
    expect(() => clientConfig("us-east-1", { proxy: "https://h:1" })).toThrow(InvalidArgumentError);
  });
});
