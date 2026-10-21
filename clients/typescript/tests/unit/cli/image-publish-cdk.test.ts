/**
 * `rayito image publish` no cambia una imagen que gestiona CloudFormation
 * (`RayitoMicrovmImage`, ADR-032 D6): lee `rayito:managed-by=rayito-cdk` de
 * las etiquetas del `GetMicrovmImage` que ya hace y falla con el mensaje
 * del contrato antes de cualquier escritura (ni `putObject` del artefacto
 * ni `UpdateMicrovmImage`), también con `--force`. Una imagen sin esa etiqueta se publica como siempre.
 */

import { afterEach, describe, expect, it } from "vitest";
import { fileArtifacts } from "../../../src/images/artifact-node.js";
import { DEFAULT_MEMORY_MIB } from "../../../src/images/gateway.js";
import { isCdkManagedImage, type PublishSettings } from "../../../src/images/publish-domain.js";
import {
  ImagePublishError,
  type PublishDeps,
  publish,
} from "../../../src/images/publish-service.js";
import { BUCKET, BUILD_ROLE, FakePublishPorts, ImageWorkspace, imageArnOf } from "./image-fakes.js";

const NAME = "team-sandbox";
const workspaces: ImageWorkspace[] = [];

afterEach(() => {
  for (const workspace of workspaces.splice(0)) {
    workspace.dispose();
  }
});

class TaggedPorts extends FakePublishPorts {
  constructor(private readonly tags: Record<string, string>) {
    super();
  }

  override async getMicrovmImage(
    arn: string,
  ): Promise<{ state: string; tags: Record<string, string> } | undefined> {
    const image = await super.getMicrovmImage(arn);
    return image === undefined ? undefined : { ...image, tags: this.tags };
  }
}

function setup(tags: Record<string, string>, force = false) {
  const workspace = new ImageWorkspace();
  workspaces.push(workspace);
  const ports = new TaggedPorts(tags);
  ports.images.set(imageArnOf(NAME), { state: "CREATED", versions: [] });
  const settings: PublishSettings = {
    artifact: workspace.zip(),
    imageName: NAME,
    variant: "full",
    bucket: BUCKET,
    stackName: "stack",
    buildRoleArn: BUILD_ROLE,
    baseImageVersion: "1",
    memoryMib: DEFAULT_MEMORY_MIB,
    force,
    timeoutMs: 600_000,
    osCapabilities: undefined,
    environmentVariables: {},
    withEfs: false,
    artifactRunId: undefined,
  };
  let clock = 0;
  const deps: PublishDeps = {
    ports,
    artifacts: fileArtifacts,
    sleep: async (ms) => {
      clock += ms;
    },
    now: () => clock,
    emit: () => undefined,
    utcNow: () => new Date("2026-10-20T00:00:00Z"),
  };
  return { ports, settings, deps };
}

describe("publish sobre una imagen de CDK", () => {
  for (const force of [false, true]) {
    it(`falla sin ninguna escritura (force=${force})`, async () => {
      const { ports, settings, deps } = setup({ "rayito:managed-by": "rayito-cdk" }, force);
      const failure = await publish(deps, settings).catch((thrown: unknown) => thrown);
      expect(failure).toBeInstanceOf(ImagePublishError);
      expect((failure as Error).message).toBe(
        `la imagen ${NAME} la gestiona CloudFormation (CDK): cámbiala con cdk deploy`,
      );
      expect(ports.calls).not.toContain("updateMicrovmImage");
      expect(ports.calls).not.toContain("createMicrovmImage");
      expect(ports.calls.some((call) => call.startsWith("headObject:"))).toBe(false);
      expect(ports.calls.some((call) => call.startsWith("putObject:"))).toBe(false);
    });
  }

  it("una imagen de la CLI se sigue publicando", async () => {
    const { ports, settings, deps } = setup({ "rayito:managed-by": "rayito-sdk" });
    await publish(deps, settings);
    expect(ports.calls).toContain("updateMicrovmImage");
  });

  it("isCdkManagedImage sólo mira esa etiqueta", () => {
    expect(isCdkManagedImage(undefined)).toBe(false);
    expect(isCdkManagedImage({ tags: {} })).toBe(false);
    expect(isCdkManagedImage({ tags: { "rayito:managed-by": "rayito-cdk" } })).toBe(true);
  });
});
