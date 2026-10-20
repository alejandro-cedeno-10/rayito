/**
 * El contexto de build de `--target agentcore` deja fuera los ficheros con
 * secretos (`.env`, `.env.*`, `.git`) igual que el despliegue de MicroVMs, y
 * ellos no cambian la etiqueta de la imagen.
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { AgentProject } from "../../src/agent/project.js";
import { planAgentCoreWith } from "../../src/agentcore/facade.js";

const SENSITIVE_PATHS = [
  ".env",
  ".env.local",
  ".git/config",
  "sub/.env",
  "sub/.env.local",
  "sub/.git/config",
];
const isSecretPath = (path: string): boolean =>
  path.split("/").some((part) => part === ".git" || part.startsWith(".env"));
const BUCKET_ENVIRON = { RAYITO_BUCKET: "amzn-s3-demo-bucket" };
const REGION = "us-east-1";
const MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";

const roots: string[] = [];

afterEach(() => {
  for (const root of roots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

function projectDocument(source: string): Record<string, unknown> {
  return {
    schema: "rayito.agent-project/1",
    name: "support-bot",
    runtime: { kind: "deepagents" },
    model: { provider: "bedrock", id: MODEL_ID, region: REGION, gateway: "bedrock" },
    gateways: {
      bedrock: {
        preset: "bedrock",
        secret: "rayito/bedrock-key",
        region: REGION,
        models: [MODEL_ID],
      },
    },
    source: { path: source },
  };
}

async function planWith(withSensitive: boolean) {
  const root = mkdtempSync(join(tmpdir(), "rayito-agentcore-"));
  roots.push(root);
  mkdirSync(join(root, "app"));
  writeFileSync(join(root, "app", "agent.py"), "print('hi')\n");
  if (withSensitive) {
    for (const relative of SENSITIVE_PATHS) {
      const target = join(root, "app", relative);
      mkdirSync(dirname(target), { recursive: true });
      writeFileSync(target, "not-a-real-value\n");
    }
  }
  const project = AgentProject.fromObject(projectDocument("app"), { baseDir: root });
  return planAgentCoreWith({ environ: BUCKET_ENVIRON }, root, { project, region: REGION });
}

describe("contexto de build de AgentCore", () => {
  it("deja fuera los ficheros con secretos y no cambian la etiqueta", async () => {
    const clean = await planWith(false);
    const dirty = await planWith(true);
    for (const planned of [clean, dirty]) {
      const paths = [...planned.tree.keys()];
      expect(paths).toContain("app/agent.py");
      expect(paths.filter(isSecretPath)).toEqual([]);
    }
    expect(dirty.plan.imageTag).toBe(clean.plan.imageTag);
  });
});
