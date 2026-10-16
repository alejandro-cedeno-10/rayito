/** Las dos piezas del adaptador de `doctor` que no necesitan AWS: la traducción de errores y el plano con el JWE ya acuñado. */

import { describe, expect, it } from "vitest";
import { type ControlPlane, PortSpec } from "../../../src/aws/control-plane.js";
import { operationCall, withPreminted } from "../../../src/cli/doctor/aws.js";
import { AwsOperationError, MissingPeerError } from "../../../src/cli/doctor/ports.js";
import { InvalidArgumentError } from "../../../src/errors.js";
import { CasePlane } from "./case-world.js";
import { JWE } from "./doctor-fakes.js";

class CountingPlane extends CasePlane {
  minted = 0;

  override async createAuthToken(): Promise<string> {
    this.minted += 1;
    return `real-${this.minted}`;
  }
}

function awsError(name: string, message: string): Error {
  const error = Object.assign(new Error(message), { name });
  Object.defineProperty(error, "$metadata", { value: { httpStatusCode: 403 } });
  return error;
}

describe("operationCall", () => {
  it("traduce un error del SDK a AwsOperationError con el código y la operación", async () => {
    const failure = await operationCall("HeadBucket", async () => {
      throw awsError("Forbidden", "no tienes acceso");
    }).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(AwsOperationError);
    expect(failure).toMatchObject({ operation: "HeadBucket", code: "Forbidden" });
  });

  it("redacta el token de sesión del mensaje", async () => {
    const failure = await operationCall("GetCallerIdentity", async () => {
      throw awsError(
        "InvalidSignatureException",
        "firma inválida. The Canonical String for this request should have been 'x-amz-security-token:FwoGZXIvYXdz-secreto'",
      );
    }).catch((error: unknown) => error as AwsOperationError);
    expect(String((failure as Error).message)).not.toContain("FwoGZXIvYXdz-secreto");
  });

  it("un peer ausente es MissingPeerError, no un fallo de AWS", async () => {
    const failure = await operationCall("GetRole", async () => {
      throw new InvalidArgumentError("necesita '@aws-sdk/client-iam'", { cause: new Error("x") });
    }).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(MissingPeerError);
  });

  it("un argumento inválido propio sale tal cual", async () => {
    const failure = await operationCall("DescribeVpcs", async () => {
      throw new InvalidArgumentError("vpc inválida");
    }).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(InvalidArgumentError);
  });
});

describe("withPreminted", () => {
  it("la primera acuñación devuelve el JWE ya acuñado y las siguientes van al plano real", async () => {
    const real = new CountingPlane({});
    const plane: ControlPlane = withPreminted(real, JWE);
    const ports = [PortSpec.single(8080)];
    expect(await plane.createAuthToken("microvm-a", ports)).toBe(JWE);
    expect(real.minted).toBe(0);
    expect(await plane.createAuthToken("microvm-a", ports)).toBe("real-1");
    expect(real.minted).toBe(1);
  });

  it("sin token previo todo va al plano real y el resto de métodos se delegan", async () => {
    const real = new CountingPlane({});
    const plane = withPreminted(real, undefined);
    expect(await plane.createAuthToken("microvm-a", [PortSpec.single(8080)])).toBe("real-1");
    expect(plane.region).toBe(real.region);
    expect(await plane.resolveTemplateArn("rayito-base")).toBe(
      await real.resolveTemplateArn("rayito-base"),
    );
  });
});
