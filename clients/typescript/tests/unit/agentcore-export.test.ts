/**
 * El export `agentcore` (ADR-031, D9): el mismo árbol del export de 0.14 con el
 * argumento de build `AGENT_BASE_IMAGE` y, sólo para los frameworks que leen las
 * credenciales de variables de entorno, el ayudante que las lee del rol del
 * runtime. El export `docker` no cambia. `testdata/agent/export/` fija los
 * ficheros completos; aquí se comprueban las reglas.
 */

import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { renderExport, usesRawProtocol } from "../../src/agent/export.js";
import { AgentProject } from "../../src/agent/project.js";
import {
  AGENT_AWS_CREDENTIALS_REQUIREMENTS_SHA256,
  AGENT_EXPORT_BASE_IMAGE,
  AGENT_EXPORT_BASE_IMAGE_DIGEST,
} from "../../src/limits.js";
import { formatTemplate } from "../../src/template-text.js";
import { contract } from "./agentcore-vectors.js";

const SAMPLES = join(import.meta.dirname, "..", "..", "..", "..", "testdata", "agent-samples");
const REGION = "us-east-1";
const BASE_REFERENCE = `${AGENT_EXPORT_BASE_IMAGE}@${AGENT_EXPORT_BASE_IMAGE_DIGEST}`;

async function sample(path: string): Promise<AgentProject> {
  const [project] = await AgentProject.detect(join(SAMPLES, path), undefined, { region: REGION });
  return project;
}

function text(tree: Map<string, string | Uint8Array>, name: string): string {
  const value = tree.get(name);
  if (value === undefined) {
    throw new Error(`falta ${name} en el export`);
  }
  return typeof value === "string" ? value : new TextDecoder().decode(value);
}

describe("export agentcore", () => {
  it("lleva el argumento de la imagen base antes de FROM y el FROM lo usa", async () => {
    const project = await sample("python/deepagents");
    const dockerfile = text(await renderExport(project, "agentcore"), "Dockerfile").split("\n");
    const from = dockerfile.findIndex((line) => line.startsWith("FROM "));
    expect(dockerfile[from - 1]).toBe(`ARG AGENT_BASE_IMAGE=${BASE_REFERENCE}`);
    expect(dockerfile[from]).toBe(`FROM --platform=linux/arm64 \${AGENT_BASE_IMAGE}`);
  });

  it("el export docker no cambia: sin argumento y con la imagen fijada en FROM", async () => {
    const project = await sample("python/deepagents");
    const dockerfile = text(await renderExport(project, "docker"), "Dockerfile");
    expect(dockerfile).not.toContain("AGENT_BASE_IMAGE");
    expect(dockerfile).toContain(`FROM --platform=linux/arm64 ${BASE_REFERENCE}`);
  });

  it("el README de agentcore explica el despliegue, MMDSv2 y la seguridad", async () => {
    const readme = text(
      await renderExport(await sample("python/deepagents"), "agentcore"),
      "README.md",
    );
    expect(readme).toContain("rayito agent deploy <directorio> --target agentcore");
    expect(readme).toContain("requireMMDSV2");
    expect(readme).toContain("### Seguridad: sin pasarela de secretos");
    const docker = text(
      await renderExport(await sample("python/deepagents"), "docker"),
      "README.md",
    );
    expect(docker).not.toContain("AgentCore");
  });

  it("Vercel AI SDK lleva el ayudante de credenciales del rol, fijado por hash, antes del host", async () => {
    const project = await sample("typescript/vercel-ai-sdk");
    const tree = await renderExport(project, "agentcore");
    const credentials = contract.constants.export.awsCredentials as Record<string, string>;
    expect(tree.has(credentials.requirementsAsset ?? "")).toBe(true);
    expect(tree.has(credentials.scriptAsset ?? "")).toBe(true);
    const lines = text(tree, "Dockerfile").split("\n");
    const install = formatTemplate(credentials.installCommandTemplate ?? "", {
      sha256: AGENT_AWS_CREDENTIALS_REQUIREMENTS_SHA256,
      requirementsPath: credentials.requirementsPath ?? "",
      venvDir: credentials.venvDir ?? "",
    });
    const copyRequirements = lines.indexOf(
      `COPY ${credentials.requirementsAsset} ${credentials.requirementsPath}`,
    );
    expect(copyRequirements).toBeGreaterThan(0);
    expect(lines[copyRequirements + 1]).toBe(`RUN ${install}`);
    expect(lines[copyRequirements + 2]).toBe(
      `COPY ${credentials.scriptAsset} ${credentials.scriptPath}`,
    );
    expect(copyRequirements).toBeLessThan(
      lines.findIndex((line) => line.startsWith("COPY agent_host")),
    );
    const host = JSON.parse(text(tree, "host.json")) as Record<string, unknown>;
    expect(host.aws_env_credentials).toEqual({
      python: `${credentials.venvDir}/bin/python`,
      script: credentials.scriptPath,
    });
  });

  it("el export docker de Vercel AI SDK no lleva el ayudante", async () => {
    const tree = await renderExport(await sample("typescript/vercel-ai-sdk"), "docker");
    expect(tree.has("aws_env_credentials.py")).toBe(false);
    expect(tree.has("requirements-aws-credentials.txt")).toBe(false);
    expect(JSON.parse(text(tree, "host.json"))).not.toHaveProperty("aws_env_credentials");
  });

  it("los frameworks que usan el proveedor de credenciales de AWS no llevan el ayudante", async () => {
    for (const path of ["python/strands", "typescript/langgraph-js", "typescript/express-custom"]) {
      const tree = await renderExport(await sample(path), "agentcore");
      expect(tree.has("aws_env_credentials.py"), path).toBe(false);
    }
  });

  it("un proyecto de framework no es una app con protocol raw", async () => {
    expect(usesRawProtocol(await sample("python/deepagents"))).toBe(false);
  });
});
