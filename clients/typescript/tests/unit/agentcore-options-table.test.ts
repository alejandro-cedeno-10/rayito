/**
 * La tabla de opciones de MicroVMs que no aplican a AgentCore (ADR-031, D10):
 * cada fila con `call: "error"` del contrato lanza `option_not_supported` con su
 * motivo antes de cualquier llamada a AWS, y un valor igual al valor por
 * defecto no cuenta como puesto.
 */

import { describe, expect, it } from "vitest";
import { AgentDeployOptions } from "../../src/agent/deploy-plan.js";
import { ScalingOptions } from "../../src/agent/scaling-domain.js";
import { type CallFlags, checkCallOptions } from "../../src/agentcore/domain.js";
import { AgentCoreError } from "../../src/errors.js";
import type { DynamoDbIndex } from "../../src/index/dynamodb.js";
import type { PoolBackend } from "../../src/pool/backend.js";
import { contract, errorTemplate } from "./agentcore-vectors.js";

interface Case {
  readonly label: string;
  readonly options?: AgentDeployOptions;
  readonly flags?: CallFlags;
}

function scaling(values: Record<string, number>): ScalingOptions {
  const defaults = new ScalingOptions();
  return Object.assign(Object.create(ScalingOptions.prototype), { ...defaults, ...values });
}

const CASES: Case[] = [
  {
    label: "scaling.min_vms",
    options: new AgentDeployOptions({ scaling: scaling({ minVms: 2 }) }),
  },
  {
    label: "scaling.max_vms",
    options: new AgentDeployOptions({ scaling: scaling({ maxVms: 3 }) }),
  },
  {
    label: "scaling.warm_vms",
    options: new AgentDeployOptions({ scaling: scaling({ warmVms: 1 }) }),
  },
  {
    label: "scaling.scale_in_after_seconds",
    options: new AgentDeployOptions({ scaling: new ScalingOptions({ scaleInAfterMs: 120_000 }) }),
  },
  { label: "memory_mib", flags: { memoryMib: 4096 } },
  { label: "serve.port", flags: { port: 9100 } },
  { label: "serve.timeout_seconds", flags: { timeoutMs: 120_000 } },
  { label: "serve.idle", flags: { idleSeconds: 300 } },
  {
    label: "secret_names",
    options: new AgentDeployOptions({ secretNames: { bedrock: "mi-clave" } }),
  },
  {
    label: "create_secret_from_env",
    options: new AgentDeployOptions({ createSecretFromEnv: true }),
  },
  {
    label: "access_token",
    options: new AgentDeployOptions({ accessToken: "0123456789abcdef0123456789abcdef" }),
  },
  { label: "keep_on_failure", options: new AgentDeployOptions({ keepOnFailure: true }) },
  { label: "base_image_version", options: new AgentDeployOptions({ baseImageVersion: "3" }) },
  { label: "ensure_base_image", options: new AgentDeployOptions({ ensureBaseImage: false }) },
  {
    label: "base_publisher",
    options: new AgentDeployOptions({ basePublisher: async () => undefined }),
  },
  {
    label: "session_index",
    options: new AgentDeployOptions({ sessionIndex: {} as unknown as DynamoDbIndex }),
  },
  {
    label: "pool_backend",
    options: new AgentDeployOptions({ poolBackend: {} as unknown as PoolBackend }),
  },
  { label: "warm_timeout_seconds", options: new AgentDeployOptions({ warmTimeoutMs: 1000 }) },
];

function reasonOf(label: string): string {
  const row = contract.optionsTable.rows.find((candidate) =>
    candidate.option.split(" / ")[0]?.split(", ").includes(label),
  );
  if (row === undefined) {
    throw new Error(`la tabla del contrato no tiene ${label}`);
  }
  return row.reason;
}

describe("opciones de MicroVMs en AgentCore", () => {
  for (const testCase of CASES) {
    it(`${testCase.label} es option_not_supported con el motivo del contrato`, () => {
      const options = testCase.options ?? new AgentDeployOptions();
      let raised: unknown;
      try {
        checkCallOptions(options, testCase.flags ?? {});
      } catch (error) {
        raised = error;
      }
      expect(raised).toBeInstanceOf(AgentCoreError);
      const error = raised as AgentCoreError;
      expect(error.code).toBe("option_not_supported");
      expect(error.message).toBe(
        errorTemplate("option_not_supported")
          .replace("{option}", testCase.label)
          .replace("{reason}", reasonOf(testCase.label)),
      );
    });
  }

  it("los valores por defecto cuentan como no puestos", () => {
    expect(() =>
      checkCallOptions(new AgentDeployOptions({ scaling: new ScalingOptions() }), {
        minVms: 1,
        maxVms: 1,
        warmVms: 0,
      }),
    ).not.toThrow();
  });

  it("las opciones que aplican no dan error", () => {
    expect(() =>
      checkCallOptions(
        new AgentDeployOptions({
          bucket: "amzn-s3-demo-bucket",
          buildImage: false,
          buildTimeoutMs: 600_000,
          scaling: new ScalingOptions({ sessions: "dedicated" }),
        }),
      ),
    ).not.toThrow();
  });

  it("cada fila de error de la tabla tiene su caso", () => {
    const tested = new Set(CASES.map((testCase) => testCase.label));
    const rows = contract.optionsTable.rows.filter((row) => row.call === "error");
    const labels = rows.flatMap((row) => (row.option.split(" / ")[0] ?? "").split(", "));
    const labelLike = labels.filter((label) => /^[a-z_.]+$/.test(label));
    const untested = labelLike.filter((label) => !tested.has(label));
    expect(untested).toEqual([]);
  });
});
