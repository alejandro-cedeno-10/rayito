/**
 * Los vectores de `rayito sandbox proxy` que comparte con la CLI de Python
 * (`testdata/cli/proxy-vectors.json`, ADR-027, fase 4): contrabando de
 * cabeceras, delimitación del cuerpo, reescritura de la cabecera y lista de
 * Host/Origin. Cada función de TypeScript produce lo que Python produce.
 */

import { describe, expect, it } from "vitest";
import { isWildcard } from "../../../src/authority.js";
import {
  type AccessInput,
  buildAccess,
  isLoopbackBind,
  sandboxLocalhostName,
} from "../../../src/cli/access.js";
import {
  type BodyFraming,
  chunkSize,
  MalformedFramingError,
  requestBodyFraming,
  responseBodyFraming,
  responseStatus,
} from "../../../src/cli/http-framing.js";
import {
  closingResponseHead,
  MalformedHttpHeadError,
  parseHttpHead,
  rewriteHead,
} from "../../../src/cli/http-head.js";
import {
  validateLocalPort,
  validateMaxConnections,
  validateProxyPort,
} from "../../../src/cli/proxy-settings.js";
import { InvalidArgumentError } from "../../../src/errors.js";
import { loadCliTestdata } from "./paths.js";

interface Vectors {
  headAccepted: Array<{ raw: string; requestLine: string; headers: string[][] }>;
  headRejected: Array<{ name: string; raw: string }>;
  rewriteHead: Array<{
    name: string;
    raw: string;
    endpoint: string;
    jwe: string;
    port: number;
    expected: string;
  }>;
  closingResponseHead: Array<{ raw: string; expected: string }>;
  requestFraming: Array<{
    transferEncodings: string[];
    contentLengths: string[];
    expected: BodyFraming;
  }>;
  requestFramingRejected: Array<{ transferEncodings: string[]; contentLengths: string[] }>;
  responseFraming: Array<{
    status: number;
    method: string;
    transferEncodings: string[];
    contentLengths: string[];
    expected: BodyFraming;
  }>;
  responseStatus: Array<{ line: string; expected: number }>;
  responseStatusRejected: string[];
  chunkSize: Array<{ line: string; expected: number }>;
  chunkSizeRejected: string[];
  access: Array<{
    name: string;
    input: JsonAccessInput;
    hosts: string[];
    origins: string[];
    sandboxHost: string | null;
    checks: Array<{
      host: string | null;
      origin: string | null;
      hostAdmitted: boolean;
      originAdmitted: boolean;
    }>;
  }>;
  accessRejected: Array<{ option: string; input: JsonAccessInput }>;
  wildcard: Array<{ host: string; expected: boolean }>;
  loopbackBind: Array<{ bind: string; expected: boolean }>;
  sandboxLocalhostName: Array<{ id: string; expected: string | null }>;
  ports: Record<
    "proxyPort" | "localPort" | "maxConnections",
    { valid: number[]; invalid: number[] }
  >;
}

type JsonAccessInput = Omit<AccessInput, "sandboxId"> & { sandboxId: string | null };

const vectors = loadCliTestdata<Vectors>("proxy-vectors.json");

function absent<T>(value: T | null): T | undefined {
  return value ?? undefined;
}

function accessInput(input: JsonAccessInput): AccessInput {
  return { ...input, sandboxId: absent(input.sandboxId) };
}

describe("vectores de cabecera HTTP compartidos con Python", () => {
  it.each(vectors.headAccepted)("acepta $raw", (vector) => {
    const head = parseHttpHead(vector.raw);
    expect(head.requestLine).toBe(vector.requestLine);
    expect(head.headers.map(([name, value]) => [name, value])).toEqual(vector.headers);
  });

  it.each(vectors.headRejected)("rechaza: $name", (vector) => {
    expect(() => parseHttpHead(vector.raw)).toThrow(MalformedHttpHeadError);
  });

  it.each(vectors.rewriteHead)("reescribe: $name", (vector) => {
    const head = parseHttpHead(vector.raw);
    expect(
      rewriteHead(head, { endpoint: vector.endpoint, jwe: vector.jwe, port: vector.port }),
    ).toBe(vector.expected);
  });

  it.each(vectors.closingResponseHead)("cierra la respuesta $raw", (vector) => {
    expect(closingResponseHead(parseHttpHead(vector.raw))).toBe(vector.expected);
  });
});

describe("vectores de delimitación del cuerpo compartidos con Python", () => {
  it.each(vectors.requestFraming)("petición TE=$transferEncodings CL=$contentLengths", (vector) => {
    expect(requestBodyFraming(vector.transferEncodings, vector.contentLengths)).toEqual(
      vector.expected,
    );
  });

  it.each(vectors.requestFramingRejected)(
    "petición ambigua TE=$transferEncodings CL=$contentLengths",
    (vector) => {
      expect(() => requestBodyFraming(vector.transferEncodings, vector.contentLengths)).toThrow(
        MalformedFramingError,
      );
    },
  );

  it.each(vectors.responseFraming)("respuesta $status $method", (vector) => {
    expect(
      responseBodyFraming(vector.status, {
        requestMethod: vector.method,
        transferEncodings: vector.transferEncodings,
        contentLengths: vector.contentLengths,
      }),
    ).toEqual(vector.expected);
  });

  it.each(vectors.responseStatus)("estado $line", (vector) => {
    expect(responseStatus(vector.line)).toBe(vector.expected);
  });

  it.each(vectors.responseStatusRejected)("estado inválido %j", (line) => {
    expect(() => responseStatus(line)).toThrow(MalformedFramingError);
  });

  it.each(vectors.chunkSize)("trozo $line", (vector) => {
    expect(chunkSize(vector.line)).toBe(vector.expected);
  });

  it.each(vectors.chunkSizeRejected)("trozo inválido %j", (line) => {
    expect(() => chunkSize(line)).toThrow(MalformedFramingError);
  });
});

describe("vectores de Host y Origin compartidos con Python", () => {
  it.each(vectors.access)("$name", (vector) => {
    const access = buildAccess(accessInput(vector.input));
    expect([...access.hosts].sort()).toEqual(vector.hosts);
    expect([...access.origins].sort()).toEqual(vector.origins);
    expect(access.sandboxHost).toBe(absent(vector.sandboxHost));
    for (const check of vector.checks) {
      expect(access.admitsHost(absent(check.host))).toBe(check.hostAdmitted);
      expect(access.admitsOrigin(absent(check.origin))).toBe(check.originAdmitted);
    }
  });

  it.each(vectors.accessRejected)(
    "rechaza $option $input.allowedHosts $input.allowedOrigins",
    (vector) => {
      expect(() => buildAccess(accessInput(vector.input))).toThrow(InvalidArgumentError);
    },
  );

  it.each(vectors.wildcard)("comodín $host", (vector) => {
    expect(isWildcard(vector.host)).toBe(vector.expected);
  });

  it.each(vectors.loopbackBind)("loopback $bind", (vector) => {
    expect(isLoopbackBind(vector.bind)).toBe(vector.expected);
  });

  it.each(vectors.sandboxLocalhostName)("nombre .localhost de $id", (vector) => {
    expect(sandboxLocalhostName(vector.id)).toBe(absent(vector.expected));
  });
});

describe("vectores de validación de puertos compartidos con Python", () => {
  const validators = {
    proxyPort: validateProxyPort,
    localPort: validateLocalPort,
    maxConnections: validateMaxConnections,
  } as const;

  it.each(Object.entries(validators))(
    "%s acepta sus valores válidos y rechaza los inválidos",
    (key, validate) => {
      const cases = vectors.ports[key as keyof Vectors["ports"]];
      for (const value of cases.valid) {
        expect(validate(value)).toBe(value);
      }
      for (const value of cases.invalid) {
        expect(() => validate(value)).toThrow(InvalidArgumentError);
      }
    },
  );
});
