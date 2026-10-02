/**
 * `src/templates/build.ts` (m15-templates) sobre un `BuildClients` falso:
 * espejo de `test_m15_templates_build.py`.
 */

import { afterEach, describe, expect, test } from "vitest";
import {
  BuildError,
  InvalidArgumentError,
  NotFoundError,
  TemplateError,
} from "../../src/errors.js";
import {
  _resetBuildClientsFactory,
  _setBuildClientsFactory,
  build,
  buildInBackground,
  _desiredConfigurationForTests as desiredConfiguration,
  getBuildStatus,
  _imageArnForTests as imageArn,
  templateExists,
} from "../../src/templates/build.js";
import { buildsInFlight } from "../../src/templates/concurrency.js";
import { Template } from "../../src/templates/dsl.js";
import { FakeBuildClients, makeBaseZip } from "./m15-fake-build-clients.js";

const BASE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base";
const BUCKET = "my-artifact-bucket";
const BASE_DOCKERFILE =
  'FROM scratch\nCOPY rayd /usr/local/bin/rayd\nCMD ["/usr/local/bin/rayd"]\n';
const BASE_HOOKS = { port: 9000 };

function clientsWithBaseImage(): FakeBuildClients {
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
    hooks: BASE_HOOKS,
    codeArtifact: { uri: `s3://${BUCKET}/base.zip` },
  });
  clients.objects.set(
    `${BUCKET}/base.zip`,
    makeBaseZip(BASE_DOCKERFILE, { rayd: new Uint8Array([0x7f]) }),
  );
  return clients;
}

afterEach(() => {
  _resetBuildClientsFactory();
});

describe("templates/build", () => {
  test("imageArn passes through an already-resolved ARN", () => {
    const clients = new FakeBuildClients();
    expect(imageArn(clients, BASE_ARN, "123456789012")).toBe(BASE_ARN);
  });

  test("imageArn builds one from region, account and name", () => {
    const clients = new FakeBuildClients();
    expect(imageArn(clients, "mi-template", "444455556666")).toBe(
      `arn:aws:lambda:${clients.region}:444455556666:microvm-image:mi-template`,
    );
  });

  test("building without fromBaseImage raises first", async () => {
    _setBuildClientsFactory(() => new FakeBuildClients());
    await expect(
      build(new Template().pipInstall("pandas"), "mi-template", { bucket: BUCKET }),
    ).rejects.toThrow(InvalidArgumentError);
  });

  test("building with an unsupported memory size raises before any call", async () => {
    await expect(
      build(new Template().fromBaseImage(), "mi-template", { bucket: BUCKET, memoryMb: 3000 }),
    ).rejects.toThrow(InvalidArgumentError);
  });

  test("a successful build uploads the artifact and returns BuildInfo", async () => {
    const clients = clientsWithBaseImage();
    _setBuildClientsFactory(() => clients);

    const t = new Template().fromBaseImage().pipInstall("pandas");
    const info = await build(t, "mi-template", { bucket: BUCKET, contextDir: "." });

    expect(info.templateId.endsWith(":microvm-image:mi-template")).toBe(true);
    expect(info.alias).toBe("mi-template");
    expect(clients.calls.some((call) => call[0] === "createMicrovmImage")).toBe(true);
    expect(clients.calls.filter((call) => call[0] === "putObject")).toHaveLength(1);
  });

  test("building twice with the same configuration reuses the version", async () => {
    const clients = clientsWithBaseImage();
    _setBuildClientsFactory(() => clients);
    const t = new Template().fromBaseImage().pipInstall("pandas");

    await build(t, "mi-template", { bucket: BUCKET, contextDir: "." });
    clients.calls.length = 0;
    await build(t, "mi-template", { bucket: BUCKET, contextDir: "." });

    expect(
      clients.calls.some(
        (call) => call[0] === "createMicrovmImage" || call[0] === "updateMicrovmImage",
      ),
    ).toBe(false);
  });

  test("force rebuilds even with an identical configuration", async () => {
    const clients = clientsWithBaseImage();
    _setBuildClientsFactory(() => clients);
    const t = new Template().fromBaseImage().pipInstall("pandas");

    await build(t, "mi-template", { bucket: BUCKET, contextDir: "." });
    clients.calls.length = 0;
    await build(t, "mi-template", { bucket: BUCKET, contextDir: ".", force: true });

    expect(clients.calls.some((call) => call[0] === "updateMicrovmImage")).toBe(true);
  });

  test("a failed RUN step raises BuildError with step and exit code", async () => {
    const clients = clientsWithBaseImage();
    clients.logLines = [
      "#4 [2/5] RUN pip install not-a-real-package",
      "#4 ERROR: executor failed running [...]: exit code: 1",
    ];
    clients.failNextBuildWith("The container image build failed.");
    _setBuildClientsFactory(() => clients);

    await expect(
      build(
        new Template().fromBaseImage().pipInstall("not-a-real-package"),
        "mi-failing-template",
        {
          bucket: BUCKET,
          contextDir: ".",
        },
      ),
    ).rejects.toMatchObject({ step: 2, exitCode: 1 });
  });

  test("a ready cmd server error is classified as such", async () => {
    const clients = clientsWithBaseImage();
    clients.failNextBuildWith(
      "Ready hook check failed: the application returned a server error (HTTP 5xx) response",
    );
    _setBuildClientsFactory(() => clients);

    await expect(
      build(
        new Template()
          .fromBaseImage()
          .setStartCmd("python app.py", "curl -f http://localhost:8000"),
        "mi-template",
        { bucket: BUCKET, contextDir: "." },
      ),
    ).rejects.toMatchObject({ reason: "ready_server_error" });
  });

  test("buildInBackground then getBuildStatus reports SUCCESSFUL", async () => {
    const clients = clientsWithBaseImage();
    _setBuildClientsFactory(() => clients);

    const handle = await buildInBackground(
      new Template().fromBaseImage().pipInstall("pandas"),
      "mi-template",
      {
        bucket: BUCKET,
        contextDir: ".",
      },
    );
    const status = await getBuildStatus(handle);
    expect(status.state).toBe("SUCCESSFUL");
    expect(status.info?.templateId).toBe(handle.arn);
  });

  test("templateExists reflects whether the image was created", async () => {
    const clients = clientsWithBaseImage();
    _setBuildClientsFactory(() => clients);
    expect(await templateExists("mi-template")).toBe(false);
    clients.images.set(imageArn(clients, "mi-template", "123456789012"), { state: "CREATED" });
    expect(await templateExists("mi-template")).toBe(true);
  });

  test("resolveBaseVersion with no active version is NotFoundError", async () => {
    _setBuildClientsFactory(() => new FakeBuildClients());
    await expect(
      build(new Template().fromBaseImage(), "mi-template", { bucket: BUCKET }),
    ).rejects.toThrow(NotFoundError);
  });

  test("desiredConfiguration copies the base image's own managed base, not itself", () => {
    const baseVersion = {
      imageArn: BASE_ARN,
      imageVersion: "7",
      baseImageArn: "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
      baseImageVersion: "1.0",
      buildRoleArn: "arn:aws:iam::123456789012:role/rayito-build",
      hooks: BASE_HOOKS,
    };
    const desired = desiredConfiguration({
      artifactUri: "s3://bucket/key.zip",
      memoryMb: 2048,
      baseImageVersion: baseVersion,
      logGroup: "/rayito/x",
    });
    expect(desired["baseImageArn"]).toBe("arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1");
    expect(desired["baseImageVersion"]).toBe("1.0");
  });

  test("a failing build still throws BuildError, not an unhandled type error", async () => {
    const clients = clientsWithBaseImage();
    _setBuildClientsFactory(() => clients);
    clients.failNextBuildWith("some other reason");
    await expect(
      build(new Template().fromBaseImage().pipInstall("pandas"), "mi-template", {
        bucket: BUCKET,
        contextDir: ".",
      }),
    ).rejects.toBeInstanceOf(BuildError);
  });

  test("desiredConfiguration keeps the caps variant's OS capabilities", () => {
    const desired = desiredConfiguration({
      artifactUri: "s3://bucket/key.zip",
      memoryMb: 2048,
      baseImageVersion: {
        baseImageArn: "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
        baseImageVersion: "1.0",
        buildRoleArn: "arn:aws:iam::123456789012:role/rayito-build",
        hooks: BASE_HOOKS,
        cpuConfigurations: [{ architecture: "ARM_64" }],
        additionalOsCapabilities: ["ALL"],
      },
      logGroup: "/rayito/x",
    });
    expect(desired["additionalOsCapabilities"]).toEqual(["ALL"]);
    expect(desired["cpuConfigurations"]).toEqual([{ architecture: "ARM_64" }]);
    expect(desired["codeArtifact"]).toEqual({ uri: "s3://bucket/key.zip" });
  });

  test("a caps base image builds a caps template", async () => {
    const clients = clientsWithBaseImage();
    const base = clients.versions.get(`${BASE_ARN}#1`);
    clients.versions.set(`${BASE_ARN}#1`, { ...base!, additionalOsCapabilities: ["ALL"] });
    _setBuildClientsFactory(() => clients);

    const handle = await buildInBackground(new Template().fromBaseImage(), "mi-template", {
      bucket: BUCKET,
      contextDir: ".",
    });

    expect(
      clients.versions.get(`${handle.arn}#${handle.version}`)?.["additionalOsCapabilities"],
    ).toEqual(["ALL"]);
  });

  test("skipCache rebuilds even with an identical configuration", async () => {
    const clients = clientsWithBaseImage();
    _setBuildClientsFactory(() => clients);

    await build(new Template().fromBaseImage(), "mi-template", { bucket: BUCKET, contextDir: "." });
    clients.calls.length = 0;
    await build(new Template().fromBaseImage().skipCache(), "mi-template", {
      bucket: BUCKET,
      contextDir: ".",
    });

    expect(clients.calls.some((call) => call[0] === "updateMicrovmImage")).toBe(true);
  });

  test("the AWS build quota is a BuildError with reason build_quota", async () => {
    const clients = clientsWithBaseImage();
    clients.submitErrorName = "ServiceQuotaExceededException";
    _setBuildClientsFactory(() => clients);

    await expect(
      buildInBackground(new Template().fromBaseImage(), "mi-template", {
        bucket: BUCKET,
        contextDir: ".",
      }),
    ).rejects.toMatchObject({ reason: "build_quota" });
  });

  test("build holds its slot while waiting for the gate", async () => {
    const clients = clientsWithBaseImage();
    const slotsDuringGate: number[] = [];
    const original = clients.getMicrovmImageVersion.bind(clients);
    clients.getMicrovmImageVersion = async (arn, version) => {
      if (arn.endsWith(":mi-template")) {
        slotsDuringGate.push(buildsInFlight());
      }
      return original(arn, version);
    };
    _setBuildClientsFactory(() => clients);

    await build(new Template().fromBaseImage(), "mi-template", { bucket: BUCKET, contextDir: "." });

    expect(slotsDuringGate.length).toBeGreaterThan(0);
    expect(slotsDuringGate.every((slots) => slots === 1)).toBe(true);
    expect(buildsInFlight()).toBe(0);
  });

  test.each(["mi-template:v1", "", "con espacio", "x".repeat(65)])(
    "an invalid template name %j is a TemplateError",
    async (name) => {
      await expect(
        build(new Template().fromBaseImage(), name, { bucket: BUCKET }),
      ).rejects.toBeInstanceOf(TemplateError);
    },
  );

  test("a failed build message names the template, not the ARN or AWS text", async () => {
    const clients = clientsWithBaseImage();
    clients.failNextBuildWith("Build failed for arn:aws:lambda:us-east-1:123456789012:x");
    _setBuildClientsFactory(() => clients);

    const error = await build(new Template().fromBaseImage(), "mi-template", {
      bucket: BUCKET,
      contextDir: ".",
    }).then(
      () => undefined,
      (caught: unknown) => caught as BuildError,
    );

    expect(error).toBeInstanceOf(BuildError);
    expect(error?.message).toContain("mi-template");
    expect(error?.message).not.toContain(clients.accountIdValue);
    expect(error?.message).not.toContain("Build failed for");
  });

  test("a missing base image is NotFoundError without the account id", async () => {
    _setBuildClientsFactory(() => new FakeBuildClients());
    const error = await build(new Template().fromBaseImage(), "mi-template", {
      bucket: BUCKET,
    }).then(
      () => undefined,
      (caught: unknown) => caught as Error,
    );
    expect(error).toBeInstanceOf(NotFoundError);
    expect(error?.message).not.toContain("123456789012");
  });
});
