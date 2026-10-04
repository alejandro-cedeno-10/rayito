/**
 * Los caminos que antes se saltaban `sanitizeAwsError` (el `cause` de
 * `StackError`, el adaptador de `Template.build` y su `submitBuild`): un
 * error de smithy con `$response` (la petición firmada) y la cadena canónica
 * en el `message` no deja el token ni el id de clave en `message`, `cause`
 * ni en `util.inspect`. Espejo de `test_aws_error_hygiene.py`.
 */

import { inspect } from "node:util";
import { LambdaMicrovmsClient } from "@aws-sdk/client-lambda-microvms";
import { STSClient } from "@aws-sdk/client-sts";
import { afterEach, describe, expect, test, vi } from "vitest";
import { BuildError, StackError } from "../../src/errors.js";
import { CloudFormationProvisioner } from "../../src/stacks/cloudformation.js";
import {
  _resetBuildClientsFactory,
  _setBuildClientsFactory,
  buildInBackground,
} from "../../src/templates/build.js";
import { Template } from "../../src/templates/dsl.js";
import { FakeBuildClients, makeBaseZip } from "./m15-fake-build-clients.js";

const SESSION_TOKEN = "IQoJb3JpZ2luX2VjEXAMPLESESSIONTOKENVALUE0123456789";
const ACCESS_KEY_ID = "ASIAEXAMPLEKEYID0123";
const AUTHORIZATION = `AWS4-HMAC-SHA256 Credential=${ACCESS_KEY_ID}/20261004/us-east-1/lambda/aws4_request`;
const INVALID_SIGNATURE_MESSAGE =
  "The request signature we calculated does not match the signature you provided." +
  "\n\nThe Canonical String for this request should have been\n" +
  `'POST\n/\n\nhost:example.com\nx-amz-security-token:${SESSION_TOKEN}\n'` +
  "\n\nThe String-to-Sign should have been\n'AWS4-HMAC-SHA256'\n";
const LEAKS = [SESSION_TOKEN, ACCESS_KEY_ID] as const;
const BASE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base";
const BUCKET = "amzn-s3-demo-bucket";

function smithyError(): Error {
  const error = new Error(INVALID_SIGNATURE_MESSAGE);
  error.name = "InvalidSignatureException";
  Object.assign(error, {
    [`${"$"}fault`]: "client",
    [`${"$"}metadata`]: { httpStatusCode: 403, requestId: "req-1" },
    [`${"$"}response`]: {
      statusCode: 403,
      headers: { authorization: AUTHORIZATION, "x-amz-security-token": SESSION_TOKEN },
    },
  });
  return error;
}

function expectClean(error: unknown): void {
  const shown = `${inspect(error, { depth: 10, showHidden: true })}\n${String(error)}`;
  for (const leak of LEAKS) {
    expect(shown).not.toContain(leak);
  }
  expect(shown).not.toContain("$response");
}

afterEach(() => {
  _resetBuildClientsFactory();
  vi.restoreAllMocks();
});

describe("AWS error hygiene", () => {
  test("a StackError carries the sanitized summary as its cause", async () => {
    const api = {
      describeStacks: async () => {
        throw smithyError();
      },
      createStack: async () => ({}),
      updateStack: async () => ({}),
      deleteStack: async () => ({}),
    };
    const adapter = new CloudFormationProvisioner({
      region: "us-east-1",
      cloudformationClient: api,
    });
    const error = await adapter
      .describe("rayito-metadata-index")
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(StackError);
    expect((error as Error).message).toContain("InvalidSignatureException");
    expectClean(error);
  });

  test("a rejected template build is BuildError(aws_error) with a sanitized cause", async () => {
    const clients = new FakeBuildClients();
    clients.images.set(BASE_ARN, { state: "CREATED" });
    clients.versions.set(`${BASE_ARN}#1`, {
      state: "SUCCESSFUL",
      status: "ACTIVE",
      imageVersion: "1",
      createdAt: new Date(1).toISOString(),
      baseImageArn: "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
      baseImageVersion: "1.0",
      buildRoleArn: "arn:aws:iam::123456789012:role/rayito-build",
      hooks: { port: 9000 },
      codeArtifact: { uri: `s3://${BUCKET}/base.zip` },
    });
    clients.objects.set(
      `${BUCKET}/base.zip`,
      makeBaseZip('FROM scratch\nCMD ["/usr/local/bin/rayd"]\n', { rayd: new Uint8Array([0x7f]) }),
    );
    clients.createMicrovmImage = async () => {
      throw smithyError();
    };
    _setBuildClientsFactory(() => clients);
    const error = await buildInBackground(new Template().fromBaseImage(), "mi-template", {
      bucket: BUCKET,
      contextDir: ".",
    }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(BuildError);
    expect((error as BuildError).reason).toBe("aws_error");
    expect((error as Error).message).toContain("InvalidSignatureException");
    expectClean(error);
  });

  test("the real build adapter never lets a raw SDK error out", async () => {
    vi.spyOn(LambdaMicrovmsClient.prototype, "send").mockRejectedValue(smithyError());
    vi.spyOn(STSClient.prototype, "send").mockRejectedValue(smithyError());
    const error = await buildInBackground(new Template().fromBaseImage(), "mi-template", {
      bucket: BUCKET,
      contextDir: ".",
      region: "us-east-1",
      credentials: { accessKeyId: "AKIDEXAMPLE", secretAccessKey: "secret" },
    }).catch((caught: unknown) => caught);
    expect((error as Error).name).toBe("InvalidSignatureException");
    expectClean(error);
  });
});
