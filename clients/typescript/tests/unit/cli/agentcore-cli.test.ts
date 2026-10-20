/**
 * `rayito agent deploy|invoke|status|stop|list|delete --target agentcore`
 * (ADR-031): las órdenes sólo validan, imprimen y delegan en `AgentCoreTools`;
 * los textos y los documentos JSON son los de `testdata/agentcore-deploy/contract.json`.
 * Con dobles en memoria para el flujo y con el adaptador real (sin AWS) para el
 * plan.
 */

import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SdkAgentCoreTools } from "../../../src/cli/agentcore-adapter.js";
import type { TerminalInput } from "../../../src/cli/interactive-terminal.js";
import { contract, errorTemplate } from "../agentcore-vectors.js";
import { FakeAgentCoreTools } from "./agentcore-cli-fakes.js";
import { buildWorld } from "./case-world.js";
import { runWithServices } from "./run-case.js";

const SAMPLE = join(
  import.meta.dirname,
  "..",
  "..",
  "..",
  "..",
  "..",
  "testdata",
  "agent-samples",
  "python",
  "deepagents",
);
const ENV = { AWS_REGION: "us-east-1", RAYITO_BUCKET: "amzn-s3-demo-bucket" };
const TTY: TerminalInput = {
  isTTY: true,
  on: () => undefined,
  off: () => undefined,
  pause: () => undefined,
  resume: () => undefined,
};
const NO_AWS = new SdkAgentCoreTools(async () => {
  throw new Error("la CLI no debía construir la sesión de AWS");
});

async function run(
  args: string[],
  options: {
    tools?: FakeAgentCoreTools | SdkAgentCoreTools;
    stdin?: string;
    tty?: boolean;
    env?: Record<string, string>;
  } = {},
) {
  const tools = options.tools ?? new FakeAgentCoreTools();
  const outcome = await runWithServices({
    args,
    services: buildWorld({}).services,
    agentcore: tools,
    stdin: options.stdin,
    rawInput: options.tty === true ? TTY : undefined,
    env: options.env ?? ENV,
  });
  return { ...outcome, tools };
}

function documentOf(stdout: string): Record<string, unknown> {
  return JSON.parse(stdout) as Record<string, unknown>;
}

function calls(tools: FakeAgentCoreTools | SdkAgentCoreTools): string[] {
  return tools instanceof FakeAgentCoreTools ? tools.calls.map((call) => String(call[0])) : [];
}

describe("agent deploy --target agentcore", () => {
  it("--dry-run imprime el plan, la seguridad y el coste, y no despliega", async () => {
    const outcome = await run(["agent", "deploy", "--target", "agentcore", "--dry-run"]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toContain(
      "rayito agent deploy: support-bot en Amazon Bedrock AgentCore Runtime",
    );
    expect(outcome.stdout).toContain("Seguridad:");
    expect(outcome.stdout).toContain("AgentCore no tiene la pasarela de secretos de Rayito");
    expect(outcome.stdout).toContain("Avisos:");
    expect(outcome.stdout).toContain("Coste aproximado (us-east-1, precios de lista):");
    expect(outcome.stdout).toContain("no se creó ni cambió nada (--dry-run)");
    expect(calls(outcome.tools)).toEqual(["plan"]);
  });

  it("--dry-run --json da el documento del contrato", async () => {
    const outcome = await run(["--json", "agent", "deploy", "--target", "agentcore", "--dry-run"]);
    const document = documentOf(outcome.stdout);
    expect(Object.keys(document)).toEqual(contract.json.planDocumentKeys);
    expect(Object.keys(document.plan as object)).toEqual(contract.json.planKeys);
    expect(document.dryRun).toBe(true);
    expect(document.target).toBe("agentcore");
  });

  it("sin --yes y sin terminal pide confirmación y sale con 2", async () => {
    const outcome = await run(["--json", "agent", "deploy", "--target", "agentcore"]);
    expect(outcome.exitCode).toBe(2);
    expect(documentOf(outcome.stdout)).toEqual({
      error: { code: "needs_confirmation", message: errorTemplate("needs_confirmation") },
      exitCode: 2,
    });
    expect(outcome.stderr).toContain(`rayito: ${errorTemplate("needs_confirmation")}`);
    expect(calls(outcome.tools)).toEqual(["plan"]);
  });

  it("con --yes despliega, cuenta el progreso en stderr y deja las líneas del contrato", async () => {
    const outcome = await run(["agent", "deploy", "--target", "agentcore", "--yes"]);
    expect(outcome.exitCode).toBe(0);
    expect(calls(outcome.tools)).toEqual(["plan", "apply"]);
    const lines = outcome.stdout.split("\n");
    expect(lines).toContain("target=agentcore");
    expect(lines).toContain("name=support-bot");
    expect(lines).toContain("version=1");
    expect(lines.at(-2)).toBe(
      'invoca con: rayito agent invoke support-bot "<prompt>" --target agentcore',
    );
    expect(outcome.stderr).toContain("pila rayito-agentcore-support-bot: crear");
    expect(outcome.stderr).toContain("runtime rayito_support_bot: crear y exigir MMDSv2");
  });

  it("--json deja el documento de despliegue con las claves del contrato", async () => {
    const outcome = await run(["--json", "agent", "deploy", "--target", "agentcore", "--yes"]);
    const document = documentOf(outcome.stdout);
    expect(Object.keys(document)).toEqual(contract.json.deployKeys);
    expect((document.steps as object[]).length).toBe(3);
    expect(Object.keys((document.steps as object[])[0] as object)).toEqual(contract.json.stepKeys);
  });

  it("en una terminal pregunta y un no no despliega", async () => {
    const declined = await run(["agent", "deploy", "--target", "agentcore"], {
      stdin: "n\n",
      tty: true,
    });
    expect(declined.exitCode).toBe(1);
    expect(declined.stdout).toContain("¿Desplegar en AgentCore?");
    expect(calls(declined.tools)).toEqual(["plan"]);
    const accepted = await run(["agent", "deploy", "--target", "agentcore"], {
      stdin: "y\n",
      tty: true,
    });
    expect(accepted.exitCode).toBe(0);
    expect(calls(accepted.tools)).toEqual(["plan", "apply"]);
  });

  it("un fallo de AgentCore sale con su código de salida y su documento", async () => {
    const tools = new FakeAgentCoreTools();
    const { AgentCoreError } = await import("../../../src/errors.js");
    tools.applyError = new AgentCoreError("access_denied", {
      operation: "CreateStack",
      awsCode: "AccessDenied",
    });
    const outcome = await run(["--json", "agent", "deploy", "--target", "agentcore", "--yes"], {
      tools,
    });
    expect(outcome.exitCode).toBe(1);
    const error = documentOf(outcome.stdout).error as { code: string; message: string };
    expect(error.code).toBe("access_denied");
    expect(error.message).not.toMatch(/arn:|https?:/);
  });

  it("pasa a las opciones de AgentCore --tag, --bedrock-model, --readable-secret y los plazos en segundos", async () => {
    const outcome = await run([
      "agent",
      "deploy",
      "--target",
      "agentcore",
      "--dry-run",
      "--tag",
      "team=agents",
      "--bedrock-model",
      "amazon.nova-lite-v1:0",
      "--readable-secret",
      "rayito/agents/support/db",
      "--idle-session-timeout",
      "600",
      "--max-session-lifetime",
      "7200",
    ]);
    const tools = outcome.tools as FakeAgentCoreTools;
    const request = tools.calls[0]?.[1] as { agentcore: Record<string, unknown> };
    expect(request.agentcore).toEqual({
      bedrockModels: ["amazon.nova-lite-v1:0"],
      readableSecrets: ["rayito/agents/support/db"],
      idleSessionTimeoutMs: 600_000,
      maxSessionLifetimeMs: 7_200_000,
      tags: { team: "agents" },
    });
  });

  it("las opciones de AgentCore con el destino por defecto dan option_requires_agentcore y no llaman a nada", async () => {
    const outcome = await run(["agent", "deploy", "--tag", "team=agents"]);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toBe(
      `rayito: ${errorTemplate("option_requires_agentcore").replace("{option}", "--tag")}\n`,
    );
    expect(calls(outcome.tools)).toEqual([]);
  });

  it("un --target desconocido es un error de uso", async () => {
    const outcome = await run(["agent", "deploy", "--target", "docker"]);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("--target: admitidas: microvm, agentcore");
  });
});

describe("el plan con el adaptador real, sin AWS", () => {
  const base = ["agent", "deploy", SAMPLE, "--target", "agentcore", "--dry-run"];

  it("--dry-run de un proyecto real no construye la sesión de AWS", async () => {
    const outcome = await run(base, { tools: NO_AWS });
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toContain("rayito_");
    expect(outcome.stdout).toContain("Seguridad:");
  });

  it("--dry-run --json de un proyecto real lleva las claves del plan", async () => {
    const outcome = await run(["--json", ...base], { tools: NO_AWS });
    const document = documentOf(outcome.stdout);
    expect(Object.keys(document.plan as object)).toEqual(contract.json.planKeys);
  });

  it.each([
    ["--min-vms", "2"],
    ["--max-vms", "2"],
    ["--warm", "1"],
    ["--memory", "4096"],
    ["--port", "9100"],
    ["--timeout", "120"],
    ["--idle", "300"],
    ["--token-file", "token"],
    ["--keep-on-failure", ""],
    ["--secret", "bedrock=mi-clave"],
    ["--create-secret", ""],
  ])("%s no aplica a AgentCore y sale con 2 antes de llamar a AWS", async (flag, value) => {
    const extra = value === "" ? [flag] : [flag, value];
    const outcome = await run([...base, ...extra], { tools: NO_AWS });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain(`${flag} no aplica a --target agentcore`);
  });

  it("--allow-out es una política de red que AgentCore no aplica", async () => {
    const outcome = await run([...base, "--allow-out", "api.example.com"], { tools: NO_AWS });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("AgentCore no puede aplicar la política de salida");
  });

  it("un plazo fuera de rango es lifecycle_invalid y un par mal formado un error de uso", async () => {
    const lifecycle = await run([...base, "--idle-session-timeout", "30"], { tools: NO_AWS });
    expect(lifecycle.exitCode).toBe(2);
    expect(lifecycle.stderr).toContain(
      "idle_session_timeout debe ser un número entero de segundos",
    );
    const tags = await run([...base, "--tag", "sinvalor"], { tools: NO_AWS });
    expect(tags.exitCode).toBe(2);
  });

  it("sin región el plan lo dice con 2", async () => {
    const outcome = await run(base, {
      tools: NO_AWS,
      env: { RAYITO_BUCKET: "amzn-s3-demo-bucket" },
    });
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("región");
  });
});

describe("invoke, status y stop con --target agentcore", () => {
  it("invoke resuelve el agente por nombre, imprime el resultado y devuelve la sesión", async () => {
    const outcome = await run([
      "agent",
      "invoke",
      "support-bot",
      "hola",
      "--session-id",
      "s1",
      "--target",
      "agentcore",
    ]);
    expect(outcome.exitCode).toBe(0);
    const tools = outcome.tools as FakeAgentCoreTools;
    expect(tools.calls).toEqual([
      ["connect", "support-bot"],
      ["invoke", "hola", "s1"],
    ]);
    expect(outcome.stderr).toContain("session_id=");
  });

  it("invoke --stream imprime los eventos", async () => {
    const outcome = await run([
      "agent",
      "invoke",
      "support-bot",
      "hola",
      "--stream",
      "--target",
      "agentcore",
    ]);
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toContain("42");
  });

  it("invoke y status con --token-file dan option_not_supported", async () => {
    for (const command of ["invoke", "status"]) {
      const args = [
        "agent",
        command,
        "support-bot",
        ...(command === "invoke" ? ["hola"] : []),
        "--token-file",
        "t",
        "--target",
        "agentcore",
      ];
      const outcome = await run(args);
      expect(outcome.exitCode).toBe(2);
      expect(outcome.stderr).toContain("--token-file no aplica a --target agentcore");
    }
  });

  it("status imprime las líneas del contrato y --json las claves", async () => {
    const human = await run(["agent", "status", "support-bot", "--target", "agentcore"]);
    expect(human.stdout.split("\n").slice(0, 3)).toEqual([
      "target: agentcore",
      "name: support-bot",
      "runtime_id: rayito_support_bot-a1b2c3d4e5",
    ]);
    const json = await run(["--json", "agent", "status", "support-bot", "--target", "agentcore"]);
    expect(Object.keys(documentOf(json.stdout))).toEqual(contract.json.statusKeys);
  });

  it("stop exige --session-id y para sólo esa sesión", async () => {
    const missing = await run(["agent", "stop", "support-bot", "--target", "agentcore"]);
    expect(missing.exitCode).toBe(2);
    expect(missing.stderr).toContain(errorTemplate("session_required").slice(0, 40));
    const stopped = await run([
      "--json",
      "agent",
      "stop",
      "support-bot",
      "--session-id",
      "s1",
      "--target",
      "agentcore",
    ]);
    expect(documentOf(stopped.stdout)).toEqual({
      target: "agentcore",
      name: "support-bot",
      sessionId: "s1",
      stopped: true,
    });
    expect(Object.keys(documentOf(stopped.stdout))).toEqual(contract.json.stopKeys);
  });

  it("stop de una sesión que ya no estaba activa no imprime nada y sale con 0", async () => {
    const tools = new FakeAgentCoreTools();
    tools.client.stopped = false;
    const outcome = await run(
      ["agent", "stop", "support-bot", "--session-id", "s1", "--target", "agentcore"],
      { tools },
    );
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toBe("");
  });

  it("--session-id en stop con MicroVMs es option_requires_agentcore", async () => {
    const outcome = await run(["agent", "stop", "support-bot", "--session-id", "s1"]);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("--session-id sólo aplica a --target agentcore");
  });
});

describe("agent list y agent delete", () => {
  it("list imprime un agente por línea y --json las claves del contrato", async () => {
    const human = await run(["agent", "list", "--target", "agentcore"]);
    expect(human.stdout).toBe(
      "support-bot runtime=rayito_support_bot id=rayito_support_bot-a1b2c3d4e5 version=1 status=READY\n",
    );
    const json = await run(["--json", "agent", "list", "--target", "agentcore"]);
    const document = documentOf(json.stdout);
    expect(Object.keys(document)).toEqual(contract.json.listKeys);
    expect(Object.keys((document.agents as object[])[0] as object)).toEqual(
      contract.json.listItemKeys,
    );
  });

  it("list sin agentes lo dice, y list --target microvm es target_unsupported", async () => {
    const tools = new FakeAgentCoreTools();
    tools.agents = [];
    const empty = await run(["agent", "list", "--target", "agentcore"], { tools });
    expect(empty.stdout).toBe("no hay agentes de Rayito en AgentCore en us-east-1\n");
    const microvm = await run(["agent", "list", "--target", "microvm"]);
    expect(microvm.exitCode).toBe(2);
    expect(microvm.stderr).toContain("sólo aplica a --target agentcore");
  });

  it("delete sin --yes con --json sale con 2 y no borra nada", async () => {
    const outcome = await run([
      "--json",
      "agent",
      "delete",
      "support-bot",
      "--target",
      "agentcore",
    ]);
    expect(outcome.exitCode).toBe(2);
    expect(documentOf(outcome.stdout).error).toMatchObject({ code: "delete_needs_confirmation" });
    expect(calls(outcome.tools)).toEqual(["previewDelete"]);
  });

  it("delete --yes muestra lo que borra, lo borra y cuenta el resultado", async () => {
    const outcome = await run(["agent", "delete", "support-bot", "--target", "agentcore", "--yes"]);
    expect(outcome.exitCode).toBe(0);
    expect(calls(outcome.tools)).toEqual(["previewDelete", "delete"]);
    expect(outcome.stdout).toContain(
      "rayito agent delete: support-bot en AgentCore, región us-east-1",
    );
    expect(outcome.stdout).toContain(
      "borrado support-bot: runtime true, pila true, grupo de logs true",
    );
    const json = await run([
      "--json",
      "agent",
      "delete",
      "support-bot",
      "--target",
      "agentcore",
      "--yes",
    ]);
    expect(Object.keys(documentOf(json.stdout))).toEqual(contract.json.deleteKeys);
  });

  it("delete pregunta en una terminal", async () => {
    const declined = await run(["agent", "delete", "support-bot", "--target", "agentcore"], {
      stdin: "n\n",
      tty: true,
    });
    expect(declined.exitCode).toBe(1);
    expect(calls(declined.tools)).toEqual(["previewDelete"]);
  });

  it("delete de algo que no existe sale con 0 y lo dice", async () => {
    const tools = new FakeAgentCoreTools();
    tools.preview = {
      ...tools.preview,
      runtimeId: undefined,
      stackExists: false,
      logGroup: undefined,
    };
    const outcome = await run(["agent", "delete", "support-bot", "--target", "agentcore"], {
      tools,
    });
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toBe(
      "no hay ningún despliegue de Rayito llamado support-bot en us-east-1: nada que borrar\n",
    );
    expect(calls(tools)).toEqual(["previewDelete"]);
  });

  it.each([
    ["list", []],
    ["delete", ["support-bot"]],
  ])(
    "%s con --target microvm es el error target_unsupported con su documento JSON",
    async (command, extra) => {
      const outcome = await run(["--json", "agent", command, ...extra, "--target", "microvm"]);
      expect(outcome.exitCode).toBe(2);
      const document = documentOf(outcome.stdout);
      expect(Object.keys(document)).toEqual(["error", "exitCode"]);
      expect(document.exitCode).toBe(2);
      const error = document.error as { code: string; message: string };
      expect(error.code).toBe("target_unsupported");
      expect(error.message).toContain(`rayito agent ${command}`);
    },
  );
});
