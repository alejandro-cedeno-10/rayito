/**
 * Un cliente HTTP de prueba del servidor local del panel: arranca
 * `LocalDashboard` en loopback, canjea el token de arranque por la cookie y
 * pide el token CSRF, como la SPA. Las peticiones usan `node:http` para poder
 * fijar `Host`, cabeceras sueltas y cuerpos que `fetch` no deja escribir.
 */

import { request as httpRequest } from "node:http";
import { dashboardStaticDirectory } from "../../src/cli/dashboard-assets.js";
import { CSRF_HEADER, LOOPBACK, LocalDashboard } from "../../src/cli/dashboard-server.js";
import type { ApiBackend } from "../../src/dashboard/api/contract.js";
import type { Appearance } from "../../src/dashboard/theme.js";

export interface RawResponse {
  readonly status: number;
  readonly headers: Record<string, string>;
  readonly body: string;
}

export interface RawOptions {
  readonly headers?: Readonly<Record<string, string>>;
  readonly body?: string;
  readonly sign?: boolean;
}

export class LocalClient {
  readonly server: LocalDashboard;
  /** La ruta con el token de arranque: sólo se puede leer antes de canjearlo. */
  readonly startTarget: string;
  cookie = "";
  csrf = "";

  private constructor(server: LocalDashboard) {
    this.server = server;
    const url = new URL(server.url);
    this.startTarget = `${url.pathname}${url.search}`;
  }

  get port(): number {
    return this.server.port;
  }

  get host(): string {
    return `${LOOPBACK}:${this.port}`;
  }

  static async start(
    api: ApiBackend,
    options: { readonly appearance?: Appearance; readonly signIn?: boolean } = {},
  ): Promise<LocalClient> {
    const server = await LocalDashboard.start(api, {
      port: 0,
      appearance: options.appearance,
      staticDirectory: dashboardStaticDirectory(),
    });
    const client = new LocalClient(server);
    if (options.signIn !== false) {
      await client.signIn();
    }
    return client;
  }

  async signIn(): Promise<void> {
    const redeemed = await this.raw("GET", this.startTarget, { sign: false });
    this.cookie = (redeemed.headers["set-cookie"] ?? "").split(";")[0] as string;
    this.csrf = JSON.parse((await this.raw("GET", "/api/v1/session")).body).csrfToken;
  }

  raw(method: string, target: string, options: RawOptions = {}): Promise<RawResponse> {
    const headers: Record<string, string> = { Host: this.host };
    if (options.sign !== false && this.cookie !== "") {
      headers.Cookie = this.cookie;
    }
    Object.assign(headers, options.headers);
    if (options.body !== undefined && !("Content-Length" in headers)) {
      headers["Content-Length"] = String(Buffer.byteLength(options.body));
    }
    return new Promise((resolve, reject) => {
      const request = httpRequest(
        { host: LOOPBACK, port: this.port, method, path: target, headers, setHost: false },
        (response) => {
          const chunks: Buffer[] = [];
          response.on("data", (chunk: Buffer) => chunks.push(chunk));
          response.on("end", () =>
            resolve({
              status: response.statusCode ?? 0,
              headers: Object.fromEntries(
                Object.entries(response.headers).map(([name, value]) => [
                  name,
                  Array.isArray(value) ? value.join(", ") : (value ?? ""),
                ]),
              ),
              body: Buffer.concat(chunks).toString("utf8"),
            }),
          );
        },
      );
      request.on("error", reject);
      request.end(options.body);
    });
  }

  /** Como la SPA: con el token CSRF y, en un `POST`, JSON. */
  async call(
    method: string,
    path: string,
    body?: unknown,
  ): Promise<{ status: number; body: Record<string, unknown> }> {
    const headers: Record<string, string> = { [CSRF_HEADER]: this.csrf };
    if (method === "POST") {
      headers["Content-Type"] = "application/json";
    }
    const response = await this.raw(method, `/api/v1${path}`, {
      headers,
      ...(method === "POST" ? { body: JSON.stringify(body ?? {}) } : {}),
    });
    return { status: response.status, body: JSON.parse(response.body) };
  }

  close(): Promise<void> {
    return this.server.shutdown();
  }
}
