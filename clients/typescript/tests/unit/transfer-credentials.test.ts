/**
 * Los clientes S3 de las transferencias heredan del plano de control sus
 * credenciales y su proxy (como el SDK Python firma con la sesión boto3 del
 * sandbox): sin esto usarían la cadena por defecto y rodearían el proxy.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { awsClientSettingsOf, LambdaMicrovmsControlPlane } from "../../src/aws/control-plane.js";
import { LaunchObserver } from "../../src/pool/pool.js";
import { s3ClientOverridesFor } from "../../src/sandbox/transfer.js";
import { ProxyTunnelAgent } from "../../src/transport/proxy-tunnel.js";
import { REGION } from "./fake/control-plane.js";
import { FakeS3 } from "./fake/s3.js";
import { createTestSandbox, Sandbox } from "./helpers.js";

const BUCKET = "amzn-s3-demo-bucket";
const PLANE_KEY_ID = "AKIAI44QH8DHBEXAMPLE";

function planeCredentials() {
  const provider = vi.fn(async () => ({
    accessKeyId: PLANE_KEY_ID,
    secretAccessKey: "je7MtGbClwBF/2Zp9Utk/h3yCo8nvbEXAMPLEKEY",
  }));
  return provider;
}

const fakes: FakeS3[] = [];

beforeEach(() => {
  vi.stubEnv("RAYITO_TRANSFER_BUCKET", "");
});

afterEach(async () => {
  vi.unstubAllEnvs();
  for (const fake of fakes.splice(0)) {
    await fake.close();
  }
});

describe("S3 client settings derived from the control plane", () => {
  test("a plane built with credentials and proxy hands both to the S3 clients", async () => {
    const credentials = planeCredentials();
    const plane = LambdaMicrovmsControlPlane.fromRegion(REGION, {
      credentials,
      proxy: "http://u:p@127.0.0.1:3128",
    });
    expect(awsClientSettingsOf(plane)).toEqual({
      credentials,
      proxy: "http://u:p@127.0.0.1:3128",
    });
    const overrides = s3ClientOverridesFor(plane);
    expect(overrides.credentials).toBe(credentials);
    const handlerConfig = await (
      overrides.requestHandler as unknown as { configProvider: Promise<{ httpsAgent: unknown }> }
    ).configProvider;
    expect(handlerConfig.httpsAgent).toBeInstanceOf(ProxyTunnelAgent);
  });

  test("a plain plane adds nothing, and the pool's observer forwards its plane's settings", () => {
    expect(s3ClientOverridesFor(LambdaMicrovmsControlPlane.fromRegion(REGION))).toEqual({});
    const credentials = planeCredentials();
    const inner = LambdaMicrovmsControlPlane.fromRegion(REGION, { credentials });
    const observed = new LaunchObserver(inner, async () => undefined);
    expect(s3ClientOverridesFor(observed).credentials).toBe(credentials);
  });

  test("uploadUrl is signed with the control plane's credentials, not the default chain", async () => {
    const s3 = await FakeS3.start();
    fakes.push(s3);
    const credentials = planeCredentials();
    const { sandbox } = await createTestSandbox({
      beforeCreate: (_rayd, plane) => {
        Object.defineProperty(plane, "awsClientSettings", { value: { credentials } });
      },
      create: { transfer: { bucket: BUCKET } },
    });
    Sandbox.coreOf(sandbox).s3ClientOverrides = { endpoint: s3.endpoint, forcePathStyle: true };
    const ticket = await sandbox.files.uploadUrl("up/data.bin");
    expect(credentials).toHaveBeenCalled();
    expect(decodeURIComponent(ticket.url)).toContain(`X-Amz-Credential=${PLANE_KEY_ID}/`);
  });
});
