/**
 * `RayitoMicrovmImage` y `MicrovmImageArtifact` sin AWS (ADR-032 D9 y
 * D12.1): las propiedades de cada imagen son la configuración de `rayito
 * image publish` con los nombres del recurso, el rol de build sólo lee el
 * asset y escribe sus grupos de logs, los tamaños se llaman
 * `<nombre>-<tamaño>`, las validaciones fallan antes de añadir nada y
 * `downloadRelease` reutiliza la release verificada sin red.
 */

import { createHash } from "node:crypto";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { App, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { afterEach, describe, expect, it } from "vitest";
import {
  AccountSetupError,
  CHECKSUMS_ASSET_NAME,
  type DownloadedRelease,
  IMAGE_ASSET_NAME,
} from "../../src/account-setup/domain.js";
import type { ReleaseArtifactSource } from "../../src/account-setup/ports.js";
import {
  type DownloadReleaseOptions,
  replaceReleaseSourceFactory,
} from "../../src/cdk/artifact.js";
import { cfnImageProperties } from "../../src/cdk/image-properties.js";
import {
  MicrovmImageArtifact,
  RayitoCdkError,
  RayitoMicrovmImage,
  type RayitoMicrovmImageProps,
} from "../../src/cdk/index.js";
import { imageEnvironment } from "../../src/cdk/microvm-image.js";
import { desiredConfiguration, IMAGE_HOOKS } from "../../src/images/publish-domain.js";
import { CDK_MANAGED_BY_TAG_VALUE } from "../../src/limits.js";
import { CDK_TESTDATA, loadContract, writeFixtureZip } from "./cdk-contract-harness.js";

const ACCOUNT = "123456789012";
const REGION = "us-east-1";
const scratches: string[] = [];

afterEach(() => {
  for (const directory of scratches.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function scratch(): string {
  const directory = mkdtempSync(join(tmpdir(), "rayito-cdk-image-"));
  scratches.push(directory);
  return directory;
}

function fixture(name = "image-full"): MicrovmImageArtifact {
  const zip = writeFixtureZip(join(CDK_TESTDATA, "fixtures", name), join(scratch(), "a.zip"));
  return MicrovmImageArtifact.fromFile(zip);
}

function newStack(withEnvironment = true): Stack {
  const app = new App({
    context: JSON.parse(loadContract().caseFormat.appContext) as Record<string, unknown>,
  });
  return new Stack(
    app,
    "ImageTest",
    withEnvironment ? { env: { account: ACCOUNT, region: REGION } } : {},
  );
}

/** La fixture `full` con el marcador de EFS de `rayito image zip --with-efs`. */
function efsArtifact(): MicrovmImageArtifact {
  const tree = scratch();
  cpSync(join(CDK_TESTDATA, "fixtures", "image-full"), tree, { recursive: true });
  mkdirSync(join(tree, "kernel-sidecar"), { recursive: true });
  writeFileSync(join(tree, "kernel-sidecar", "efs_variant"), "efs\n");
  return MicrovmImageArtifact.fromFile(writeFixtureZip(tree, join(scratch(), "efs.zip")));
}

function build(props: Partial<RayitoMicrovmImageProps> = {}, stack = newStack()) {
  const image = new RayitoMicrovmImage(stack, "Image", {
    name: "team-sandbox",
    artifact: props.artifact ?? fixture(),
    baseImageVersion: "1",
    ...props,
  });
  return { stack, image, template: Template.fromStack(stack) };
}

type Resource = { Type: string; Properties: Record<string, unknown>; DependsOn?: string[] };

function images(template: Template): Resource[] {
  return Object.values(template.findResources("AWS::Lambda::MicrovmImage")) as Resource[];
}

describe("RayitoMicrovmImage: lo que sintetiza", () => {
  it("una imagen con las propiedades de rayito image publish y la marca +cdk", () => {
    const { template } = build();
    const [image] = images(template);
    expect(image?.Properties).toMatchObject({
      Name: "team-sandbox",
      Description: "Rayito image team-sandbox (rayito.microvm-image/1+cdk)",
      BaseImageArn: `arn:aws:lambda:${REGION}:aws:microvm-image:al2023-1`,
      BaseImageVersion: "1",
      Resources: [{ MinimumMemoryInMiB: 2048 }],
      CpuConfigurations: [{ Architecture: "ARM_64" }],
      Logging: { CloudWatch: { LogGroup: "/rayito/team-sandbox" } },
      AdditionalOsCapabilities: [],
      EgressNetworkConnectors: [],
      EnvironmentVariables: [],
    });
    expect(image?.Properties.Hooks).toMatchObject({ Port: 9000 });
    template.resourceCountIs("AWS::Logs::LogGroup", 1);
    template.resourceCountIs("AWS::IAM::Role", 1);
  });

  it("cada tamaño es otra imagen <nombre>-<tamaño> con su baseline y su grupo", () => {
    const { template, image } = build({
      osCapabilities: true,
      sizes: ["512mb", "4gb"],
      allowedMountBuckets: ["amzn-s3-demo-bucket"],
      environment: { TEAM: "demo" },
    });
    const byName = new Map(images(template).map((item) => [item.Properties.Name, item]));
    expect([...byName.keys()].sort()).toEqual([
      "team-sandbox",
      "team-sandbox-4gb",
      "team-sandbox-512mb",
    ]);
    expect(byName.get("team-sandbox-512mb")?.Properties).toMatchObject({
      Resources: [{ MinimumMemoryInMiB: 512 }],
      AdditionalOsCapabilities: ["ALL"],
      Logging: { CloudWatch: { LogGroup: "/rayito/team-sandbox-512mb" } },
      EnvironmentVariables: [
        { Key: "RAYITO_ALLOWED_MOUNT_BUCKETS", Value: "amzn-s3-demo-bucket" },
        { Key: "RAYITO_BASELINE_MEMORY_MIB", Value: "512" },
        { Key: "TEAM", Value: "demo" },
      ],
    });
    expect(Object.keys(image.sizeImageArns).sort()).toEqual(["4gb", "512mb"]);
    template.resourceCountIs("AWS::Logs::LogGroup", 3);
  });

  it("las propiedades son desiredConfiguration con los nombres del recurso", () => {
    const configuration = desiredConfiguration({
      region: REGION,
      settings: {
        imageName: "x",
        baseImageVersion: "1",
        memoryMib: 2048,
        osCapabilities: "ALL",
        environmentVariables: { B: "2", A: "1" },
      },
      artifactUri: "s3://amzn-s3-demo-bucket/k.zip",
      buildRoleArn: `arn:aws:iam::${ACCOUNT}:role/build`,
    });
    expect(cfnImageProperties("x", configuration)).toEqual({
      name: "x",
      description: "Rayito image x (rayito.microvm-image/1+cdk)",
      baseImageArn: configuration.baseImageArn,
      baseImageVersion: "1",
      buildRoleArn: `arn:aws:iam::${ACCOUNT}:role/build`,
      codeArtifact: { uri: "s3://amzn-s3-demo-bucket/k.zip" },
      resources: [{ minimumMemoryInMiB: 2048 }],
      cpuConfigurations: [{ architecture: "ARM_64" }],
      hooks: IMAGE_HOOKS,
      logging: { cloudWatch: { logGroup: "/rayito/x" } },
      additionalOsCapabilities: ["ALL"],
      egressNetworkConnectors: [],
      environmentVariables: [
        { key: "A", value: "1" },
        { key: "B", value: "2" },
      ],
    });
  });

  it("el rol de build sólo lee el asset y escribe los grupos de sus imágenes", () => {
    const { template } = build({ sizes: ["4gb"] });
    const role = Object.values(template.findResources("AWS::IAM::Role"))[0] as Resource;
    expect(JSON.stringify(role.Properties.AssumeRolePolicyDocument)).toContain(
      "lambda.amazonaws.com",
    );
    expect(role.Properties.AssumeRolePolicyDocument).toMatchObject({
      Statement: [
        {
          Action: ["sts:AssumeRole", "sts:TagSession"],
          Condition: { StringEquals: { "aws:SourceAccount": ACCOUNT } },
        },
      ],
    });
    const [policy] = role.Properties.Policies as Array<{
      PolicyName: string;
      PolicyDocument: { Statement: Array<{ Action: unknown; Resource: unknown }> };
    }>;
    expect(policy?.PolicyName).toBe("build");
    const [read, logs] = policy?.PolicyDocument.Statement ?? [];
    expect(read?.Action).toBe("s3:GetObject");
    expect(JSON.stringify(read?.Resource)).toMatch(/\.zip"/);
    expect(logs?.Action).toEqual([
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]);
    expect(logs?.Resource).toHaveLength(2);
    template.resourceCountIs("AWS::IAM::Policy", 0);
  });

  it("cada imagen depende de su grupo de logs y todo lleva las etiquetas", () => {
    const { template } = build({ tags: { team: "demo" } });
    const [image] = images(template);
    const groupId = Object.keys(template.findResources("AWS::Logs::LogGroup"))[0];
    expect(image?.DependsOn).toContain(groupId);
    for (const type of ["AWS::Lambda::MicrovmImage", "AWS::IAM::Role", "AWS::Logs::LogGroup"]) {
      for (const resource of Object.values(template.findResources(type))) {
        const tags = Object.fromEntries(
          (resource.Properties.Tags as Array<{ Key: string; Value: string }>).map((tag) => [
            tag.Key,
            tag.Value,
          ]),
        );
        expect(tags, type).toMatchObject({
          "rayito:component": "microvm-image",
          "rayito:managed-by": CDK_MANAGED_BY_TAG_VALUE,
          team: "demo",
        });
      }
    }
  });
});

describe("RayitoMicrovmImage: validaciones", () => {
  function refuses(props: Partial<RayitoMicrovmImageProps>, code: string, stack = newStack()) {
    expect(() => build(props, stack)).toThrow(expect.objectContaining({ code }));
    expect(stack.node.tryFindChild("Image")).toBeUndefined();
  }

  it("región token, nombres reservados o inválidos", () => {
    refuses({}, "region_unresolved", newStack(false));
    refuses({ name: "rayito-base-caps" }, "image_name_reserved");
    refuses({ name: "bad name" }, "image_name_invalid");
    refuses({ name: "x".repeat(60), sizes: ["512mb"] }, "image_name_invalid");
  });

  it("opciones inválidas son option_invalid", () => {
    refuses({ baseImageVersion: "latest" }, "option_invalid");
    refuses({ variant: "tiny" as never }, "option_invalid");
    refuses({ withEfs: true }, "option_invalid");
    refuses({ sizes: ["2gb"] }, "option_invalid");
    refuses({ sizes: ["4gb"], memoryMib: 1024 }, "option_invalid");
    refuses({ environment: { RAYITO_BASELINE_MEMORY_MIB: "1" } }, "option_invalid");
    refuses({ environment: { "1BAD": "x" } }, "option_invalid");
    refuses({ allowedMountBuckets: ["Bad_Bucket"] }, "option_invalid");
    refuses({ tags: { "bad*key": "x" } }, "option_invalid");
  });

  it("el artefacto debe casar con la variante y con withEfs", () => {
    refuses({ artifact: fixture("image-slim") }, "artifact_variant_mismatch");
    refuses({ artifact: efsArtifact() }, "artifact_efs_mismatch");
    refuses({ withEfs: true, osCapabilities: true }, "artifact_efs_mismatch");
    expect(
      build({ artifact: efsArtifact(), withEfs: true, osCapabilities: true }).image.imageName,
    ).toBe("team-sandbox");
  });

  it("imageEnvironment quita buckets repetidos y limita a 49 variables", () => {
    expect(imageEnvironment({}, ["amzn-s3-demo-bucket", "amzn-s3-demo-bucket"])).toEqual({
      RAYITO_ALLOWED_MOUNT_BUCKETS: "amzn-s3-demo-bucket",
    });
    const many = Object.fromEntries(Array.from({ length: 50 }, (_, index) => [`V${index}`, "x"]));
    expect(() => imageEnvironment(many, [])).toThrow(RayitoCdkError);
  });
});

describe("MicrovmImageArtifact", () => {
  it("fromFile comprueba que existe y su sha256", () => {
    const artifact = fixture();
    expect(artifact.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(() => MicrovmImageArtifact.fromFile(join(scratch(), "missing.zip"))).toThrow(
      expect.objectContaining({ code: "artifact_not_found" }),
    );
    expect(() => MicrovmImageArtifact.fromFile(artifact.path, { sha256: "0".repeat(64) })).toThrow(
      expect.objectContaining({ code: "artifact_checksum_mismatch" }),
    );
    expect(
      MicrovmImageArtifact.fromFile(artifact.path, { sha256: artifact.sha256.toUpperCase() })
        .sha256,
    ).toBe(artifact.sha256);
  });

  class FakeRelease implements ReleaseArtifactSource {
    calls = 0;
    constructor(
      private readonly content: Uint8Array,
      private readonly listed: string | undefined,
      private readonly failure?: AccountSetupError,
    ) {}

    async fetch(_tag: string, directory: string): Promise<DownloadedRelease> {
      this.calls += 1;
      if (this.failure !== undefined) {
        throw this.failure;
      }
      const artifact = join(directory, IMAGE_ASSET_NAME);
      writeFileSync(artifact, this.content);
      const digest = createHash("sha256").update(this.content).digest("hex");
      return {
        checksumsText: `${this.listed ?? digest}  ${IMAGE_ASSET_NAME}\n`,
        artifact,
        artifactSha256: digest,
      };
    }
  }

  /** `downloadRelease` con `source` como fuente de la release, sin red. */
  async function downloadWith(
    source: ReleaseArtifactSource,
    options: DownloadReleaseOptions,
  ): Promise<MicrovmImageArtifact> {
    const previous = replaceReleaseSourceFactory(() => source);
    try {
      return await MicrovmImageArtifact.downloadRelease(options);
    } finally {
      replaceReleaseSourceFactory(previous);
    }
  }

  it("downloadRelease verifica y la segunda vez no descarga nada", async () => {
    const directory = scratch();
    const source = new FakeRelease(readFileSync(fixture().path), undefined);
    const first = await downloadWith(source, { version: "0.15.0", directory });
    expect(existsSync(join(directory, CHECKSUMS_ASSET_NAME))).toBe(true);
    const second = await downloadWith(source, { version: "0.15.0", directory });
    expect(source.calls).toBe(1);
    expect(second.sha256).toBe(first.sha256);
    writeFileSync(join(directory, IMAGE_ASSET_NAME), "tocado");
    await expect(downloadWith(source, { version: "0.15.0", directory })).rejects.toMatchObject({
      code: "artifact_checksum_mismatch",
    });
  });

  it("un sha256 que no casa borra el zip y no guarda SHA256SUMS", async () => {
    const directory = scratch();
    const source = new FakeRelease(new Uint8Array([1, 2, 3]), "f".repeat(64));
    await expect(downloadWith(source, { directory })).rejects.toMatchObject({
      code: "artifact_checksum_mismatch",
    });
    expect(existsSync(join(directory, IMAGE_ASSET_NAME))).toBe(false);
    expect(existsSync(join(directory, CHECKSUMS_ASSET_NAME))).toBe(false);
  });

  it("un fallo de descarga es release_download_failed con el motivo", async () => {
    const failure = new AccountSetupError("release_download", {
      asset: IMAGE_ASSET_NAME,
      release: "rayd-v0.15.0",
      reason: "HTTP 404",
    });
    await expect(
      downloadWith(new FakeRelease(new Uint8Array(), undefined, failure), {
        version: "0.15.0",
        directory: scratch(),
      }),
    ).rejects.toMatchObject({
      code: "release_download_failed",
      fields: { asset: IMAGE_ASSET_NAME, release: "rayd-v0.15.0", reason: "HTTP 404" },
    });
  });
});
