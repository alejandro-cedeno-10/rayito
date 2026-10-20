/**
 * Las decisiones y el flujo de `AgentCoreDeployer` (ADR-031, D5 y D6): cada
 * caso de `testdata/agentcore-deploy/apply-vectors.json`, que Python comparte,
 * sobre dobles de los puertos, y los flujos que los vectores no cubren
 * (primer despliegue, redespliegue sin cambios, build fallido, carrera de
 * creación, esperas).
 */

import { describe, expect, it } from "vitest";
import type { RuntimeDetails } from "../../src/agentcore/models.js";
import { AgentCoreDeployer, type DeployRequest } from "../../src/agentcore/service.js";
import { AgentCoreError } from "../../src/errors.js";
import {
  AGENT,
  BUCKET,
  clockOf,
  DESCRIPTION,
  desiredDetails,
  type FakeAccount,
  IMAGE_TAG,
  newAccount,
  operations,
  REGION,
  RUNTIME_ID,
  RUNTIME_NAME,
  samplePlan,
} from "./agentcore-fakes.js";
import { type ApplyCase, applyVectors } from "./agentcore-vectors.js";

function request(overrides: Partial<DeployRequest> = {}): DeployRequest {
  return {
    plan: samplePlan(),
    tree: new Map([["Dockerfile", "FROM scratch\n"]]),
    modelRegion: REGION,
    buildImage: undefined,
    buildTimeoutMs: 1_800_000,
    ...overrides,
  };
}

function deployer(account: FakeAccount): AgentCoreDeployer {
  return new AgentCoreDeployer(account.ports, clockOf(account));
}

function accountOf(testCase: ApplyCase): FakeAccount {
  const { stack, image, runtime, runtimeDetails } = testCase.input;
  const waiting = runtime !== null && ["CREATING", "UPDATING"].includes(runtime.status);
  const listed =
    runtime === null
      ? undefined
      : {
          runtimeId: runtime.agentRuntimeId,
          runtimeArn: desiredDetails().runtimeArn,
          runtimeName: runtime.agentRuntimeName,
          runtimeVersion: runtime.agentRuntimeVersion,
          status: runtime.status,
          description: runtime.description,
        };
  const details: RuntimeDetails | undefined =
    runtimeDetails === null || listed === undefined ? undefined : { ...listed, ...runtimeDetails };
  const account = newAccount({
    stack: {
      status: stack?.status,
      outputs: stack?.outputs ?? {},
      settlesTo: stack?.status === "UPDATE_IN_PROGRESS" ? "UPDATE_COMPLETE" : undefined,
    },
    runtime: {
      listed,
      details,
      settled: waiting ? desiredDetails() : undefined,
      conflictOnCreate: testCase.input.conflictOnCreate ?? false,
    },
    image: { exists: image?.exists ?? false, sizeBytes: image?.sizeBytes },
  });
  if (waiting) {
    account.control.settle();
  }
  return account;
}

function planOf(testCase: ApplyCase) {
  const { desired } = testCase.input;
  return samplePlan({
    imageTag: desired.imageTag,
    environment: desired.environmentVariables,
    lifecycle: {
      idleSessionTimeoutSeconds: desired.idleRuntimeSessionTimeout,
      maxSessionLifetimeSeconds: desired.maxLifetime,
    },
  });
}

describe("decisiones al aplicar", () => {
  for (const testCase of applyVectors) {
    it(testCase.name, async () => {
      const account = accountOf(testCase);
      const run = deployer(account).apply(
        request({
          plan: planOf(testCase),
          buildImage: testCase.input.buildImage ?? undefined,
        }),
      );
      const { error } = testCase.expected;
      if (error !== undefined) {
        const failure = await run.then(
          () => undefined,
          (thrown: unknown) => thrown,
        );
        expect(failure).toBeInstanceOf(AgentCoreError);
        expect((failure as AgentCoreError).code).toBe(error.code);
        for (const [field, value] of Object.entries(error)) {
          if (field !== "code") {
            expect((failure as AgentCoreError).message).toContain(String(value));
          }
        }
        return;
      }
      const result = await run;
      const actions = Object.fromEntries(result.steps.map((step) => [step.resource, step.action]));
      expect(actions.runtime).toBe(testCase.expected.runtime);
      expect(actions.image).toBe(testCase.expected.image);
      const stackAction =
        testCase.expected.stack === "update" ? ["update", "reuse"] : [testCase.expected.stack];
      expect(stackAction).toContain(actions.stack);
    });
  }
});

describe("despliegue completo", () => {
  it("el primer despliegue crea la pila, construye la imagen y crea el runtime con MMDSv2", async () => {
    const account = newAccount();
    const result = await deployer(account).apply(request());
    expect(result.steps.map((step) => `${step.resource}:${step.action}`)).toEqual([
      "stack:create",
      "image:build",
      "runtime:create",
    ]);
    expect(operations(account)).toEqual([
      "GetCallerIdentity",
      "DescribeStacks",
      "CreateStack",
      "WaitStack",
      "DescribeStacks",
      "PutObject",
      "StartBuild",
      "BatchGetBuilds",
      "BatchGetBuilds",
      "DeleteObject",
      "DescribeImages",
      "ListAgentRuntimes",
      "CreateAgentRuntime",
      "GetAgentRuntime",
      "GetAgentRuntime",
      "UpdateAgentRuntime",
      "GetAgentRuntime",
      "GetAgentRuntime",
    ]);
    const [create, update] = account.control.writes;
    expect(create?.runtimeName).toBe(RUNTIME_NAME);
    expect(create?.requireMMDSV2).toBeUndefined();
    expect(create?.description).toBe(DESCRIPTION);
    expect(create?.baseVersion).toBeUndefined();
    expect(update?.requireMMDSV2).toBe(true);
    expect(update?.baseVersion).toBe("1");
    expect(result.runtimeId).toBe(RUNTIME_ID);
    expect(result.logGroup).toBe(`/aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT`);
  });

  it("sube el zip al prefijo del agente con la etiqueta y lo borra al terminar el build", async () => {
    const account = newAccount();
    await deployer(account).apply(request());
    const key = `rayito/images/agentcore/${AGENT}/${IMAGE_TAG}.zip`;
    expect(account.source.puts).toEqual([key]);
    expect(account.source.deletes).toEqual([key]);
    expect(account.builds[0]).toMatchObject({
      sourceLocation: `${BUCKET}/${key}`,
      imageTag: IMAGE_TAG,
      projectName: `rayito-agentcore-${AGENT}`,
    });
    expect(account.builds[0]?.baseImage).toMatch(
      /^public\.ecr\.aws\/docker\/library\/python:3\.12-slim-bookworm@sha256:[0-9a-f]{64}$/,
    );
  });

  it("un redespliegue sin cambios no construye ni escribe el runtime", async () => {
    const account = newAccount({
      stack: { status: "UPDATE_COMPLETE" },
      image: { exists: true },
      runtime: { listed: desiredDetails(), details: desiredDetails() },
    });
    account.stacks.unchanged = true;
    const result = await deployer(account).apply(request());
    expect(result.steps.map((step) => step.action)).toEqual(["reuse", "reuse", "reuse"]);
    const names = operations(account);
    for (const forbidden of [
      "PutObject",
      "StartBuild",
      "CreateAgentRuntime",
      "UpdateAgentRuntime",
      "CreateStack",
    ]) {
      expect(names).not.toContain(forbidden);
    }
  });

  it("un cambio de código construye la imagen nueva y actualiza el runtime sin duplicarlo", async () => {
    const account = newAccount({
      stack: { status: "UPDATE_COMPLETE" },
      image: { exists: false },
      runtime: {
        listed: desiredDetails({ containerUri: `${"x"}:src-old` }),
        details: desiredDetails({ containerUri: "other:src-old" }),
      },
    });
    const result = await deployer(account).apply(request());
    expect(result.steps.map((step) => step.action)).toEqual(["update", "build", "update"]);
    expect(operations(account).filter((name) => name === "CreateAgentRuntime")).toHaveLength(0);
    expect(account.control.writes.at(-1)?.requireMMDSV2).toBe(true);
  });

  it("un build que falla borra el zip, no toca el runtime y nombra el build y su grupo de logs", async () => {
    const account = newAccount({ image: { buildStatus: "FAILED" } });
    const failure = await deployer(account)
      .apply(request())
      .catch((thrown: unknown) => thrown);
    expect(failure).toBeInstanceOf(AgentCoreError);
    expect((failure as AgentCoreError).code).toBe("image_build_failed");
    expect((failure as AgentCoreError).message).toContain("FAILED");
    expect((failure as AgentCoreError).message).toContain("/aws/codebuild/rayito-agentcore-");
    expect(account.source.deletes).toHaveLength(1);
    expect(operations(account)).not.toContain("CreateAgentRuntime");
  });

  it("un build que no termina a tiempo es image_build_timeout", async () => {
    const account = newAccount({ image: { buildStatus: "IN_PROGRESS" } });
    const failure = await deployer(account)
      .apply(request({ buildTimeoutMs: 1 }))
      .catch((thrown: unknown) => thrown);
    expect((failure as AgentCoreError).code).toBe("image_build_timeout");
    expect(account.source.deletes).toHaveLength(1);
  });

  it("si otro despliegue crea el runtime a la vez, lo actualiza en vez de duplicarlo", async () => {
    const account = newAccount({
      stack: { status: "UPDATE_COMPLETE" },
      image: { exists: true },
      runtime: { conflictOnCreate: true },
    });
    const result = await deployer(account).apply(request());
    expect(result.steps.at(-1)?.action).toBe("update");
    expect(operations(account).filter((name) => name === "CreateAgentRuntime")).toHaveLength(1);
    expect(operations(account)).toContain("UpdateAgentRuntime");
  });

  it("una imagen de más de 2 GB se rechaza antes de tocar el runtime", async () => {
    const account = newAccount({ image: { sizeBytes: 2_000_000_001 } });
    const failure = await deployer(account)
      .apply(request())
      .catch((thrown: unknown) => thrown);
    expect((failure as AgentCoreError).code).toBe("image_too_large");
    expect(operations(account)).not.toContain("CreateAgentRuntime");
  });

  it("pasa a la pila los ARN de los modelos y de los secretos legibles", async () => {
    const account = newAccount();
    await deployer(account).apply(
      request({
        plan: samplePlan({ readableSecrets: ["rayito/agents/support/db"] }),
      }),
    );
    const input = account.calls.find((call) => call.operation === "CreateStack")?.input as {
      parameters: Record<string, string>;
      tags: Record<string, string>;
    };
    expect(input.parameters.BedrockModelArns).toBe(
      "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0," +
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
    );
    expect(input.parameters.ReadableSecretArns).toBe(
      "arn:aws:secretsmanager:us-east-1:123456789012:secret:rayito/agents/support/db-??????",
    );
    expect(input.parameters.ArtifactBucket).toBe(BUCKET);
    expect(input.tags["rayito:component"]).toBe("agentcore-runtime");
  });

  it("sin bucket ni descubrimiento es no_bucket, y uno de otra región es bucket_region", async () => {
    const noBucket = newAccount();
    const missing = await deployer(noBucket)
      .apply(request({ plan: samplePlan({ bucket: null, bucketSource: "discovered" }) }))
      .catch((thrown: unknown) => thrown);
    expect((missing as AgentCoreError).code).toBe("no_bucket");

    const other = newAccount();
    other.source.region = "eu-west-1";
    const wrong = await deployer(other)
      .apply(request())
      .catch((thrown: unknown) => thrown);
    expect((wrong as AgentCoreError).code).toBe("bucket_region");
    expect((wrong as AgentCoreError).message).toContain("eu-west-1");
    expect(operations(other)).not.toContain("CreateStack");
  });

  it("descubre el bucket sólo cuando el plan no lo nombra", async () => {
    const account = newAccount();
    account.discovered = BUCKET;
    await deployer(account).apply(
      request({ plan: samplePlan({ bucket: null, bucketSource: "discovered" }) }),
    );
    expect(account.source.puts).toHaveLength(1);
  });

  it("--no-build sin pila falla antes de crear nada", async () => {
    const account = newAccount();
    const failure = await deployer(account)
      .apply(request({ buildImage: false }))
      .catch((thrown: unknown) => thrown);
    expect((failure as AgentCoreError).code).toBe("image_missing");
    expect(operations(account)).not.toContain("CreateStack");
  });

  it("una pila con una operación en curso se espera y después se actualiza", async () => {
    const account = newAccount({
      stack: { status: "UPDATE_IN_PROGRESS", settlesTo: "UPDATE_COMPLETE" },
      image: { exists: true },
      runtime: { listed: desiredDetails(), details: desiredDetails() },
    });
    const result = await deployer(account).apply(request());
    expect(result.steps[0]).toMatchObject({ action: "wait", status: "UPDATE_IN_PROGRESS" });
    const names = operations(account);
    expect(names.indexOf("WaitStack")).toBeLessThan(names.indexOf("UpdateStack"));
  });

  it("una pila que falla al desplegarse es stack_failed", async () => {
    const account = newAccount();
    account.stacks.failWait = true;
    const failure = await deployer(account)
      .apply(request())
      .catch((thrown: unknown) => thrown);
    expect((failure as AgentCoreError).code).toBe("stack_failed");
  });

  it("un runtime que queda en UPDATE_FAILED tras actualizar es runtime_failed con su grupo de logs", async () => {
    const account = newAccount({
      stack: { status: "UPDATE_COMPLETE" },
      image: { exists: true },
      runtime: {
        listed: desiredDetails({ environmentVariables: {} }),
        details: desiredDetails({ environmentVariables: {} }),
        failOnSettle: "UPDATE_FAILED",
      },
    });
    const failure = await deployer(account)
      .apply(request())
      .catch((thrown: unknown) => thrown);
    expect((failure as AgentCoreError).code).toBe("runtime_failed");
    expect((failure as AgentCoreError).message).toContain(RUNTIME_ID);
  });

  it("el progreso son las líneas del contrato en español, sin ARN ni URL", async () => {
    const account = newAccount();
    const steps: { kind: string; detail: string }[] = [];
    await deployer(account).apply(request({ onStep: (step) => steps.push(step) }));
    expect(steps.map((step) => step.kind)).toEqual([
      "stackDeploy",
      "stackReady",
      "sourceUpload",
      "buildStart",
      "sourceDeleted",
      "imageReady",
      "runtimeCreate",
      "runtimeRequireMmdsV2",
      "runtimeReady",
    ]);
    for (const step of steps) {
      expect(step.detail).not.toMatch(/arn:|https?:/);
    }
  });
});
