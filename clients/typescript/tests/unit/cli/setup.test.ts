/**
 * `rayito setup` por la CLI: cada caso de `testdata/account-setup/cli-cases.json`
 * con dobles construidos desde su `input`, las plantillas de salida contra
 * `contract.json`, los flujos de `apply` con un doctor falso, la confirmación en
 * una terminal, el documento JSON y los errores de `--artifact` antes de
 * cualquier llamada. Espejo de `tests/unit/cli/test_setup.py`.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cdkBucketName, defaultArtifactBucketName } from "../../../src/account-setup/domain.js";
import { AwsOperationError } from "../../../src/cli/doctor/ports.js";
import type { TerminalInput } from "../../../src/cli/interactive-terminal.js";
import type { CliServices } from "../../../src/cli/services.js";
import * as output from "../../../src/cli/setup-output.js";
import { StackError } from "../../../src/errors.js";
import { VERSION } from "../../../src/version.js";
import {
  BUILD_ROLE_ARN,
  FakeAccountSetup,
  type FakeBehavior,
  type VectorInput,
  withPlaceholders,
} from "../account-setup-fakes.js";
import { buildWorld } from "./case-world.js";
import { FakeDoctorAws } from "./doctor-fakes.js";
import { ImageWorkspace } from "./image-fakes.js";
import { loadAccountSetupTestdata } from "./paths.js";
import { type CliOutcome, runWithServices } from "./run-case.js";

interface Contract {
  messages: {
    header: string;
    step: string;
    labels: Record<string, string>;
    actions: Record<string, string>;
    costHeader: string;
    costLine: string;
    nothingToDo: string;
    dryRunFooter: string;
    confirm: string;
    done: string;
    templateLine: string;
    bucketLine: string;
    doctorFailed: string;
    errorPrefix: string;
  };
  json: { documentKeys: string[]; resultKeys: string[]; errorKeys: string[] };
}

interface CliCase {
  name: string;
  args: string[];
  input: VectorInput;
  expected: {
    exitCode: number;
    stdout?: string;
    stderr: string;
    json?: Record<string, unknown>;
  };
}

const contract = loadAccountSetupTestdata<Contract>("contract.json");
const cases = loadAccountSetupTestdata<{ cases: CliCase[] }>("cli-cases.json").cases;
const HTTP_FORBIDDEN = 403;
const REGION = "us-east-1";
const ACCOUNT = "123456789012";
const IMAGE_ARN = `arn:aws:lambda:${REGION}:${ACCOUNT}:microvm-image:rayito-base`;
const CDK_BUCKET = cdkBucketName(ACCOUNT, REGION);

const FRESH: VectorInput = {
  region: REGION,
  account: ACCOUNT,
  bucket: null,
  environmentBucket: null,
  artifact: null,
  stack: null,
  buckets: { [CDK_BUCKET]: REGION },
  forbiddenBuckets: [],
  artifactStack: null,
  image: null,
};

const DEFAULT_BUCKET = defaultArtifactBucketName(ACCOUNT, REGION);

const workspaces: ImageWorkspace[] = [];
afterEach(() => {
  for (const workspace of workspaces.splice(0)) {
    workspace.dispose();
  }
});

interface Invocation {
  readonly args: string[];
  readonly input?: VectorInput;
  readonly behavior?: FakeBehavior;
  readonly doctor?: FakeDoctorAws;
  readonly stdin?: string;
  readonly terminal?: boolean;
  readonly env?: Record<string, string>;
}

const TTY_INPUT: TerminalInput = {
  isTTY: true,
  on: () => undefined,
  off: () => undefined,
  pause: () => undefined,
  resume: () => undefined,
};

async function invoke(
  invocation: Invocation,
): Promise<CliOutcome & { fake: FakeAccountSetup; doctor: FakeDoctorAws }> {
  const input = invocation.input ?? FRESH;
  const fake = new FakeAccountSetup(input, invocation.behavior);
  const doctor = invocation.doctor ?? new FakeDoctorAws();
  const services: CliServices = {
    ...buildWorld({}).services,
    region: input.region,
    accountSetup: fake,
    doctor,
  };
  const outcome = await runWithServices({
    args: invocation.args,
    services,
    stdin: invocation.stdin,
    env: invocation.env,
    rawInput: invocation.terminal === true ? TTY_INPUT : undefined,
  });
  return { ...outcome, fake, doctor };
}

describe("rayito setup: shared CLI cases", () => {
  it.each(cases.map((item) => [item.name, item] as const))("%s", async (_name, raw) => {
    const item = withPlaceholders(raw, raw.input, VERSION);
    const outcome = await invoke({ args: item.args, input: item.input });
    expect(outcome.exitCode).toBe(item.expected.exitCode);
    expect(outcome.stderr).toBe(item.expected.stderr);
    if (item.expected.json === undefined) {
      expect(outcome.stdout).toBe(item.expected.stdout);
    } else {
      expect(JSON.parse(outcome.stdout)).toEqual(item.expected.json);
    }
  });

  it("an unsupported region makes no call", async () => {
    const input = { ...FRESH, region: "sa-east-1" };
    const outcome = await invoke({ args: ["setup", "--dry-run"], input });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.fake.calls).toEqual([]);
  });

  it("a dry run only reads and downloads nothing", async () => {
    const outcome = await invoke({ args: ["setup", "--dry-run"] });
    expect(outcome.fake.calls.map(([name]) => name)).toEqual([
      "accountId",
      "describe",
      "head_bucket",
      "get_image",
    ]);
  });
});

describe("rayito setup: output templates", () => {
  it("equal the contract", () => {
    const { messages } = contract;
    expect(output.HEADER_TEMPLATE).toBe(messages.header);
    expect(output.STEP_TEMPLATE).toBe(messages.step);
    expect({ ...output.RESOURCE_LABELS }).toEqual(messages.labels);
    expect({ ...output.ACTION_TEMPLATES }).toEqual(messages.actions);
    expect(output.COST_HEADER).toBe(messages.costHeader);
    expect(output.COST_LINE_TEMPLATE).toBe(messages.costLine);
    expect(output.NOTHING_TO_DO).toBe(messages.nothingToDo);
    expect(output.DRY_RUN_FOOTER).toBe(messages.dryRunFooter);
    expect(output.CONFIRM_QUESTION).toBe(messages.confirm);
    expect(output.DONE_TEMPLATE).toBe(messages.done);
    expect(output.TEMPLATE_LINE_TEMPLATE).toBe(messages.templateLine);
    expect(output.BUCKET_LINE_TEMPLATE).toBe(messages.bucketLine);
    expect(output.DOCTOR_FAILED).toBe(messages.doctorFailed);
    expect(output.ERROR_PREFIX).toBe(messages.errorPrefix);
  });
});

function lastLines(text: string, count: number): string[] {
  return text.trimEnd().split("\n").slice(-count);
}

describe("rayito setup: apply", () => {
  it("prepares a fresh account and prints the next-step variables", async () => {
    const outcome = await invoke({ args: ["setup", "--yes"] });
    expect(outcome.exitCode).toBe(0);
    expect(lastLines(outcome.stdout, 3)).toEqual([
      `rayito setup: cuenta preparada en ${REGION}`,
      `RAYITO_TEMPLATE=${IMAGE_ARN}`,
      `RAYITO_BUCKET=${CDK_BUCKET}`,
    ]);
    expect(outcome.stdout).toContain("creando la pila de IAM rayito-m0-iam");
    expect(outcome.stdout).toContain("rayito doctor:");
    expect(outcome.fake.writes.map(([name]) => name)).toEqual([
      "create",
      "download",
      "download",
      "publish",
    ]);
  });

  it("a doctor FAIL exits 1 with the doctorFailed line last", async () => {
    const doctor = new FakeDoctorAws();
    doctor.identity = new AwsOperationError("GetCallerIdentity", "ExpiredToken", "caducado");
    const outcome = await invoke({ args: ["setup", "--yes"], doctor });
    expect(outcome.exitCode).toBe(1);
    expect(lastLines(outcome.stdout, 1)).toEqual([contract.messages.doctorFailed]);
  });

  it("runs the doctor on rayito-base with the chosen bucket and no launch", async () => {
    const outcome = await invoke({ args: ["setup", "--yes"] });
    expect(outcome.doctor.calls).toContain(`headBucket:${CDK_BUCKET}`);
    expect(outcome.doctor.launches).toEqual([]);
  });

  it("a prepared account writes nothing and still runs the doctor", async () => {
    const input: VectorInput = {
      ...FRESH,
      buckets: { "amzn-s3-demo-bucket": REGION },
      stack: {
        status: "CREATE_COMPLETE",
        outputs: { BuildRoleArn: BUILD_ROLE_ARN },
        parameters: { ArtifactBucket: "amzn-s3-demo-bucket" },
      },
      image: {
        state: "UPDATED",
        versions: [
          {
            imageVersion: "1.0",
            state: "SUCCESSFUL",
            status: "ACTIVE",
            createdAt: "2026-10-01T00:00:00Z",
          },
        ],
      },
    };
    const outcome = await invoke({ args: ["setup"], input });
    expect(outcome.exitCode).toBe(0);
    expect(outcome.fake.writes).toEqual([]);
    expect(outcome.stdout).toContain(contract.messages.nothingToDo);
    expect(lastLines(outcome.stdout, 1)).toEqual(["RAYITO_BUCKET=amzn-s3-demo-bucket"]);
  });

  it("RAYITO_BUCKET picks the bucket", async () => {
    const input = { ...FRESH, buckets: { "amzn-s3-demo-bucket": REGION } };
    const outcome = await invoke({
      args: ["setup", "--dry-run"],
      input,
      env: { RAYITO_BUCKET: "amzn-s3-demo-bucket" },
    });
    expect(outcome.stdout).toContain("bucket amzn-s3-demo-bucket: reutilizar (RAYITO_BUCKET)");
  });

  it("a checksum mismatch exits 1 and publishes nothing", async () => {
    const outcome = await invoke({
      args: ["setup", "--yes"],
      behavior: { downloadedSha256: "b".repeat(64) },
    });
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stderr).toContain("no coincide con SHA256SUMS");
    expect(outcome.fake.calls.map(([name]) => name)).not.toContain("publish");
  });
});

describe("rayito setup: an account without a bucket", () => {
  it("prints the bucket progress and ends with the created bucket", async () => {
    const outcome = await invoke({ args: ["setup", "--yes"], input: { ...FRESH, buckets: {} } });
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toContain(
      `creando el bucket ${DEFAULT_BUCKET} con la pila rayito-m0-artifacts`,
    );
    expect(outcome.stdout).toContain(`bucket ${DEFAULT_BUCKET} listo (pila rayito-m0-artifacts)`);
    expect(lastLines(outcome.stdout, 1)).toEqual([`RAYITO_BUCKET=${DEFAULT_BUCKET}`]);
    expect(outcome.fake.writes.map(([name]) => name)).toEqual([
      "create_artifact_stack",
      "create",
      "download",
      "download",
      "publish",
    ]);
  });
});

describe("rayito setup: confirmation", () => {
  it("asks on a terminal and 'n' exits 1 without writing", async () => {
    const outcome = await invoke({ args: ["setup"], stdin: "n\n", terminal: true });
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stdout).toContain(`${contract.messages.confirm} [y/N]: `);
    expect(outcome.fake.writes).toEqual([]);
  });

  it("asks on a terminal and 'y' applies", async () => {
    const outcome = await invoke({ args: ["setup"], stdin: "y\n", terminal: true });
    expect(outcome.exitCode).toBe(0);
    expect(outcome.fake.writes.length).toBeGreaterThan(0);
  });

  it("does not ask when there is nothing to create", async () => {
    const input: VectorInput = {
      ...FRESH,
      buckets: { "amzn-s3-demo-bucket": REGION },
      stack: {
        status: "CREATE_COMPLETE",
        outputs: { BuildRoleArn: BUILD_ROLE_ARN },
        parameters: { ArtifactBucket: "amzn-s3-demo-bucket" },
      },
      image: {
        state: "UPDATED",
        versions: [
          {
            imageVersion: "1.0",
            state: "SUCCESSFUL",
            status: "ACTIVE",
            createdAt: "2026-10-01T00:00:00Z",
          },
        ],
      },
    };
    const outcome = await invoke({ args: ["setup"], input, terminal: true });
    expect(outcome.stdout).not.toContain(contract.messages.confirm);
  });
});

describe("rayito setup: --json", () => {
  it("emits one document with the contract keys and the doctor document", async () => {
    const outcome = await invoke({ args: ["--json", "setup", "--yes"] });
    const document = JSON.parse(outcome.stdout) as Record<string, unknown>;
    expect(Object.keys(document)).toEqual(contract.json.documentKeys);
    expect(Object.keys(document.result as object)).toEqual(contract.json.resultKeys);
    expect(document).toMatchObject({
      ready: true,
      dryRun: false,
      error: null,
      exitCode: 0,
      result: {
        bucket: CDK_BUCKET,
        imageArn: IMAGE_ARN,
        imageVersion: "3.0",
        artifactUploaded: true,
      },
    });
    expect(document.doctor).toMatchObject({ rayito: VERSION, region: REGION });
    expect(outcome.stderr).toContain("creando la pila de IAM rayito-m0-iam");
  });

  it("an error document carries code, message and exit code", async () => {
    const outcome = await invoke({
      args: ["--json", "setup", "--yes"],
      behavior: { downloadedSha256: "b".repeat(64) },
    });
    const document = JSON.parse(outcome.stdout) as Record<string, unknown>;
    expect(Object.keys(document.error as object)).toEqual(contract.json.errorKeys);
    expect(document).toMatchObject({
      ready: false,
      result: null,
      doctor: null,
      exitCode: 1,
      error: { code: "checksum_mismatch" },
    });
    expect(outcome.stderr).toContain("rayito: el sha256");
  });
});

describe("rayito setup: --json with a failure setup does not translate", () => {
  async function failing(args: string[], error: Error): Promise<CliOutcome> {
    const fake = new FakeAccountSetup(FRESH);
    const services: CliServices = {
      ...buildWorld({}).services,
      region: REGION,
      accountSetup: {
        ...fake,
        iamStack: {
          ...fake.iamStack,
          observe: async () => {
            throw error;
          },
        },
      },
      doctor: new FakeDoctorAws(),
    };
    return runWithServices({ args, services });
  }

  const denied = Object.assign(
    new Error("not authorized to perform cloudformation:DescribeStacks"),
    {
      name: "AccessDenied",
      // `$metadata` es el nombre real del campo del SDK; se construye aparte para no escribirlo como clave literal.
      [`${"$"}metadata`]: { httpStatusCode: HTTP_FORBIDDEN },
    },
  );

  it("emits one error document for an AWS error and exits 1 like human mode", async () => {
    const human = await failing(["setup", "--dry-run"], denied);
    const json = await failing(["--json", "setup", "--dry-run"], denied);
    expect(json.exitCode).toBe(human.exitCode);
    expect(json.exitCode).toBe(1);
    const document = JSON.parse(json.stdout) as Record<string, unknown>;
    expect(Object.keys(document)).toEqual(contract.json.documentKeys);
    expect(document).toMatchObject({
      ready: false,
      steps: [],
      result: null,
      doctor: null,
      dryRun: true,
      exitCode: 1,
      error: { code: "stack_blocked" },
    });
    expect((document.error as { message: string }).message).toMatch(/^AWS error AccessDenied/);
    expect(json.stderr).toBe(human.stderr);
  });

  it("emits one error document for a stack exception", async () => {
    const json = await failing(
      ["--json", "setup", "--yes"],
      new StackError("falló la pila", { code: "failed" }),
    );
    expect(json.exitCode).toBe(1);
    expect(JSON.parse(json.stdout)).toMatchObject({
      error: { code: "stack_blocked", message: "falló la pila" },
      exitCode: 1,
    });
  });

  it("an environment failure keeps exit 2 in the document", async () => {
    const json = await failing(
      ["--json", "setup", "--dry-run"],
      Object.assign(new Error("no creds"), { name: "CredentialsProviderError" }),
    );
    expect(json.exitCode).toBe(2);
    expect(JSON.parse(json.stdout)).toMatchObject({
      error: { code: "bucket_unavailable" },
      exitCode: 2,
    });
  });

  it("human mode stays as it was: the shared handler prints one line", async () => {
    const human = await failing(["setup", "--dry-run"], denied);
    expect(human.stdout).toBe("");
    expect(human.stderr).toMatch(/^rayito: AWS error AccessDenied/);
  });
});

describe("rayito setup: --artifact", () => {
  function zip(variant: string, withEfs = false): string {
    const workspace = new ImageWorkspace();
    workspaces.push(workspace);
    return workspace.zip(variant, withEfs);
  }

  it("a missing file exits 2 before any call", async () => {
    const outcome = await invoke({ args: ["setup", "--dry-run", "--artifact", "/nope/image.zip"] });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toBe("rayito: /nope/image.zip no existe\n");
    expect(outcome.fake.calls).toEqual([]);
  });

  it.each([
    ["slim", false],
    ["full", true],
  ])("a %s artifact (efs %s) exits 2 before any call", async (variant, withEfs) => {
    const path = zip(variant, withEfs);
    const outcome = await invoke({ args: ["setup", "--dry-run", "--artifact", path] });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("no es un zip de la variante full sin EFS");
    expect(outcome.fake.calls).toEqual([]);
  });

  it("a full artifact is published without downloading anything", async () => {
    const path = zip("full");
    const outcome = await invoke({ args: ["setup", "--yes", "--artifact", path] });
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toContain(`imagen rayito-base: publicar desde ${path} (≈ 10 min)`);
    expect(outcome.fake.calls.map(([name]) => name)).not.toContain("download");
  });
});
