/**
 * `rayito doctor`: cada comprobación en cada estado, los escenarios del spec
 * (cuenta nueva sin imagen, cuotas reducidas y deny implícito como WARN,
 * `--launch` terminado pase lo que pase, la sonda de `Health`) y que una
 * excepción en una comprobación no detiene las demás. Espejo de
 * `clients/python/tests/unit/cli/test_doctor.py`; el contrato de nombres y
 * acciones IAM lo comparte `testdata/cli/doctor-contract.json`.
 */

import { describe, expect, it } from "vitest";
import type { PortSpec } from "../../../src/aws/control-plane.js";
import { CliConsole } from "../../../src/cli/console.js";
import {
  CHECK_NAMES,
  CHECKS,
  EFS_NETWORK_CHECK,
  principalKind,
  runCheck,
  runDoctor,
} from "../../../src/cli/doctor/checks.js";
import * as constants from "../../../src/cli/doctor/constants.js";
import { type DoctorContext, newDoctorContext } from "../../../src/cli/doctor/model.js";
import { AwsOperationError, MissingPeerError } from "../../../src/cli/doctor/ports.js";
import { doctorDocument, renderJson } from "../../../src/cli/doctor/render.js";
import { AuthenticationError, SandboxError } from "../../../src/errors.js";
import { IMAGE_ARTIFACT_KEY_PREFIX, MANAGED_BASE_IMAGE_NAME } from "../../../src/images/managed.js";
import { sandboxInfo } from "../../../src/models.js";
import { VERSION } from "../../../src/version.js";
import type {
  NetworkInspector,
  RouteTableFacts,
  SubnetFacts,
  VpcFacts,
} from "../../../src/volumes/network.js";
import { buildWorld, type CaseWorld, REGION } from "./case-world.js";
import {
  BUCKET,
  defaultQuotas,
  FakeDoctorAws,
  FakeLaunched,
  healthy,
  JWE,
  ROLE_ARN,
  ROLE_SESSION_ARN,
  USER_ARN,
} from "./doctor-fakes.js";
import { loadCliTestdata } from "./paths.js";
import { runWithServices } from "./run-case.js";
import { FakeTerminal } from "./terminal.js";

interface DoctorContract {
  checkNames: string[];
  preLaunchCheckCount: number;
  defaultTemplate: string;
  launchTimeoutSeconds: number;
  launchMetadata: Record<string, string>;
  maxRunningBeforeFail: number;
  maxListedIds: number;
  capsImageSuffix: string;
  imageArtifactKeyPrefix: string;
  managedBaseImageName: string;
  simulatedActions: {
    image: string[];
    wildcard: string[];
    connector: string[];
    bucketObjects: string[];
    bucket: string[];
  };
}

const contract = loadCliTestdata<DoctorContract>("doctor-contract.json");
const STARTED = "2026-09-15T14:39:02Z";
const IMAGE_VERSION = "17.0";

const MS_PER_HOUR = 3_600_000;

function runningWorldOf(image: string, ...ids: string[]): CaseWorld {
  const sandboxes = ids.map((id, index) => ({
    id,
    state: "RUNNING",
    image,
    version: IMAGE_VERSION,
    startedAt: new Date(Date.parse(STARTED) + index * MS_PER_HOUR).toISOString(),
  }));
  return { sandboxes, infos: sandboxes };
}

function runningWorld(...ids: string[]): CaseWorld {
  return runningWorldOf("rayito-base", ...ids);
}

function spec(name: string): (typeof CHECKS)[number] {
  const found = CHECKS.find((candidate) => candidate.name === name);
  if (found === undefined) {
    throw new Error(`sin comprobación ${name}`);
  }
  return found;
}

function context(overrides: Partial<DoctorContext> = {}): DoctorContext {
  return {
    ...newDoctorContext({ template: constants.DEFAULT_TEMPLATE, bucket: BUCKET }),
    ...overrides,
  };
}

function withIdentity(
  arn: string = USER_ARN,
  overrides: Partial<DoctorContext> = {},
): DoctorContext {
  return context({
    ...overrides,
    account: "123456789012",
    principalArn: arn,
    principalKind: principalKind(arn),
  });
}

function denied(operation: string): AwsOperationError {
  return new AwsOperationError(operation, constants.ACCESS_DENIED, "denied");
}

function statuses(
  results: ReadonlyArray<{ name: string; status: string }>,
): Record<string, string> {
  return Object.fromEntries(results.map((result) => [result.name, result.status]));
}

async function runAll(aws: FakeDoctorAws, overrides: Partial<DoctorContext> = {}) {
  const doctorContext = context(overrides);
  const warnings: string[] = [];
  const results = await runDoctor(aws, doctorContext, (message) => warnings.push(message));
  return { results, doctorContext, warnings };
}

function check(results: ReadonlyArray<{ name: string }>, name: string) {
  const found = results.find((result) => result.name === name);
  if (found === undefined) {
    throw new Error(`sin resultado ${name}`);
  }
  return found as {
    name: string;
    status: string;
    summary: string;
    details: Record<string, unknown>;
  };
}

describe("contrato compartido con Python", () => {
  it("los nombres, los umbrales y las acciones IAM son los de doctor-contract.json", () => {
    expect([...CHECK_NAMES]).toEqual(contract.checkNames);
    expect(constants.PRE_LAUNCH_CHECK_COUNT).toBe(contract.preLaunchCheckCount);
    expect(constants.DEFAULT_TEMPLATE).toBe(contract.defaultTemplate);
    expect(constants.LAUNCH_TIMEOUT_SECONDS).toBe(contract.launchTimeoutSeconds);
    expect(constants.LAUNCH_METADATA).toEqual(contract.launchMetadata);
    expect(constants.MAX_RUNNING_BEFORE_FAIL).toBe(contract.maxRunningBeforeFail);
    expect(constants.MAX_LISTED_IDS).toBe(contract.maxListedIds);
    expect(constants.CAPS_IMAGE_SUFFIX).toBe(contract.capsImageSuffix);
    expect(IMAGE_ARTIFACT_KEY_PREFIX).toBe(contract.imageArtifactKeyPrefix);
    expect(MANAGED_BASE_IMAGE_NAME).toBe(contract.managedBaseImageName);
    expect([...constants.IMAGE_ACTIONS]).toEqual(contract.simulatedActions.image);
    expect([...constants.WILDCARD_ACTIONS]).toEqual(contract.simulatedActions.wildcard);
    expect([...constants.CONNECTOR_ACTIONS]).toEqual(contract.simulatedActions.connector);
    expect([...constants.BUCKET_OBJECT_ACTIONS]).toEqual(contract.simulatedActions.bucketObjects);
    expect([...constants.BUCKET_ACTIONS]).toEqual(contract.simulatedActions.bucket);
  });
});

describe("una cuenta nueva y los escenarios del spec", () => {
  it("sin imagen: image-gate falla y las comprobaciones del agente se saltan", async () => {
    const aws = new FakeDoctorAws();
    aws.image = undefined;
    const { results } = await runAll(aws);
    expect(statuses(results)).toEqual({
      credentials: "OK",
      "managed-images": "OK",
      quotas: "OK",
      "iam-simulation": "OK",
      bucket: "OK",
      "image-gate": "FAIL",
      sandboxes: "OK",
      token: "SKIP",
      agent: "SKIP",
      compatibility: "SKIP",
    });
    expect(check(results, "image-gate").summary).toContain("rayito image publish");
    expect(check(results, "managed-images").details.newest_version).toMatchObject({
      imageVersion: "1",
    });
    expect(results.map((result) => result.name)).toEqual([...CHECK_NAMES]);
  });

  it("cuotas reducidas y deny implícito son avisos, no fallos", async () => {
    const aws = new FakeDoctorAws();
    aws.quotas = defaultQuotas({ "L-535CA9B6": 1 });
    aws.decisions = { "lambda:SuspendMicrovm": "implicitDeny" };
    const { results } = await runAll(aws);
    expect(check(results, "quotas").status).toBe("WARN");
    expect(check(results, "quotas").summary).toContain("RunMicrovm rate 1 < 5");
    const simulation = check(results, "iam-simulation");
    expect(simulation.status).toBe("WARN");
    expect(simulation.summary).toContain("lambda:SuspendMicrovm");
    expect(simulation.summary).toContain("orientativa");
    expect(check(results, "image-gate").status).toBe("OK");
  });

  it("una excepción en una comprobación no detiene las demás", async () => {
    const aws = new FakeDoctorAws();
    aws.identity = new RangeError("kaboom");
    const { results } = await runAll(aws);
    expect(check(results, "credentials").status).toBe("FAIL");
    expect(check(results, "credentials").summary).toContain("RangeError: kaboom");
    expect(results).toHaveLength(constants.PRE_LAUNCH_CHECK_COUNT + 3);
    expect(check(results, "image-gate").status).toBe("OK");
    expect(check(results, "iam-simulation").status).toBe("SKIP");
  });

  it("cada resultado lleva elapsed_ms y nunca el token acuñado", async () => {
    const aws = new FakeDoctorAws(runningWorld("microvm-a"));
    const { results, doctorContext } = await runAll(aws);
    for (const result of results) {
      expect(typeof result.details.elapsed_ms).toBe("number");
    }
    expect(JSON.stringify(results)).not.toContain(JWE);
    expect(doctorContext.mintedToken).toBe(JWE);
  });
});

describe("credentials", () => {
  it("un token caducado es FAIL con el código de AWS", async () => {
    const aws = new FakeDoctorAws();
    aws.identity = new AwsOperationError("GetCallerIdentity", "ExpiredToken", "expired");
    const { results } = await runAll(aws);
    expect(check(results, "credentials").status).toBe("FAIL");
    expect(check(results, "credentials").summary).toContain("ExpiredToken");
    expect(check(results, "iam-simulation").status).toBe("SKIP");
  });

  it("una región fuera de las soportadas avisa", async () => {
    const aws = new FakeDoctorAws();
    aws.region = "sa-east-1";
    const outcome = await runCheck(spec("credentials"), aws, context());
    expect(outcome.status).toBe("WARN");
    expect(outcome.summary).toContain("sa-east-1");
  });

  it("principalKind reconoce los tipos de principal", () => {
    expect(principalKind(ROLE_SESSION_ARN)).toBe("assumed-role");
    expect(principalKind("arn:aws:sts::123456789012:federated-user/bob")).toBe("federated-user");
    expect(principalKind("arn:aws:iam::123456789012:root")).toBe("root");
    expect(principalKind(USER_ARN)).toBe("user");
    expect(principalKind("garbage")).toBe("other");
  });
});

describe("managed-images y quotas", () => {
  it("la imagen base ausente avisa y un permiso denegado es FAIL con su operación", async () => {
    const aws = new FakeDoctorAws();
    aws.managedArns = [];
    const absent = await runCheck(spec("managed-images"), aws, context());
    expect(absent.status).toBe("WARN");
    expect(absent.summary).toContain(MANAGED_BASE_IMAGE_NAME);
    aws.managedArns = denied("ListManagedMicrovmImages");
    const refused = await runCheck(spec("managed-images"), aws, context());
    expect(refused.status).toBe("FAIL");
    expect(refused.summary).toContain("ListManagedMicrovmImages denegado");
    expect(refused.details).toMatchObject({
      action: "lambda:ListManagedMicrovmImages",
      operation: "ListManagedMicrovmImages",
    });
  });

  it("las cuotas denegadas se saltan y las que no se listan avisan", async () => {
    const aws = new FakeDoctorAws();
    aws.quotas = denied("ListServiceQuotas");
    const refused = await runCheck(spec("quotas"), aws, context());
    expect(refused.status).toBe("SKIP");
    expect(refused.details.action).toBe("servicequotas:ListServiceQuotas");
    aws.quotas = defaultQuotas().filter((quota) => quota.code !== "L-F8BECE9C");
    const missing = await runCheck(spec("quotas"), aws, context());
    expect(missing.status).toBe("WARN");
    expect(missing.summary).toContain("L-F8BECE9C");
  });

  it("un peer ausente se salta la comprobación avisando, no falla la cuenta", async () => {
    const aws = new FakeDoctorAws();
    aws.quotas = new MissingPeerError("falta '@aws-sdk/client-service-quotas'", new Error("x"));
    const outcome = await runCheck(spec("quotas"), aws, context());
    expect(outcome.status).toBe("SKIP");
    expect(outcome.summary).toContain("@aws-sdk/client-service-quotas");
  });
});

describe("iam-simulation", () => {
  it("la denegación explícita o por SCP es FAIL", async () => {
    const aws = new FakeDoctorAws();
    aws.decisions = { "lambda:RunMicrovm": "explicitDeny" };
    const explicit = await runCheck(spec("iam-simulation"), aws, withIdentity());
    expect(explicit.status).toBe("FAIL");
    expect(explicit.summary).toContain("lambda:RunMicrovm");
    aws.decisions = {};
    aws.scpDenied = new Set(["s3:PutObject"]);
    const byScp = await runCheck(spec("iam-simulation"), aws, withIdentity());
    expect(byScp.status).toBe("FAIL");
    expect(byScp.details.denied_by_organizations).toEqual(["s3:PutObject"]);
  });

  it("root no se simula y sin GetRole se salta", async () => {
    const aws = new FakeDoctorAws();
    const root = await runCheck(
      spec("iam-simulation"),
      aws,
      withIdentity("arn:aws:iam::123456789012:root"),
    );
    expect(root.status).toBe("SKIP");
    expect(root.summary).toContain("root");
    aws.roleArns = Object.fromEntries([["AWSReservedSSO_Admin_abc", denied("GetRole")]]);
    const noGetRole = await runCheck(spec("iam-simulation"), aws, withIdentity(ROLE_SESSION_ARN));
    expect(noGetRole.status).toBe("SKIP");
    expect(noGetRole.details.action).toBe("iam:SimulatePrincipalPolicy");
  });

  it("un assumed-role simula con el ARN del rol con su path", async () => {
    const aws = new FakeDoctorAws();
    const outcome = await runCheck(spec("iam-simulation"), aws, withIdentity(ROLE_SESSION_ARN));
    expect(outcome.status).toBe("OK");
    expect(outcome.details.policy_source_arn).toBe(ROLE_ARN);
    expect(aws.calls).toContain("roleArn:AWSReservedSSO_Admin_abc");
  });

  it("sin bucket sólo simula los grupos de imagen, comodín y conector", async () => {
    const aws = new FakeDoctorAws();
    const outcome = await runCheck(
      spec("iam-simulation"),
      aws,
      withIdentity(USER_ARN, { bucket: undefined }),
    );
    expect(outcome.status).toBe("OK");
    expect(outcome.details.results).toHaveLength(
      contract.simulatedActions.image.length +
        contract.simulatedActions.wildcard.length +
        contract.simulatedActions.connector.length,
    );
  });

  it("con bucket simula los permisos del prefijo de artefactos y del propio bucket", async () => {
    const aws = new FakeDoctorAws();
    await runCheck(spec("iam-simulation"), aws, withIdentity());
    expect(aws.calls).toContain(
      `simulate:${USER_ARN}:arn:aws:s3:::${BUCKET}/${IMAGE_ARTIFACT_KEY_PREFIX}/*`,
    );
    expect(aws.calls).toContain(`simulate:${USER_ARN}:arn:aws:s3:::${BUCKET}`);
  });
});

describe("bucket", () => {
  it("sin bucket se salta y con bucket valida la región", async () => {
    const aws = new FakeDoctorAws();
    const bucket = spec("bucket");
    expect(bucket.deniedAction).toBe("s3:ListBucket");
    expect((await runCheck(bucket, aws, context({ bucket: undefined }))).status).toBe("SKIP");
    const same = await runCheck(bucket, aws, context());
    expect(same.status).toBe("OK");
    expect(same.details.bucket_region).toBe(REGION);
    aws.bucketFacts = { region: "eu-west-1" };
    const other = await runCheck(bucket, aws, context());
    expect(other.status).toBe("WARN");
    expect(other.summary).toContain("eu-west-1");
    aws.bucketFacts = { region: "" };
    const unknown = await runCheck(bucket, aws, context());
    expect(unknown.status).toBe("WARN");
    expect(unknown.summary).toContain("x-amz-bucket-region");
  });

  it("un 403 es FAIL y nombra los permisos que publish necesita", async () => {
    const aws = new FakeDoctorAws();
    aws.bucketFacts = new AwsOperationError("HeadBucket", "403", "Forbidden");
    const outcome = await runCheck(spec("bucket"), aws, context());
    expect(outcome.status).toBe("FAIL");
    expect(outcome.summary).toContain("HeadBucket 403");
    expect(outcome.summary).toContain("s3:ListBucket");
    expect(outcome.summary).toContain("s3:PutObject");
    expect(outcome.details.operation).toBe("HeadBucket");
  });
});

describe("image-gate", () => {
  it("cada estado de la puerta", async () => {
    const aws = new FakeDoctorAws();
    const gate = spec("image-gate");
    aws.image = { state: "UPDATED", latestActiveVersion: "3.0", latestFailedVersion: "2.0" };
    const lingering = await runCheck(gate, aws, context());
    expect(lingering.status).toBe("WARN");
    expect(lingering.summary).toContain("2.0");
    aws.image = { state: "UPDATED", latestActiveVersion: "3.0", latestFailedVersion: undefined };
    aws.imageVersion = { state: "SUCCESSFUL", status: "INACTIVE", stateReason: "ok" };
    const inactive = await runCheck(gate, aws, context());
    expect(inactive.status).toBe("FAIL");
    expect(inactive.summary).toContain("INACTIVE");
    expect(inactive.summary).toContain("'ok'");
    aws.image = {
      state: "UPDATED",
      latestActiveVersion: undefined,
      latestFailedVersion: undefined,
    };
    const noVersion = await runCheck(gate, aws, context());
    expect(noVersion.status).toBe("FAIL");
    expect(noVersion.summary).toContain("sin versión activa");
    aws.image = { state: "UPDATED", latestActiveVersion: "3.0", latestFailedVersion: undefined };
    aws.imageVersion = { state: "SUCCESSFUL", status: "ACTIVE", stateReason: undefined };
    const pinned = await runCheck(gate, aws, context({ templateVersion: "2.0" }));
    expect(pinned.status).toBe("OK");
    expect(pinned.details.version).toBe("2.0");
  });

  it("un fallo distinto de no encontrada sube como FAIL con su código", async () => {
    const aws = new FakeDoctorAws();
    aws.image = new AwsOperationError("GetMicrovmImage", "ThrottlingException", "slow");
    const outcome = await runCheck(spec("image-gate"), aws, context());
    expect(outcome.status).toBe("FAIL");
    expect(outcome.summary).toContain("ThrottlingException en GetMicrovmImage");
  });
});

describe("sandboxes, token y agente", () => {
  it("sandboxes: ninguno, uno vivo y más de diez", async () => {
    const none = await runCheck(spec("sandboxes"), new FakeDoctorAws(), context());
    expect(none.status).toBe("OK");
    const one = new FakeDoctorAws(runningWorld("microvm-a"));
    const doctorContext = context();
    const warn = await runCheck(spec("sandboxes"), one, doctorContext);
    expect(warn.status).toBe("WARN");
    expect(warn.details.running_ids).toEqual(["microvm-a"]);
    expect(doctorContext.targetSandbox?.sandboxId).toBe("microvm-a");
    const many = new FakeDoctorAws(
      runningWorld(...Array.from({ length: 11 }, (_, index) => `microvm-${index}`)),
    );
    const fail = await runCheck(spec("sandboxes"), many, context());
    expect(fail.status).toBe("FAIL");
    expect(fail.details.running_ids).toHaveLength(11);
  });

  it("token y agente van contra el sandbox RUNNING más reciente", async () => {
    const aws = new FakeDoctorAws(runningWorld("microvm-old", "microvm-new"));
    const { results } = await runAll(aws);
    expect(check(results, "sandboxes").status).toBe("WARN");
    const token = check(results, "token");
    expect(token.status).toBe("OK");
    expect(token.details).toMatchObject({ sandbox_id: "microvm-new", port: 8080, ttl_minutes: 60 });
    expect(check(results, "agent").status).toBe("OK");
    expect(check(results, "agent").details.agent_version).toBe(VERSION);
    expect(check(results, "compatibility").status).toBe("OK");
    expect(aws.probes).toEqual([{ sandboxId: "microvm-new", token: JWE }]);
    expect(aws.plane.tokens).toHaveLength(1);
    expect(aws.plane.tokens[0]?.sandboxId).toBe("microvm-new");
    const ports: readonly PortSpec[] = aws.plane.tokens[0]?.ports ?? [];
    expect(ports).toHaveLength(1);
  });

  it("avisos y fallos del agente", async () => {
    const aws = new FakeDoctorAws(runningWorld("microvm-a"));
    aws.health = healthy({ kernelReady: false, hookAnomalies: 2 });
    const warned = await runAll(aws);
    const agent = check(warned.results, "agent");
    expect(agent.status).toBe("WARN");
    expect(agent.summary).toContain("kernel_ready");
    expect(agent.summary).toContain("hook_anomalies=2");
    aws.health = new SandboxError("Health no respondió (UNAVAILABLE)");
    const failed = await runAll(aws);
    expect(check(failed.results, "agent").status).toBe("FAIL");
    expect(check(failed.results, "compatibility").status).toBe("SKIP");
    aws.health = healthy({ agentReady: false });
    const notReady = await runAll(aws);
    expect(check(notReady.results, "agent").summary).toBe("rayd responde pero no está agent_ready");
  });

  it("imds_blocked=false avisa sólo en una imagen -caps", async () => {
    const capsAws = new FakeDoctorAws(runningWorldOf("rayito-base-caps", "microvm-a"));
    const caps = await runAll(capsAws, { template: "rayito-base-caps" });
    expect(check(caps.results, "agent").summary).toContain(
      "imds_blocked=false en una imagen -caps",
    );
    const plain = await runAll(new FakeDoctorAws(runningWorld("microvm-a")));
    expect(check(plain.results, "agent").status).toBe("OK");
  });

  it("un token denegado es FAIL y el agente se salta", async () => {
    const aws = new FakeDoctorAws(runningWorld("microvm-a"));
    aws.plane.tokenFailure = new AuthenticationError("not authorized", {
      awsCode: constants.ACCESS_DENIED,
    });
    const { results } = await runAll(aws);
    const token = check(results, "token");
    expect(token.status).toBe("FAIL");
    expect(token.details.action).toBe("lambda:CreateMicrovmAuthToken");
    expect(check(results, "agent").status).toBe("SKIP");
  });

  it("sandbox no RUNNING: la sonda no corre y el agente se salta", async () => {
    const aws = new FakeDoctorAws(runningWorld("microvm-a"));
    aws.health = undefined;
    const { results } = await runAll(aws);
    expect(check(results, "agent").status).toBe("SKIP");
  });
});

describe("compatibilidad", () => {
  it("OK con la versión de imagen sólo informada, FAIL por debajo del mínimo y WARN por encima", async () => {
    const aws = new FakeDoctorAws(runningWorld("microvm-a"));
    const ok = await runAll(aws);
    const first = check(ok.results, "compatibility");
    expect(first.status).toBe("OK");
    expect(first.summary).toContain(`imagen ${IMAGE_VERSION}`);
    expect(first.details.image_version).toBe(IMAGE_VERSION);
    aws.health = healthy({ agentVersion: "0.0.9" });
    const old = check((await runAll(aws)).results, "compatibility");
    expect(old.status).toBe("FAIL");
    expect(old.summary).toContain("0.0.9");
    const [major, minor] = VERSION.split(".");
    aws.health = healthy({ agentVersion: `${major}.${Number(minor) + 1}.0` });
    expect(check((await runAll(aws)).results, "compatibility").status).toBe("WARN");
  });
});

describe("--launch", () => {
  function launched(): FakeLaunched {
    return new FakeLaunched(
      "microvm-launched",
      sandboxInfo({
        sandboxId: "microvm-launched",
        state: "RUNNING",
        endpoint: "abc.lambda-microvm.us-east-1.on.aws",
        template: "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base",
        templateVersion: IMAGE_VERSION,
        startedAt: new Date(STARTED),
        maximumDurationSeconds: 300,
      }),
    );
  }

  it("se mata aunque el agente falle, con los parámetros de Python", async () => {
    const sandbox = launched();
    const aws = new FakeDoctorAws();
    aws.launchResult = sandbox;
    aws.health = new SandboxError("boom");
    const { results, doctorContext } = await runAll(aws, { launch: true });
    expect(check(results, "token").status).toBe("OK");
    expect(check(results, "token").details.source).toBe("create()");
    expect(check(results, "agent").status).toBe("FAIL");
    expect(check(results, "agent").summary).toContain("boom");
    expect(doctorContext.launched?.sandboxId).toBe("microvm-launched");
    expect(sandbox.killed).toBe(1);
    expect(aws.launches).toEqual([
      {
        template: "rayito-base",
        templateVersion: undefined,
        timeoutMs: contract.launchTimeoutSeconds * 1000,
        metadata: contract.launchMetadata,
      },
    ]);
  });

  it("con el agente sano todo es OK y se mata una vez", async () => {
    const sandbox = launched();
    const aws = new FakeDoctorAws();
    aws.launchResult = sandbox;
    const { results } = await runAll(aws, { launch: true });
    expect(statuses(results).agent).toBe("OK");
    expect(statuses(results).compatibility).toBe("OK");
    expect(aws.probes).toEqual([{ sandboxId: "microvm-launched", token: undefined }]);
    expect(sandbox.killed).toBe(1);
  });

  it("un fallo al lanzar es un FAIL de token y no hay sandbox que matar", async () => {
    const aws = new FakeDoctorAws();
    aws.launchResult = new SandboxError("ServiceQuotaExceededException");
    const { results, doctorContext } = await runAll(aws, { launch: true });
    expect(check(results, "token").status).toBe("FAIL");
    expect(doctorContext.launched).toBeUndefined();
  });

  it("si no se puede matar, lo avisa con el id", async () => {
    const sandbox = launched();
    sandbox.killFailure = new Error("denied");
    const aws = new FakeDoctorAws();
    aws.launchResult = sandbox;
    const { warnings } = await runAll(aws, { launch: true });
    expect(warnings).toEqual(["no se pudo terminar el sandbox microvm-launched"]);
  });
});

describe("efs-network", () => {
  function inspector(subnets: SubnetFacts[]): NetworkInspector {
    const vpc: VpcFacts = {
      vpcId: "vpc-0123456789abcdef0",
      state: "available",
      dnsSupport: true,
      dnsHostnames: true,
    };
    const routes: RouteTableFacts[] = [
      { subnetIds: subnets.map((subnet) => subnet.subnetId), main: false, defaultRoute: "nat" },
    ];
    return {
      vpc: async () => vpc,
      subnets: async () => subnets,
      routeTables: async () => routes,
    };
  }

  function subnet(id: string, zone: string): SubnetFacts {
    return {
      subnetId: id,
      vpcId: "vpc-0123456789abcdef0",
      availabilityZone: zone,
      availableIps: 200,
      state: "available",
    };
  }

  const SubnetA = "subnet-0123456789abcdef0";
  const SubnetB = "subnet-0123456789abcdef1";
  const Vpc = "vpc-0123456789abcdef0";

  it("una VPC apta es OK e informa de lo que deploy crearía", async () => {
    const aws = new FakeDoctorAws();
    aws.inspector = inspector([subnet(SubnetA, "us-east-1a"), subnet(SubnetB, "us-east-1b")]);
    const outcome = await runCheck(
      EFS_NETWORK_CHECK,
      aws,
      context({ efsVpcId: Vpc, efsSubnetIds: `${SubnetA},${SubnetB}` }),
    );
    expect(outcome.name).toBe("efs-network");
    expect(outcome.status).toBe("OK");
    expect(outcome.summary).toContain("crearía");
    expect(outcome.details.creates).toContain("AWS::EFS::FileSystem");
    expect(outcome.details.idle_monthly).toBeTruthy();
  });

  it("dos subredes en una AZ fallan y no se crearía nada", async () => {
    const aws = new FakeDoctorAws();
    aws.inspector = inspector([subnet(SubnetA, "us-east-1a"), subnet(SubnetB, "us-east-1a")]);
    const outcome = await runCheck(
      EFS_NETWORK_CHECK,
      aws,
      context({ efsVpcId: Vpc, efsSubnetIds: `${SubnetA},${SubnetB}` }),
    );
    expect(outcome.status).toBe("FAIL");
    expect(outcome.summary).toContain("misma AZ");
    expect(outcome.summary).toContain("no crearía nada");
  });

  it("una sola AZ es un aviso", async () => {
    const aws = new FakeDoctorAws();
    aws.inspector = inspector([subnet(SubnetA, "us-east-1a")]);
    const outcome = await runCheck(
      EFS_NETWORK_CHECK,
      aws,
      context({ efsVpcId: Vpc, efsSubnetIds: SubnetA }),
    );
    expect(outcome.status).toBe("WARN");
    expect(outcome.summary).toContain("una sola AZ");
  });

  it("sin sus opciones no aparece ni llama a EC2; con ellas se añade tras las siete", async () => {
    const aws = new FakeDoctorAws();
    const without = await runAll(aws);
    expect(without.results.map((result) => result.name)).not.toContain("efs-network");
    expect(aws.calls).not.toContain("inspectEfsNetwork");
    aws.inspector = inspector([subnet(SubnetA, "us-east-1a"), subnet(SubnetB, "us-east-1b")]);
    const asked = await runAll(aws, { efsVpcId: Vpc, efsSubnetIds: `${SubnetA},${SubnetB}` });
    expect(asked.results[constants.PRE_LAUNCH_CHECK_COUNT]?.name).toBe("efs-network");
  });
});

describe("la orden", () => {
  async function invoke(aws: FakeDoctorAws, args: string[], env: Record<string, string> = {}) {
    const built = buildWorld({});
    return runWithServices({
      args,
      env,
      services: { ...built.services, doctor: aws },
    });
  }

  it("renderJson escribe exactamente doctorDocument", async () => {
    const aws = new FakeDoctorAws();
    const context = newDoctorContext({ template: "rayito-base", bucket: BUCKET });
    const results = await runDoctor(aws, context, () => undefined);
    const input = { region: REGION, context, results, exitCode: 0 };
    const terminal = new FakeTerminal();
    renderJson(new CliConsole(terminal), input);
    expect(JSON.parse(terminal.stdoutText)).toEqual(
      JSON.parse(JSON.stringify(doctorDocument(input))),
    );
    expect(Object.keys(doctorDocument(input))[0]).toBe("rayito");
  });

  it("--json es un documento con las diez comprobaciones y la tabla", async () => {
    const aws = new FakeDoctorAws();
    aws.image = undefined;
    const outcome = await invoke(aws, ["--json", "doctor", "--bucket", BUCKET]);
    expect(outcome.exitCode).toBe(1);
    const document = JSON.parse(outcome.stdout) as Record<string, unknown>;
    expect(Object.keys(document)).toEqual([
      "rayito",
      "region",
      "account",
      "principal_kind",
      "checks",
      "compatibility",
      "launched_sandbox_id",
      "exit_code",
    ]);
    expect(document.exit_code).toBe(1);
    expect(document.launched_sandbox_id).toBeNull();
    expect(document.account).toBe("123456789012");
    expect(document.principal_kind).toBe("user");
    expect(document.region).toBe(REGION);
    expect((document.compatibility as Array<{ sdk_series: string }>)[0]?.sdk_series).toBe("0.1");
  });

  it("la salida humana lista cada comprobación, la tabla y el resumen", async () => {
    const outcome = await invoke(new FakeDoctorAws(), ["doctor", "--bucket", BUCKET]);
    expect(outcome.exitCode).toBe(0);
    for (const name of CHECK_NAMES) {
      expect(outcome.stdout).toContain(name);
    }
    expect(outcome.stdout).toContain("OK   credentials");
    expect(outcome.stdout).toContain("SKIP token");
    expect(outcome.stdout).toContain("no comprobado");
    expect(outcome.stdout).toContain("rayito doctor: 7 OK, 0 WARN, 0 FAIL, 3 SKIP");
  });

  it("RAYITO_BUCKET sustituye a --bucket", async () => {
    const aws = new FakeDoctorAws();
    await invoke(aws, ["doctor"], { RAYITO_BUCKET: "from-env" });
    expect(aws.calls).toContain("headBucket:from-env");
  });

  it("los detalles de un WARN se indentan, salvo las tablas crudas", async () => {
    const aws = new FakeDoctorAws();
    aws.quotas = defaultQuotas({ "L-535CA9B6": 1 });
    const outcome = await invoke(aws, ["doctor"]);
    expect(outcome.stdout).toContain("       below_default: ");
    expect(outcome.stdout).not.toContain("       quotas: ");
  });

  it("las listas de los detalles se imprimen como listas de Python", async () => {
    const aws = new FakeDoctorAws();
    aws.decisions = { "lambda:SuspendMicrovm": "implicitDeny" };
    const outcome = await invoke(aws, ["doctor"]);
    expect(outcome.stdout).toContain("       explicit_deny: []");
    expect(outcome.stdout).toContain("       implicit_deny: ['lambda:SuspendMicrovm']");
  });

  it("--launch dice qué sandbox terminó", async () => {
    const aws = new FakeDoctorAws();
    aws.launchResult = new FakeLaunched(
      "microvm-launched",
      sandboxInfo({
        sandboxId: "microvm-launched",
        state: "RUNNING",
        endpoint: "abc.lambda-microvm.us-east-1.on.aws",
        template: "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base",
        templateVersion: IMAGE_VERSION,
        startedAt: new Date(STARTED),
        maximumDurationSeconds: 300,
      }),
    );
    const outcome = await invoke(aws, ["doctor", "--launch"]);
    expect(outcome.stdout).toContain("sandbox de --launch terminado: microvm-launched");
  });
});
