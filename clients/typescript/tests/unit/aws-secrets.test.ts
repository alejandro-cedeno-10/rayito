/**
 * Un error del SDK de AWS v3 lleva `$response` y, a través de él, la petición
 * HTTP cruda (cabeceras `authorization` y `x-amz-security-token`, buffers del
 * socket). Ningún error que devuelva el SDK de Rayito puede exponerlos, ni
 * en `util.inspect`, ni en `JSON.stringify`, ni en `String(error.cause)`.
 */

import http from "node:http";
import type { AddressInfo } from "node:net";
import { inspect } from "node:util";
import { LambdaMicrovmsClient } from "@aws-sdk/client-lambda-microvms";
import { describe, expect, test } from "vitest";
import { LambdaMicrovmsControlPlane, translateAwsError } from "../../src/aws/control-plane.js";
import { AwsErrorSummary, redactAwsText, sanitizeAwsError } from "../../src/aws/sanitize.js";
import {
  AuthenticationError,
  InvalidArgumentError,
  RateLimitError,
  SandboxError,
} from "../../src/errors.js";
import { createS3Access, translateS3Error } from "../../src/sandbox/transfer.js";
import { SANDBOX_ID } from "./fake/control-plane.js";

const ACCESS_KEY_ID = "ASIAFAKEKEYIDEXAMPLE";
const SESSION_TOKEN = "FAKESESSIONTOKEN";
const SIGNATURE_PARAM = "X-Amz-Signature";
const SECRETS = [ACCESS_KEY_ID, SESSION_TOKEN, SIGNATURE_PARAM] as const;
const NODE_HEADER_KEY = "_header";
const NODE_PENDING_KEY = "_pendingData";
const REQUEST_ID = "req-0123456789";
const EXTENDED_REQUEST_ID = "ext-id-2-abcdef";
const REGION = "us-east-1";
const BUCKET = "amzn-s3-demo-bucket";
const CREDENTIALS = {
  accessKeyId: ACCESS_KEY_ID,
  secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
  sessionToken: SESSION_TOKEN,
};
const AUTHORIZATION =
  `AWS4-HMAC-SHA256 Credential=${ACCESS_KEY_ID}/20260924/${REGION}/s3/aws4_request, ` +
  "SignedHeaders=host;x-amz-date;x-amz-security-token, Signature=" +
  "f".repeat(64);
const PRESIGNED_URL =
  `https://${BUCKET}.s3.${REGION}.amazonaws.com/k?X-Amz-Algorithm=AWS4-HMAC-SHA256` +
  `&X-Amz-Credential=${ACCESS_KEY_ID}%2F20260924%2F${REGION}%2Fs3%2Faws4_request` +
  `&X-Amz-Security-Token=${SESSION_TOKEN}&${SIGNATURE_PARAM}=${"a".repeat(64)}`;
const RAW_REQUEST =
  `PUT /k HTTP/1.1\r\nhost: ${BUCKET}.s3.${REGION}.amazonaws.com\r\n` +
  `authorization: ${AUTHORIZATION}\r\nx-amz-security-token: ${SESSION_TOKEN}\r\n\r\n`;
const CANONICAL_REQUEST =
  `GET\n/2026-09-24/microvms/${SANDBOX_ID}\n\nhost:localhost\nx-amz-date:20260924T000000Z\n` +
  `x-amz-security-token:${SESSION_TOKEN}\n\nhost;x-amz-date;x-amz-security-token\n${"e".repeat(64)}`;
const INVALID_SIGNATURE_MESSAGE =
  "The request signature we calculated does not match the signature you provided. " +
  "Check your AWS Secret Access Key and signing method. Consult the service documentation " +
  `for details.\n\nThe Canonical String for this request should have been\n'${CANONICAL_REQUEST}'` +
  `\n\nThe String-to-Sign should have been\n'AWS4-HMAC-SHA256\n20260924T000000Z\n` +
  `20260924/${REGION}/lambda-microvms/aws4_request\n${"b".repeat(64)}'\n`;

/**
 * La forma de un `ServiceException` de smithy tal como lo lanza `send()`:
 * `$response` con cabeceras, cuerpo y la petición cruda colgando del socket,
 * más los campos del cuerpo de error de S3 que repiten la firma.
 */
function fakeSdkError(name: string, extra: Record<string, unknown> = {}): Error {
  // `_header` y `_pendingData` son los campos reales de `ClientRequest` y del socket de Node.
  const socket = {
    parser: { outgoing: { [NODE_HEADER_KEY]: RAW_REQUEST } },
    [NODE_PENDING_KEY]: RAW_REQUEST,
  };
  const request = {
    method: "PUT",
    path: `/k?${PRESIGNED_URL.split("?")[1]}`,
    headers: { authorization: AUTHORIZATION, "x-amz-security-token": SESSION_TOKEN },
    [NODE_HEADER_KEY]: RAW_REQUEST,
    socket,
  };
  const response = {
    statusCode: 403,
    reason: "Forbidden",
    headers: {
      "x-amzn-requestid": REQUEST_ID,
      "x-amz-id-2": EXTENDED_REQUEST_ID,
      "x-debug-echo-authorization": AUTHORIZATION,
    },
    body: {
      req: request,
      socket,
      rawHeaders: ["authorization", AUTHORIZATION],
      url: PRESIGNED_URL,
    },
  };
  const error = Object.assign(new Error(`${name} happened`), { name, ...extra });
  Object.assign(error, {
    [`${"$"}fault`]: "client",
    [`${"$"}metadata`]: {
      httpStatusCode: 403,
      requestId: REQUEST_ID,
      extendedRequestId: EXTENDED_REQUEST_ID,
      cfId: undefined,
      attempts: 2,
      totalRetryDelay: 150,
    },
    [`${"$"}response`]: response,
  });
  return error;
}

/** Lo que un usuario, un logger o vitest ven de un error: nada de esto puede llevar un secreto. */
function renderings(error: Error): string[] {
  const cause = (error as { cause?: unknown }).cause;
  const views = [
    inspect(error, { depth: 10 }),
    inspect(error, { depth: Number.POSITIVE_INFINITY, showHidden: true }),
    String(error),
    error.message,
    String(error.stack),
    String(cause),
    inspect(cause, { depth: 10 }),
  ];
  for (const value of [error, cause]) {
    try {
      views.push(JSON.stringify(value) ?? "");
    } catch {
      // Un grafo circular no se serializa: tampoco filtra nada.
    }
  }
  return views;
}

function expectNoSecrets(error: Error): void {
  for (const view of renderings(error)) {
    for (const secret of SECRETS) {
      expect(view).not.toContain(secret);
    }
  }
}

function causeMetadata(error: Error): Record<string, unknown> {
  const cause = (error as { cause?: unknown }).cause as Record<string, unknown> | undefined;
  return (cause?.[`${"$"}metadata`] ?? {}) as Record<string, unknown>;
}

// ------------------------------------------------------------ fake servers

type Handler = (request: http.IncomingMessage, response: http.ServerResponse) => void;

interface Server {
  readonly endpoint: string;
  readonly seen: Array<Readonly<Record<string, string | string[] | undefined>>>;
  close(): Promise<void>;
}

async function serve(handler: Handler): Promise<Server> {
  const seen: Array<Readonly<Record<string, string | string[] | undefined>>> = [];
  const server = http.createServer((request, response) => {
    seen.push({ ...request.headers });
    request.resume();
    request.on("end", () => handler(request, response));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return {
    endpoint: `http://127.0.0.1:${(server.address() as AddressInfo).port}`,
    seen,
    close: async () => {
      server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    },
  };
}

function jsonError(type: string, message: string): Handler {
  return (_request, response) => {
    response.writeHead(403, {
      "content-type": "application/json",
      "x-amzn-errortype": type,
      "x-amzn-requestid": REQUEST_ID,
    });
    response.end(JSON.stringify({ message }));
  };
}

const s3SignatureMismatch: Handler = (_request, response) => {
  response.writeHead(403, {
    "content-type": "application/xml",
    "x-amz-request-id": REQUEST_ID,
    "x-amz-id-2": EXTENDED_REQUEST_ID,
  });
  response.end(
    '<?xml version="1.0" encoding="UTF-8"?><Error><Code>SignatureDoesNotMatch</Code>' +
      "<Message>The request signature we calculated does not match the signature you " +
      `provided. Check your key and signing method.</Message><AWSAccessKeyId>${ACCESS_KEY_ID}` +
      `</AWSAccessKeyId><StringToSign>AWS4-HMAC-SHA256</StringToSign><CanonicalRequest>` +
      `POST\n/${BUCKET}/k\nuploads=\nx-amz-security-token:${SESSION_TOKEN}</CanonicalRequest>` +
      `<SignatureProvided>${"c".repeat(64)}</SignatureProvided><RequestId>${REQUEST_ID}` +
      `</RequestId><HostId>${EXTENDED_REQUEST_ID}</HostId></Error>`,
  );
};

// ------------------------------------------------------------------ tests

describe("control plane: translateAwsError never exposes the raw SDK error", () => {
  test("a fake smithy error keeps requestId and statusCode, not $response", () => {
    const translated = translateAwsError(
      fakeSdkError("ThrottlingException", { retryAfterSeconds: 3 }),
    ) as RateLimitError;
    expect(translated).toBeInstanceOf(RateLimitError);
    expect(translated.retryAfter).toBe(3);
    expect(translated.statusCode).toBe(403);
    expect(translated.awsCode).toBe("ThrottlingException");
    expectNoSecrets(translated);
    const cause = translated.cause as Record<string, unknown>;
    expect(cause.name).toBe("ThrottlingException");
    expect(cause[`${"$"}fault`]).toBe("client");
    expect(cause[`${"$"}response`]).toBeUndefined();
    expect(causeMetadata(translated)).toEqual({
      httpStatusCode: 403,
      requestId: REQUEST_ID,
      extendedRequestId: EXTENDED_REQUEST_ID,
      attempts: 2,
    });
  });

  test("every mapped name drops $response", () => {
    for (const name of [
      "ResourceNotFoundException",
      "ValidationException",
      "AccessDeniedException",
      "ConflictException",
      "InternalServerException",
      "ServiceQuotaExceededException",
      "InsufficientCapacityException",
    ]) {
      expectNoSecrets(translateAwsError(fakeSdkError(name)));
    }
    const denied = translateAwsError(fakeSdkError("AccessDeniedException"));
    expect(denied).toBeInstanceOf(AuthenticationError);
    expect(causeMetadata(denied).requestId).toBe(REQUEST_ID);
    const invalid = translateAwsError(fakeSdkError("ValidationException"));
    expect(invalid).toBeInstanceOf(InvalidArgumentError);
    expect((invalid as SandboxError).statusCode).toBe(403);
  });

  test("an InvalidSignatureException message loses the canonical request", () => {
    const translated = translateAwsError(
      fakeSdkError("InvalidSignatureException", { message: INVALID_SIGNATURE_MESSAGE }),
    ) as SandboxError;
    expect(translated).toBeInstanceOf(SandboxError);
    expect(translated.awsCode).toBe("InvalidSignatureException");
    expect(translated.message).toContain("does not match the signature you provided");
    expectNoSecrets(translated);
  });

  test("a real LambdaMicrovmsClient error over HTTP does not leak the signed request", async () => {
    const server = await serve(jsonError("InvalidSignatureException", INVALID_SIGNATURE_MESSAGE));
    try {
      const plane = new LambdaMicrovmsControlPlane({
        client: new LambdaMicrovmsClient({
          region: REGION,
          endpoint: server.endpoint,
          credentials: CREDENTIALS,
          maxAttempts: 1,
        }),
        region: REGION,
      });
      const failure = await plane.getMicrovm(SANDBOX_ID).then(
        () => undefined,
        (error: unknown) => error as Error,
      );
      // La petición sí llevaba los secretos: el test mira el sitio correcto.
      expect(String(server.seen[0]?.authorization)).toContain(ACCESS_KEY_ID);
      expect(server.seen[0]?.["x-amz-security-token"]).toBe(SESSION_TOKEN);
      expect(failure).toBeInstanceOf(SandboxError);
      expect((failure as SandboxError).awsCode).toBe("InvalidSignatureException");
      expect((failure as SandboxError).statusCode).toBe(403);
      expect(causeMetadata(failure as Error).requestId).toBe(REQUEST_ID);
      expectNoSecrets(failure as Error);
    } finally {
      await server.close();
    }
  });
});

describe("S3 transfers: translateS3Error never exposes the raw SDK error", () => {
  test("a fake S3 error keeps requestId and statusCode, not $response", () => {
    const translated = translateS3Error(
      fakeSdkError("SignatureDoesNotMatch", {
        Code: "SignatureDoesNotMatch",
        AWSAccessKeyId: ACCESS_KEY_ID,
        CanonicalRequest: `PUT\n/k\nx-amz-security-token:${SESSION_TOKEN}`,
        HostId: EXTENDED_REQUEST_ID,
      }),
    );
    expect(translated).toBeInstanceOf(AuthenticationError);
    expect((translated as AuthenticationError).awsCode).toBe("SignatureDoesNotMatch");
    expectNoSecrets(translated);
    expect(causeMetadata(translated)).toMatchObject({
      httpStatusCode: 403,
      requestId: REQUEST_ID,
      extendedRequestId: EXTENDED_REQUEST_ID,
    });
    for (const name of ["NoSuchBucket", "PermanentRedirect", "InternalError"]) {
      const other = translateS3Error(fakeSdkError(name));
      expectNoSecrets(other);
      expect(causeMetadata(other).requestId).toBe(REQUEST_ID);
    }
  });

  test("a real S3Client error over HTTP does not leak the signed request", async () => {
    const server = await serve(s3SignatureMismatch);
    try {
      const access = createS3Access(REGION, {
        endpoint: server.endpoint,
        forcePathStyle: true,
        credentials: CREDENTIALS,
        maxAttempts: 1,
      });
      const failure = await access.createMultipartUpload(BUCKET, "k").then(
        () => undefined,
        (error: unknown) => error as Error,
      );
      expect(String(server.seen[0]?.authorization)).toContain(ACCESS_KEY_ID);
      expect(server.seen[0]?.["x-amz-security-token"]).toBe(SESSION_TOKEN);
      expect(failure).toBeInstanceOf(AuthenticationError);
      expect((failure as AuthenticationError).awsCode).toBe("SignatureDoesNotMatch");
      expect(causeMetadata(failure as Error)).toMatchObject({
        httpStatusCode: 403,
        requestId: REQUEST_ID,
        extendedRequestId: EXTENDED_REQUEST_ID,
      });
      expectNoSecrets(failure as Error);
    } finally {
      await server.close();
    }
  });

  test("presigned URLs never reach an error", async () => {
    const access = createS3Access(REGION, { credentials: CREDENTIALS });
    const url = await access.presignGet(BUCKET, "k", 60);
    expect(url).toContain(SIGNATURE_PARAM);
    const translated = translateS3Error(
      Object.assign(new Error(`GET ${url} failed`), { name: "TimeoutError" }),
    );
    expectNoSecrets(translated);
  });
});

describe("sanitizeAwsError and redactAwsText", () => {
  test("only the allow-listed fields survive", () => {
    const summary = sanitizeAwsError(
      fakeSdkError("ThrottlingException", {
        Code: "Throttled",
        AWSAccessKeyId: ACCESS_KEY_ID,
        StringToSign: SESSION_TOKEN,
      }),
    );
    expect(summary).toBeInstanceOf(AwsErrorSummary);
    expect(summary).toBeInstanceOf(Error);
    expect(String(summary)).toBe("ThrottlingException: ThrottlingException happened");
    expect({ ...summary }).toEqual({
      name: "ThrottlingException",
      code: "Throttled",
      [`${"$"}fault`]: "client",
      [`${"$"}metadata`]: {
        httpStatusCode: 403,
        requestId: REQUEST_ID,
        extendedRequestId: EXTENDED_REQUEST_ID,
        attempts: 2,
      },
    });
    expect(JSON.parse(JSON.stringify(summary))).toEqual({ ...summary });
    expect(String(summary.stack)).toMatch(
      /^ThrottlingException: ThrottlingException happened\n\s+at /,
    );
  });

  test("includeMessage: false keeps only the name", () => {
    const summary = sanitizeAwsError(
      Object.assign(new Error(`connect ECONNREFUSED ${BUCKET}.s3.${REGION}.amazonaws.com`), {
        code: "ECONNREFUSED",
      }),
      { includeMessage: false },
    );
    expect(summary.message).toBe("");
    expect(summary.code).toBe("ECONNREFUSED");
    expect(inspect(summary)).not.toContain(BUCKET);
  });

  test("non-objects and hostile getters never throw", () => {
    expect(String(sanitizeAwsError(`token ${SESSION_TOKEN}?X-Amz-Signature=abc`))).not.toContain(
      "X-Amz-Signature",
    );
    expect(sanitizeAwsError(undefined).name).toBe("undefined");
    const hostile = Object.defineProperty(new Error("m"), `${"$"}metadata`, {
      get() {
        throw new Error("boom");
      },
    });
    expect(sanitizeAwsError(hostile)[`${"$"}metadata`]).toEqual({});
    expect(sanitizeAwsError({ [`${"$"}fault`]: "other" })[`${"$"}fault`]).toBeUndefined();
  });

  test("redactAwsText strips signatures, tokens, key ids and the canonical request", () => {
    const redacted = redactAwsText(
      `${INVALID_SIGNATURE_MESSAGE} ${RAW_REQUEST} ${PRESIGNED_URL} Signature=${"d".repeat(64)}`,
    );
    for (const secret of SECRETS) {
      expect(redacted).not.toContain(secret);
    }
    expect(redactAwsText(RAW_REQUEST)).toContain("authorization: <redacted>");
    expect(redactAwsText(PRESIGNED_URL)).toBe(
      `https://${BUCKET}.s3.${REGION}.amazonaws.com/k?<redacted>`,
    );
    expect(redactAwsText("Rate exceeded")).toBe("Rate exceeded");
    expect(redactAwsText("x".repeat(5000))).toHaveLength(2049);
  });
});
