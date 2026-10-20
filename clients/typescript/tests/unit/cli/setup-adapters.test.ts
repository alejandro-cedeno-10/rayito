/**
 * Los adaptadores de `rayito setup` sobre los dobles de pilas, del doctor y de
 * las imágenes: la pila de IAM por `OptionalStacks`, el sondeo del bucket, la
 * observación y la espera de `rayito-base`, y la publicación por la tubería de
 * `rayito image publish`. Espejo de `test_account_setup_adapters.py`.
 */

import { afterEach, describe, expect, it } from "vitest";
import { ARTIFACT_STACK_COMPONENT } from "../../../src/account-setup/artifact-stack.js";
import {
  AccountSetupError,
  ARTIFACT_STACK_NAME,
  IAM_STACK_NAME,
} from "../../../src/account-setup/domain.js";
import { IAM_STACK_COMPONENT } from "../../../src/account-setup/iam-stack.js";
import { AwsOperationError } from "../../../src/cli/doctor/ports.js";
import {
  PipelineImagePublisher,
  SdkArtifactBuckets,
  SdkBaseImages,
  type SetupClock,
  sdkAccountSetupGateways,
  sdkArtifactStack,
  sdkIamStack,
} from "../../../src/cli/setup-adapters.js";
import { StackError } from "../../../src/errors.js";
import { loadTemplate } from "../../../src/stacks/packaging.js";
import { COMPONENTS } from "../../../src/stacks/registry.js";
import { FakeStackProvisioner } from "../m15-fake-stacks.js";
import { componentParameterNames, templateParameterNames } from "../m15-template-parameters.js";
import { FakeDoctorAws } from "./doctor-fakes.js";
import { BUCKET, BUILD_ROLE, FakePublishPorts, IMAGE_ARN, ImageWorkspace } from "./image-fakes.js";

const BUCKET_B = "amzn-s3-demo-bucket1";
const OTHER_PARAMETERS = [
  "LogGroupPrefix",
  "NetworkConnectorArns",
  "PersistenceBucket",
  "PersistencePrefix",
  "TransferBucket",
  "TransferPrefix",
];
const BUILD_STEP_MS = 10;

const clock: SetupClock = (() => {
  let now = 0;
  return {
    sleep: async () => undefined,
    now: () => {
      now += BUILD_STEP_MS;
      return now;
    },
    utcNow: () => new Date("2026-10-01T00:00:00Z"),
  };
})();

const workspaces: ImageWorkspace[] = [];
afterEach(() => {
  for (const workspace of workspaces.splice(0)) {
    workspace.dispose();
  }
});

describe("sdkIamStack", () => {
  it("creates the stack with CAPABILITY_NAMED_IAM's single parameter and waits for it", async () => {
    const provisioner = new FakeStackProvisioner();
    const observed = await sdkIamStack(provisioner).create(BUCKET, {});
    expect(provisioner.calls.map(([name]) => name)).toEqual([
      "describe",
      "create",
      "wait",
      "describe",
    ]);
    expect(provisioner.calls).toContainEqual(["wait", IAM_STACK_NAME, "deployed"]);
    expect(provisioner.sentParameters).toEqual({ ArtifactBucket: BUCKET });
    expect(observed.parameters).toEqual({ ArtifactBucket: BUCKET });
  });

  it("an update sends only ArtifactBucket and keeps the six other parameters", async () => {
    const provisioner = new FakeStackProvisioner();
    provisioner.stacks.set(IAM_STACK_NAME, {
      name: IAM_STACK_NAME,
      state: "CREATE_COMPLETE",
      outputs: {},
      parameters: Object.fromEntries(
        ["ArtifactBucket", ...OTHER_PARAMETERS].map((name) => [name, "x"]),
      ),
    });
    await sdkIamStack(provisioner).updateBucket(BUCKET_B, {});
    expect(provisioner.sentParameters).toEqual({ ArtifactBucket: BUCKET_B });
    expect(provisioner.sentKeepPrevious).toEqual(OTHER_PARAMETERS);
    expect(provisioner.calls.map(([name]) => name)).not.toContain("create");
  });

  it("sends the caller's tags on create and on update", async () => {
    const provisioner = new FakeStackProvisioner();
    await sdkIamStack(provisioner).create(BUCKET, { Owner: "team" });
    expect(provisioner.sentTags).toMatchObject({ Owner: "team" });
    await sdkIamStack(provisioner).updateBucket(BUCKET_B, { Env: "test" });
    expect(provisioner.sentTags).toMatchObject({ Env: "test" });
  });

  it("observes a missing stack and an existing one", async () => {
    const provisioner = new FakeStackProvisioner();
    const gateway = sdkIamStack(provisioner);
    expect(await gateway.observe()).toEqual({
      name: IAM_STACK_NAME,
      status: undefined,
      outputs: {},
      parameters: {},
    });
    provisioner.stacks.set(IAM_STACK_NAME, {
      name: IAM_STACK_NAME,
      state: "CREATE_IN_PROGRESS",
      outputs: { BuildRoleArn: BUILD_ROLE },
      parameters: { ArtifactBucket: BUCKET },
    });
    expect(await gateway.observe()).toMatchObject({ status: "CREATE_IN_PROGRESS" });
  });

  it("waits for 'deployed' and observes again", async () => {
    const provisioner = new FakeStackProvisioner();
    provisioner.stacks.set(IAM_STACK_NAME, {
      name: IAM_STACK_NAME,
      state: "CREATE_COMPLETE",
      outputs: { BuildRoleArn: BUILD_ROLE },
      parameters: { ArtifactBucket: BUCKET },
    });
    const observed = await sdkIamStack(provisioner).wait();
    expect(provisioner.calls.map(([name]) => name)).toEqual(["wait", "describe"]);
    expect(observed.status).toBe("CREATE_COMPLETE");
  });

  it("fails if the stack vanished while waiting", async () => {
    const wait = sdkIamStack(new FakeStackProvisioner()).wait();
    await expect(wait).rejects.toBeInstanceOf(StackError);
    await expect(wait).rejects.toMatchObject({ code: "failed" });
  });

  it("declares exactly the parameters of the packaged iam template", async () => {
    const template = await loadTemplate(IAM_STACK_COMPONENT);
    expect(componentParameterNames(IAM_STACK_COMPONENT)).toEqual(templateParameterNames(template));
  });

  it("is not part of the rayito stack catalog", () => {
    expect(COMPONENTS.map((component) => component.name)).not.toContain("iam");
  });
});

describe("sdkArtifactStack", () => {
  it("creates the bucket stack with BucketName only and no capabilities", async () => {
    const provisioner = new FakeStackProvisioner();
    const observed = await sdkArtifactStack(provisioner).create(BUCKET, {});
    expect(provisioner.calls).toContainEqual(["create", ARTIFACT_STACK_NAME]);
    expect(provisioner.calls).toContainEqual(["wait", ARTIFACT_STACK_NAME, "deployed"]);
    expect(provisioner.sentParameters).toEqual({ BucketName: BUCKET });
    expect(ARTIFACT_STACK_COMPONENT.capabilities).toEqual([]);
    expect(observed).toMatchObject({
      name: ARTIFACT_STACK_NAME,
      parameters: { BucketName: BUCKET },
    });
  });

  it("sends the caller's tags when it creates the bucket stack", async () => {
    const provisioner = new FakeStackProvisioner();
    await sdkArtifactStack(provisioner).create(BUCKET, { Owner: "team" });
    expect(provisioner.sentTags).toMatchObject({ Owner: "team" });
  });

  it("observes a missing stack under its own name", async () => {
    expect(await sdkArtifactStack(new FakeStackProvisioner()).observe()).toEqual({
      name: ARTIFACT_STACK_NAME,
      status: undefined,
      outputs: {},
      parameters: {},
    });
  });

  it("fails if the stack vanished while waiting", async () => {
    await expect(sdkArtifactStack(new FakeStackProvisioner()).wait()).rejects.toMatchObject({
      code: "failed",
    });
  });

  it("declares exactly the parameters of the packaged artifact-bucket template", async () => {
    const template = await loadTemplate(ARTIFACT_STACK_COMPONENT);
    expect(componentParameterNames(ARTIFACT_STACK_COMPONENT)).toEqual(
      templateParameterNames(template),
    );
  });

  it("is not part of the rayito stack catalog", () => {
    expect(COMPONENTS.map((component) => component.name)).not.toContain("artifact-bucket");
  });
});

describe("SdkArtifactBuckets", () => {
  const accountId = async () => "123456789012";

  it("reports an existing bucket with its region", async () => {
    const doctor = new FakeDoctorAws();
    doctor.bucketFacts = { region: "eu-west-1" };
    expect(await new SdkArtifactBuckets(doctor, accountId).probe(BUCKET)).toEqual({
      state: "found",
      region: "eu-west-1",
    });
  });

  it("pins the probe to the caller's account so a squatted bucket is not reused", async () => {
    const doctor = new FakeDoctorAws();
    doctor.bucketFacts = { region: "eu-west-1" };
    await new SdkArtifactBuckets(doctor, accountId).probe(BUCKET);
    expect(doctor.headBucketOwners).toEqual(["123456789012"]);
  });

  it("an empty region is unknown", async () => {
    const doctor = new FakeDoctorAws();
    doctor.bucketFacts = { region: "" };
    expect((await new SdkArtifactBuckets(doctor, accountId).probe(BUCKET)).region).toBeUndefined();
  });

  it.each(["NotFound", "NoSuchBucket", "404"])("the code %s means missing", async (code) => {
    const doctor = new FakeDoctorAws();
    doctor.bucketFacts = new AwsOperationError("HeadBucket", code, "no");
    expect(await new SdkArtifactBuckets(doctor, accountId).probe(BUCKET)).toEqual({
      state: "missing",
      region: undefined,
    });
  });

  it("the code 403 means forbidden", async () => {
    const doctor = new FakeDoctorAws();
    doctor.bucketFacts = new AwsOperationError("HeadBucket", "403", "no");
    expect(await new SdkArtifactBuckets(doctor, accountId).probe(BUCKET)).toEqual({
      state: "forbidden",
      region: undefined,
    });
  });

  it("any other error propagates", async () => {
    const doctor = new FakeDoctorAws();
    doctor.bucketFacts = new AwsOperationError("HeadBucket", "SlowDown", "no");
    await expect(new SdkArtifactBuckets(doctor, accountId).probe(BUCKET)).rejects.toMatchObject({
      code: "SlowDown",
    });
  });

  it("delegates the account id", async () => {
    expect(await new SdkArtifactBuckets(new FakeDoctorAws(), accountId).accountId()).toBe(
      "123456789012",
    );
  });
});

describe("SdkBaseImages", () => {
  it("a missing image is observed without listing versions", async () => {
    const ports = new FakePublishPorts();
    const observed = await new SdkBaseImages(ports, clock).observe("rayito-base");
    expect(observed).toEqual({
      name: "rayito-base",
      imageArn: IMAGE_ARN,
      state: undefined,
      versions: [],
    });
    expect(ports.calls).toEqual(["getMicrovmImage"]);
  });

  it("an existing image lists its versions", async () => {
    const ports = new FakePublishPorts();
    ports.pendingReads = 0;
    ports.images.set(IMAGE_ARN, {
      state: "UPDATED",
      versions: [
        {
          imageVersion: "1.0",
          state: "SUCCESSFUL",
          status: "ACTIVE",
          createdAt: new Date("2026-10-01T00:00:00Z"),
        },
      ],
    });
    const observed = await new SdkBaseImages(ports, clock).observe("rayito-base");
    expect(observed.state).toBe("UPDATED");
    expect(observed.versions).toEqual([
      {
        imageVersion: "1.0",
        state: "SUCCESSFUL",
        status: "ACTIVE",
        createdAt: new Date("2026-10-01T00:00:00Z"),
      },
    ]);
  });

  it("waits for the three-state gate and reports each reading", async () => {
    const ports = new FakePublishPorts();
    ports.images.set(IMAGE_ARN, {
      state: "UPDATING",
      versions: [{ imageVersion: "2.0", createdAt: new Date("2026-10-02T00:00:00Z") }],
    });
    const lines: string[] = [];
    const result = await new SdkBaseImages(ports, clock).wait(IMAGE_ARN, "2.0", (line) =>
      lines.push(line),
    );
    expect(result).toEqual({ launchable: true, versionState: "SUCCESSFUL" });
    expect(lines.at(-1)).toMatch(/^ {2}image=UPDATED version=2\.0 state=SUCCESSFUL status=ACTIVE/);
  });
});

function artifactZip(): string {
  const workspace = new ImageWorkspace();
  workspaces.push(workspace);
  return workspace.zip();
}

describe("PipelineImagePublisher", () => {
  const options = { bucket: BUCKET, buildRoleArn: BUILD_ROLE };

  it("publishes rayito-base over the latest managed base and reports it", async () => {
    const ports = new FakePublishPorts();
    const lines: string[] = [];
    const published = await new PipelineImagePublisher(ports, clock).publish(artifactZip(), {
      ...options,
      onProgress: (line) => lines.push(line),
    });
    expect(published).toMatchObject({
      imageArn: IMAGE_ARN,
      imageVersion: "1.0",
      artifactUploaded: true,
    });
    expect(lines[0]).toBe("base_image_version=1");
    expect(ports.requests).toHaveLength(1);
    expect(ports.requests[0]?.body).toMatchObject({
      buildRoleArn: BUILD_ROLE,
      baseImageVersion: "1",
    });
  });

  it("an identical version is reused and the artifact is not uploaded twice", async () => {
    const ports = new FakePublishPorts();
    const zip = artifactZip();
    const publisher = new PipelineImagePublisher(ports, clock);
    await publisher.publish(zip, { ...options, onProgress: () => undefined });
    const again = await publisher.publish(zip, { ...options, onProgress: () => undefined });
    expect(again).toMatchObject({ imageVersion: "1.0", artifactUploaded: false });
    expect(ports.requests).toHaveLength(1);
  });

  it("a build that does not become launchable raises image_build_failed", async () => {
    const ports = new FakePublishPorts();
    ports.failNextBuild = true;
    const failure = await new PipelineImagePublisher(ports, clock)
      .publish(artifactZip(), { ...options, onProgress: () => undefined })
      .catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(AccountSetupError);
    expect(failure).toMatchObject({ code: "image_build_failed" });
    expect((failure as AccountSetupError).message).toContain("FAILED");
  });
});

describe("sdkAccountSetupGateways", () => {
  it("builds the six ports without any call", () => {
    const ports = new FakePublishPorts();
    const doctor = new FakeDoctorAws();
    const gateways = sdkAccountSetupGateways({
      region: "us-east-1",
      credentials: undefined,
      doctor,
      publishPorts: ports,
    });
    expect(Object.keys(gateways).sort()).toEqual([
      "artifactStack",
      "buckets",
      "iamStack",
      "images",
      "publisher",
      "releases",
    ]);
    expect(ports.calls).toEqual([]);
    expect(doctor.calls).toEqual([]);
  });
});
