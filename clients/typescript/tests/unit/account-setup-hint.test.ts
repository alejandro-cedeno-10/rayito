/**
 * La pista de `rayito setup` en los errores de una cuenta sin preparar:
 * `RunMicrovm` con `ResourceNotFoundException` (la misma clase, código y causa),
 * y los errores de `resolveBucket` y `ensureBaseImage`. Espejo de
 * `test_account_setup_hint.py`.
 */

import { describe, expect, test } from "vitest";
import { ACCOUNT_SETUP_HINT, CAPS_IMAGE_SETUP_HINT } from "../../src/account-setup/domain.js";
import { ensureBaseImage, resolveBucket } from "../../src/agent/aws-resolve.js";
import { type CommandSender, LambdaMicrovmsControlPlane } from "../../src/aws/control-plane.js";
import { InvalidArgumentError, SandboxNotFoundError } from "../../src/errors.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import { IMAGE_ARN, REGION } from "./fake/control-plane.js";
import { ACCESS_TOKEN } from "./helpers.js";

const HTTP_NOT_FOUND = 404;

/** `$metadata` es el nombre real del campo del SDK; se construye aparte para no escribirlo como clave literal. */
function withMetadata(httpStatusCode: number): Record<string, unknown> {
  return { [`${"$"}metadata`]: { httpStatusCode } };
}

function rejecting(name: string, extra: Record<string, unknown> = {}): CommandSender {
  return {
    send: async () => {
      throw Object.assign(new Error(`${name} happened`), { name, ...extra });
    },
  };
}

function planeFor(client: CommandSender): LambdaMicrovmsControlPlane {
  return new LambdaMicrovmsControlPlane({ client, region: REGION });
}

describe("Sandbox.create on an account without the image", () => {
  test("keeps the class, the codes and the cause, and points to rayito setup", async () => {
    const control = planeFor(rejecting("ResourceNotFoundException", withMetadata(HTTP_NOT_FOUND)));
    const failure = await Sandbox.create({
      template: IMAGE_ARN,
      accessToken: ACCESS_TOKEN,
      controlPlane: control,
    }).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(SandboxNotFoundError);
    const error = failure as SandboxNotFoundError;
    expect(error.awsCode).toBe("ResourceNotFoundException");
    expect(error.statusCode).toBe(HTTP_NOT_FOUND);
    expect(error.message).toContain("rayito setup");
    expect(error.message.endsWith(`(${ACCOUNT_SETUP_HINT})`)).toBe(true);
    expect(error.cause).toBeDefined();
  });

  test("other errors of RunMicrovm are unchanged", async () => {
    const control = planeFor(rejecting("AccessDeniedException"));
    const failure = await Sandbox.create({
      template: IMAGE_ARN,
      accessToken: ACCESS_TOKEN,
      controlPlane: control,
    }).catch((error: unknown) => error);
    expect((failure as Error).message).not.toContain("rayito setup");
  });
});

describe("deployAgent errors", () => {
  test("no bucket names Rayito does not create buckets and rayito setup", async () => {
    const failure = await resolveBucket(undefined, undefined, {}, async () => undefined).catch(
      (error: unknown) => error,
    );
    expect(failure).toBeInstanceOf(InvalidArgumentError);
    expect((failure as Error).message).toContain("Rayito no crea buckets");
    expect((failure as Error).message).toContain("rayito setup");
  });

  const noImageAws = {
    resolvedRegion: () => REGION,
    accountId: async () => "123456789012",
    bucketExists: async () => true,
    artifactStackBucket: async () => undefined,
    imageExists: async () => false,
  };

  test("a missing caps base image points to rayito setup --caps-image", async () => {
    const failure = await ensureBaseImage(noImageAws, "rayito-base-caps", undefined, {
      ensure: true,
    }).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(InvalidArgumentError);
    expect((failure as Error).message).toContain("no existe en esta cuenta y región");
    expect((failure as Error).message).toContain(CAPS_IMAGE_SETUP_HINT);
  });

  test("any other missing base image names rayito setup", async () => {
    const aws = {
      resolvedRegion: () => REGION,
      accountId: async () => "123456789012",
      bucketExists: async () => true,
      artifactStackBucket: async () => undefined,
      imageExists: async () => false,
    };
    const failure = await ensureBaseImage(aws, "my-base", undefined, {
      ensure: true,
    }).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(InvalidArgumentError);
    expect((failure as Error).message).toContain("no existe en esta cuenta y región");
    expect((failure as Error).message).toContain("rayito setup");
  });
});
