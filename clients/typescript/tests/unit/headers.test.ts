/**
 * `transport.extraHeaders` (D6/D18): las reglas de validación de
 * `headers=`, los pares detrás de las cuatro cabeceras reservadas en
 * unarios, streams y `Health`, y el rechazo antes de tocar AWS.
 */

import { describe, expect, test } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import { Sandbox } from "../../src/index.js";
import { validateExtraHeaders } from "../../src/transport/headers.js";
import { FakeControlPlane, IMAGE_ARN } from "./fake/control-plane.js";
import { ACCESS_TOKEN, createTestSandbox, startRayd } from "./helpers.js";

describe("validateExtraHeaders", () => {
  test("keys are lower-cased and undefined passes", () => {
    expect(validateExtraHeaders(undefined)).toBeUndefined();
    expect(validateExtraHeaders({ "X-Trace": "1", "x-team": "a b" })).toEqual({
      "x-trace": "1",
      "x-team": "a b",
    });
  });

  test("reserved keys, reserved prefixes, -bin and non-token keys are refused by name", () => {
    const refused = [
      "x-aws-proxy-auth",
      "X-AWS-Proxy-Port",
      "x-aws-proxy-force-h2",
      "x-access-token",
      "rayito-compress",
      "user-agent",
      "content-type",
      "te",
      "host",
      "x-aws-proxy-anything",
      "grpc-timeout",
      ":authority",
      "trace-bin",
      "bad key",
      "clé",
    ];
    for (const key of refused) {
      expect(() => validateExtraHeaders({ [key]: "valor-secreto" })).toThrow(
        new InvalidArgumentError(`headers: la clave '${key.toLowerCase()}' está reservada`),
      );
    }
  });

  test("a value outside printable ASCII is refused without echoing it", () => {
    for (const value of ["línea", "a\nb", "\u0000"]) {
      let message = "";
      try {
        validateExtraHeaders({ "x-trace": value });
      } catch (error) {
        expect(error).toBeInstanceOf(InvalidArgumentError);
        message = (error as Error).message;
      }
      expect(message).toBe("headers: el valor de 'x-trace' no es ASCII imprimible");
    }
  });
});

describe("extraHeaders on the wire", () => {
  test("reach Health, a unary and a stream after the reserved headers", async () => {
    let transport: Record<string, unknown> = {};
    const { sandbox, rayd } = await createTestSandbox({
      beforeCreate: (fake) => {
        transport = { ...fake.transport, extraHeaders: { "X-Trace": "1" } };
      },
      create: {
        get transport() {
          return transport;
        },
      },
    });
    await sandbox.commands.list();
    const handle = await sandbox.commands.run("sleep 1", { background: true });
    await handle.kill();
    expect(rayd.health.healthCalls.at(-1)?.["x-trace"]).toBe("1");
    expect(rayd.process.listHeaders.at(-1)?.["x-trace"]).toBe("1");
    expect(rayd.process.startHeaders.at(-1)?.["x-trace"]).toBe("1");
    expect(rayd.process.startHeaders.at(-1)?.["x-aws-proxy-port"]).toBe("8080");
  });

  test("a reserved key rejects the create before any control-plane call", async () => {
    const rayd = await startRayd();
    try {
      const plane = new FakeControlPlane({ endpoint: rayd.host });
      for (const extraHeaders of [{ "x-access-token": "x" }, { "X-AWS-Proxy-Port": "1" }]) {
        await expect(
          Sandbox.create({
            template: IMAGE_ARN,
            accessToken: ACCESS_TOKEN,
            controlPlane: plane,
            transport: { ...rayd.transport, extraHeaders },
          }),
        ).rejects.toBeInstanceOf(InvalidArgumentError);
      }
      expect(plane.calls).toEqual([]);
      expect(plane.launches).toEqual([]);
    } finally {
      await rayd.close();
    }
  });
});
