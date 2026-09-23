/**
 * S3 falso en `127.0.0.1:0` que habla el subconjunto de §18 en estilo ruta
 * (`/bucket/key`): `PutObject`, `GetObject` (con `Range`), `DeleteObject`,
 * `HeadObject`, el POST de formulario con `content-length-range`, y la subida
 * multiparte (`CreateMultipartUpload`, `UploadPart`, `CompleteMultipartUpload`,
 * `AbortMultipartUpload`). No verifica firmas; sí la caducidad de una URL
 * prefirmada (`X-Amz-Date` + `X-Amz-Expires` → 403 `Request has expired`).
 * Lo usan a la vez el SDK (con `endpoint` apuntando aquí) y el `rayd` falso
 * (que hace `fetch` de las URLs prefirmadas), y registra cada petición.
 */

import { createHash, randomBytes } from "node:crypto";
import http from "node:http";
import type { AddressInfo } from "node:net";

export interface StoredObject {
  readonly data: Uint8Array;
  readonly contentType: string | undefined;
  readonly etag: string;
}

export interface S3RequestRecord {
  readonly method: string;
  readonly bucket: string;
  readonly key: string;
  readonly query: Readonly<Record<string, string>>;
  readonly headers: Readonly<Record<string, string>>;
  readonly bodyBytes: number;
}

interface PendingUpload {
  readonly bucket: string;
  readonly key: string;
  readonly contentType: string | undefined;
  readonly parts: Map<number, StoredObject>;
}

const XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>';
const EXPIRED_MESSAGE = "Request has expired";

function etagOf(data: Uint8Array): string {
  return `"${createHash("md5").update(data).digest("hex")}"`;
}

function xmlEscape(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function xmlUnescape(text: string): string {
  return text
    .replaceAll("&quot;", '"')
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&amp;", "&");
}

function amzDateMs(value: string): number {
  const match = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(value);
  if (match === null) {
    return Number.NaN;
  }
  const [, year, month, day, hour, minute, second] = match.map(Number) as number[];
  return Date.UTC(
    year as number,
    (month as number) - 1,
    day as number,
    hour as number,
    minute as number,
    second as number,
  );
}

async function readBody(request: http.IncomingMessage): Promise<Uint8Array> {
  const parts: Buffer[] = [];
  for await (const chunk of request) {
    parts.push(chunk as Buffer);
  }
  return new Uint8Array(Buffer.concat(parts));
}

function headerRecord(request: http.IncomingMessage): Record<string, string> {
  const headers: Record<string, string> = {};
  for (const [name, value] of Object.entries(request.headers)) {
    if (value !== undefined) {
      headers[name.toLowerCase()] = Array.isArray(value) ? value.join(",") : value;
    }
  }
  return headers;
}

export class FakeS3 {
  readonly objects = new Map<string, StoredObject>();
  readonly uploads = new Map<string, PendingUpload>();
  readonly requests: S3RequestRecord[] = [];
  /** Métodos que, firmados por cabecera (el SDK, no una URL prefirmada), nunca reciben respuesta. */
  readonly stalled = new Set<string>();
  /** Métodos que responden `403 AccessDenied` sea cual sea la firma. */
  readonly denied = new Set<string>();
  readonly server: http.Server;
  readonly port: number;

  private constructor(server: http.Server, port: number) {
    this.server = server;
    this.port = port;
  }

  static async start(): Promise<FakeS3> {
    let fake: FakeS3 | undefined;
    const server = http.createServer((request, response) => {
      void (fake as FakeS3).handle(request, response);
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    fake = new FakeS3(server, (server.address() as AddressInfo).port);
    return fake;
  }

  get endpoint(): string {
    return `http://127.0.0.1:${this.port}`;
  }

  object(bucket: string, key: string): StoredObject | undefined {
    return this.objects.get(`${bucket}/${key}`);
  }

  putObject(bucket: string, key: string, data: Uint8Array): void {
    this.objects.set(`${bucket}/${key}`, {
      data,
      contentType: "application/octet-stream",
      etag: etagOf(data),
    });
  }

  keys(bucket: string): string[] {
    const prefix = `${bucket}/`;
    return [...this.objects.keys()]
      .filter((name) => name.startsWith(prefix))
      .map((name) => name.slice(prefix.length));
  }

  requestsFor(method: string): S3RequestRecord[] {
    return this.requests.filter((request) => request.method === method);
  }

  async close(): Promise<void> {
    this.server.closeAllConnections();
    await new Promise<void>((resolve) => this.server.close(() => resolve()));
  }

  async handle(request: http.IncomingMessage, response: http.ServerResponse): Promise<void> {
    const url = new URL(request.url ?? "/", this.endpoint);
    const [bucket = "", ...rest] = url.pathname.slice(1).split("/");
    const key = rest.map((segment) => decodeURIComponent(segment)).join("/");
    const query = Object.fromEntries(url.searchParams.entries());
    const body = await readBody(request);
    const headers = headerRecord(request);
    this.requests.push({
      method: request.method ?? "",
      bucket,
      key,
      query,
      headers,
      bodyBytes: body.byteLength,
    });
    if (this.stalled.has(request.method ?? "") && !("X-Amz-Signature" in query)) {
      return;
    }
    if (this.denied.has(request.method ?? "")) {
      this.#error(response, 403, "AccessDenied", "access denied");
      return;
    }
    if (this.#expired(query)) {
      this.#error(response, 403, "AccessDenied", EXPIRED_MESSAGE);
      return;
    }
    this.#route(request.method ?? "", bucket, key, query, headers, body, response);
  }

  #expired(query: Readonly<Record<string, string>>): boolean {
    const date = query["X-Amz-Date"];
    const expires = query["X-Amz-Expires"];
    if (date === undefined || expires === undefined) {
      return false;
    }
    return Date.now() > amzDateMs(date) + Number(expires) * 1000;
  }

  #route(
    method: string,
    bucket: string,
    key: string,
    query: Readonly<Record<string, string>>,
    headers: Readonly<Record<string, string>>,
    body: Uint8Array,
    response: http.ServerResponse,
  ): void {
    const uploadId = query.uploadId;
    if (method === "POST" && key === "") {
      void this.#formPost(bucket, headers, body, response);
      return;
    }
    if (method === "POST" && "uploads" in query) {
      this.#createUpload(bucket, key, headers, response);
      return;
    }
    if (method === "PUT" && uploadId !== undefined) {
      this.#uploadPart(uploadId, Number(query.partNumber), body, response);
      return;
    }
    if (method === "POST" && uploadId !== undefined) {
      this.#completeUpload(uploadId, body, response);
      return;
    }
    if (method === "DELETE" && uploadId !== undefined) {
      this.uploads.delete(uploadId);
      response.writeHead(204).end();
      return;
    }
    switch (method) {
      case "PUT":
        this.#put(bucket, key, headers, body, response);
        return;
      case "GET":
      case "HEAD":
        this.#get(bucket, key, headers, method === "HEAD", response);
        return;
      case "DELETE":
        this.objects.delete(`${bucket}/${key}`);
        response.writeHead(204).end();
        return;
      default:
        this.#error(response, 405, "MethodNotAllowed", "method not allowed");
    }
  }

  #put(
    bucket: string,
    key: string,
    headers: Readonly<Record<string, string>>,
    body: Uint8Array,
    response: http.ServerResponse,
  ): void {
    const stored = { data: body, contentType: headers["content-type"], etag: etagOf(body) };
    this.objects.set(`${bucket}/${key}`, stored);
    response.writeHead(200, { ETag: stored.etag }).end();
  }

  #get(
    bucket: string,
    key: string,
    headers: Readonly<Record<string, string>>,
    headOnly: boolean,
    response: http.ServerResponse,
  ): void {
    const stored = this.objects.get(`${bucket}/${key}`);
    if (stored === undefined) {
      if (headOnly) {
        response.writeHead(404).end();
        return;
      }
      this.#error(response, 404, "NoSuchKey", "The specified key does not exist.");
      return;
    }
    const range = /^bytes=(\d+)-(\d*)$/.exec(headers.range ?? "");
    const start = range === null ? 0 : Number(range[1]);
    const end = range === null || range[2] === "" ? stored.data.byteLength - 1 : Number(range[2]);
    const slice = stored.data.subarray(start, end + 1);
    const status = range === null ? 200 : 206;
    const responseHeaders: Record<string, string | number> = {
      "Content-Length": slice.byteLength,
      "Content-Type": stored.contentType ?? "binary/octet-stream",
      ETag: stored.etag,
    };
    if (range !== null) {
      responseHeaders["Content-Range"] = `bytes ${start}-${end}/${stored.data.byteLength}`;
    }
    response.writeHead(status, responseHeaders);
    response.end(headOnly ? undefined : Buffer.from(slice));
  }

  #createUpload(
    bucket: string,
    key: string,
    headers: Readonly<Record<string, string>>,
    response: http.ServerResponse,
  ): void {
    const uploadId = randomBytes(12).toString("hex");
    this.uploads.set(uploadId, {
      bucket,
      key,
      contentType: headers["content-type"],
      parts: new Map(),
    });
    this.#xml(
      response,
      200,
      `<InitiateMultipartUploadResult><Bucket>${bucket}</Bucket><Key>${xmlEscape(key)}</Key>` +
        `<UploadId>${uploadId}</UploadId></InitiateMultipartUploadResult>`,
    );
  }

  #uploadPart(
    uploadId: string,
    partNumber: number,
    body: Uint8Array,
    response: http.ServerResponse,
  ): void {
    const upload = this.uploads.get(uploadId);
    if (upload === undefined) {
      this.#error(response, 404, "NoSuchUpload", "The specified upload does not exist.");
      return;
    }
    const part = { data: body, contentType: undefined, etag: etagOf(body) };
    upload.parts.set(partNumber, part);
    response.writeHead(200, { ETag: part.etag }).end();
  }

  #completeUpload(uploadId: string, body: Uint8Array, response: http.ServerResponse): void {
    const upload = this.uploads.get(uploadId);
    if (upload === undefined) {
      this.#error(response, 404, "NoSuchUpload", "The specified upload does not exist.");
      return;
    }
    const listed = [...new TextDecoder().decode(body).matchAll(/<Part>([\s\S]*?)<\/Part>/g)].map(
      (match) => {
        const inner = match[1] as string;
        return {
          etag: xmlUnescape(/<ETag>([\s\S]*?)<\/ETag>/.exec(inner)?.[1] ?? ""),
          partNumber: Number(/<PartNumber>(\d+)<\/PartNumber>/.exec(inner)?.[1]),
        };
      },
    );
    const chunks: Uint8Array[] = [];
    for (const { etag, partNumber } of listed) {
      const part = upload.parts.get(partNumber);
      if (part === undefined || part.etag !== etag) {
        this.#error(
          response,
          400,
          "InvalidPart",
          "One or more of the specified parts was not found.",
        );
        return;
      }
      chunks.push(part.data);
    }
    const data = new Uint8Array(Buffer.concat(chunks));
    const stored = { data, contentType: upload.contentType, etag: etagOf(data) };
    this.objects.set(`${upload.bucket}/${upload.key}`, stored);
    this.uploads.delete(uploadId);
    this.#xml(
      response,
      200,
      `<CompleteMultipartUploadResult><Bucket>${upload.bucket}</Bucket>` +
        `<Key>${xmlEscape(upload.key)}</Key><ETag>${xmlEscape(stored.etag)}</ETag>` +
        "</CompleteMultipartUploadResult>",
    );
  }

  async #formPost(
    bucket: string,
    headers: Readonly<Record<string, string>>,
    body: Uint8Array,
    response: http.ServerResponse,
  ): Promise<void> {
    const form = await new Response(body, {
      headers: { "content-type": headers["content-type"] ?? "" },
    }).formData();
    const key = String(form.get("key") ?? "");
    const file = form.get("file");
    const data = file instanceof Blob ? new Uint8Array(await file.arrayBuffer()) : new Uint8Array();
    const policy = JSON.parse(
      Buffer.from(String(form.get("Policy") ?? ""), "base64").toString(),
    ) as {
      conditions?: unknown[];
    };
    const range = (policy.conditions ?? []).find(
      (condition): condition is [string, number, number] =>
        Array.isArray(condition) && condition[0] === "content-length-range",
    );
    if (range !== undefined && data.byteLength > range[2]) {
      this.#error(
        response,
        400,
        "EntityTooLarge",
        "Your proposed upload exceeds the maximum allowed size",
      );
      return;
    }
    this.objects.set(`${bucket}/${key}`, {
      data,
      contentType: undefined,
      etag: etagOf(data),
    });
    response.writeHead(204).end();
  }

  #xml(response: http.ServerResponse, status: number, body: string): void {
    response.writeHead(status, { "Content-Type": "application/xml" });
    response.end(`${XML_HEADER}${body}`);
  }

  #error(response: http.ServerResponse, status: number, code: string, message: string): void {
    this.#xml(
      response,
      status,
      `<Error><Code>${code}</Code><Message>${message}</Message><RequestId>fake-request</RequestId></Error>`,
    );
  }
}
