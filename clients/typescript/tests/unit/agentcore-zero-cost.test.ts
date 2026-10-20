/**
 * El destino por defecto sigue siendo la flota de MicroVMs y no cuesta nada de
 * AgentCore (ADR-031, D13): sin `target: "agentcore"` no se importa el servicio
 * de AgentCore ni se carga ningún cliente de AgentCore, CodeBuild o ECR; con él,
 * el plan tampoco llama a AWS, y construir los adaptadores no carga ningún peer.
 */

import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { deployWith, planDeploy } from "../../src/agent/deploy.js";
import { AgentProject } from "../../src/agent/project.js";
import { sdkAgentCorePorts } from "../../src/agentcore/aws-ports.js";
import { AgentCoreFleet } from "../../src/agentcore/fleet.js";
import { SdkAgentCoreTools } from "../../src/cli/agentcore-adapter.js";
import { AgentCoreError, InvalidArgumentError } from "../../src/errors.js";
import { planVectors } from "./agentcore-vectors.js";

const observed = vi.hoisted(() => ({ peers: [] as string[], facadeImports: 0 }));

vi.mock("../../src/optional.js", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../src/optional.js")>();
  return {
    ...original,
    loadOptionalPeer: vi.fn((specifier: string, feature: string) => {
      observed.peers.push(specifier);
      return original.loadOptionalPeer(specifier, feature);
    }),
  };
});

vi.mock("../../src/agentcore/facade.js", async (importOriginal) => {
  observed.facadeImports += 1;
  return importOriginal();
});

const SRC = join(import.meta.dirname, "..", "..", "src");
const AGENTCORE_PEERS = [
  "@aws-sdk/client-bedrock-agentcore",
  "@aws-sdk/client-bedrock-agentcore-control",
  "@aws-sdk/client-codebuild",
  "@aws-sdk/client-ecr",
];
const BASE_DIR = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agentcore-deploy");

function project(): AgentProject {
  const [first] = planVectors;
  return AgentProject.fromObject((first as { project: Record<string, unknown> }).project, {
    baseDir: BASE_DIR,
  });
}

function sources(directory: string): string[] {
  return readdirSync(directory, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".ts"))
    .map((entry) => join(entry.parentPath, entry.name));
}

beforeEach(() => {
  observed.peers.length = 0;
});

describe("coste cero de AgentCore", () => {
  it("el plan del destino por defecto no importa AgentCore ni carga un cliente", async () => {
    observed.facadeImports = 0;
    await planDeploy(".", { project: project(), region: "us-east-1" });
    expect(observed.facadeImports).toBe(0);
    expect(observed.peers).toEqual([]);
  });

  it("el plan con target agentcore no llama a AWS ni carga ningún cliente", async () => {
    vi.stubEnv("RAYITO_BUCKET", "amzn-s3-demo-bucket");
    try {
      const plan = await planDeploy(".", {
        project: project(),
        region: "us-east-1",
        target: "agentcore",
      });
      expect(plan.target).toBe("agentcore");
      expect(observed.peers).toEqual([]);
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("construir los adaptadores, el cliente y la CLI no carga ningún peer", () => {
    const ports = sdkAgentCorePorts({ region: "us-east-1" });
    new AgentCoreFleet({
      name: "support-bot",
      region: "us-east-1",
      runtimeId: "rayito_support_bot-a1b2c3d4e5",
      runtimeArn: "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/x",
      runtimeVersion: "1",
      imageUri: "uri",
      ports,
    });
    new SdkAgentCoreTools(async () => {
      throw new Error("no debía construirse la sesión");
    });
    expect(observed.peers).toEqual([]);
  });

  it("las opciones de AgentCore con el destino por defecto fallan antes de cualquier llamada", async () => {
    const calls: string[] = [];
    const aws = new Proxy(
      {},
      {
        get: (_target, name) => {
          calls.push(String(name));
          throw new Error("no debía llamar a AWS");
        },
      },
    );
    const failure = await deployWith({ aws: aws as never }, ".", {
      project: project(),
      agentcore: { idleSessionTimeoutMs: 600_000 },
    }).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(AgentCoreError);
    expect((failure as AgentCoreError).code).toBe("option_requires_agentcore");
    expect(calls).toEqual([]);
    const planned = await planDeploy(".", {
      project: project(),
      agentcore: { tags: { team: "agents" } },
    }).catch((error: unknown) => error);
    expect((planned as AgentCoreError).code).toBe("option_requires_agentcore");
    expect(observed.peers).toEqual([]);
  });

  it("un target desconocido es un error de argumento", async () => {
    await expect(
      planDeploy(".", { project: project(), target: "docker" as never }),
    ).rejects.toBeInstanceOf(InvalidArgumentError);
  });
});

describe("los clientes de AgentCore sólo se importan en los adaptadores", () => {
  const Forbidden = new RegExp(
    `from\\s+["'](?:${AGENTCORE_PEERS.map((peer) => peer.replace(/[.*+?^${}()|[\]\\/-]/g, "\\$&")).join("|")})["']`,
  );

  it("ningún módulo los importa de forma estática", () => {
    for (const file of sources(SRC)) {
      expect(readFileSync(file, "utf8"), relative(SRC, file)).not.toMatch(Forbidden);
    }
  });

  it("el despliegue de MicroVMs no importa el servicio de AgentCore de forma estática", () => {
    const source = readFileSync(join(SRC, "agent", "deploy.ts"), "utf8");
    expect(source).not.toMatch(/from\s+["']\.\.\/agentcore\/(?:facade|aws|service)/);
    expect(source).toMatch(/import\("\.\.\/agentcore\/facade\.js"\)/);
  });
});
