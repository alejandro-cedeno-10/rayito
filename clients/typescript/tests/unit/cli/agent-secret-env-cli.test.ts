/**
 * `rayito agent deploy` con `--secret-env`, `--secret-env-from`,
 * `--create-secrets-from` y `--check-env` (ADR-033 D4) sobre dobles: las
 * banderas llegan tal cual a la petición, sin ellas la petición no cambia, y
 * crear el secreto de un `.env` enseña sólo nombres y exige `--yes` antes de
 * desplegar. Nada toca AWS.
 */

import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { applyProjectFlags } from "../../../src/cli/agent-project-flags.js";
import {
  CREATE_SECRETS_NEEDS_CONFIRMATION,
  CREATE_SECRETS_QUESTION,
} from "../../../src/cli/agent-secret-env.js";
import type { TerminalInput } from "../../../src/cli/interactive-terminal.js";
import type { AgentDeployFlags } from "../../../src/cli/ops-ports.js";
import { InvalidArgumentError } from "../../../src/errors.js";
import { project } from "../agent-project-fake.js";
import { FakeAgentProjects } from "./agent-project-fakes.js";
import { buildWorld } from "./case-world.js";
import { runWithServices } from "./run-case.js";

const WITH_TOKEN = { RAYITO_ACCESS_TOKEN: "fake-access-token" };
const EXIT_USAGE = 2;
const TOKEN_VALUE = "tk-value";

const TTY: TerminalInput = {
  isTTY: true,
  on: () => undefined,
  off: () => undefined,
  pause: () => undefined,
  resume: () => undefined,
};

async function invoke(
  projects: FakeAgentProjects,
  args: string[],
  terminal: { readonly stdin?: string; readonly tty?: boolean } = {},
) {
  const built = buildWorld({});
  const services = new Proxy(built.services, {
    get() {
      throw new Error("el comando no debía abrir la sesión de AWS");
    },
  });
  return runWithServices({
    args,
    services,
    agentProjects: projects,
    env: WITH_TOKEN,
    stdin: terminal.stdin,
    rawInput: terminal.tty === true ? TTY : undefined,
  });
}

function envFile(): string {
  const path = join(mkdtempSync(join(tmpdir(), "rayito-secret-env-")), "values.env");
  writeFileSync(
    path,
    `TICKETS_API_URL=https://tickets.example.com\nTICKETS_TOKEN=${TOKEN_VALUE}\n`,
  );
  return path;
}

function flags(extra: Partial<AgentDeployFlags>): AgentDeployFlags {
  return {
    port: undefined,
    timeoutMs: undefined,
    memory: undefined,
    maxSessions: undefined,
    idleSeconds: undefined,
    queue: undefined,
    queueTimeoutMs: undefined,
    env: {},
    allowOut: [],
    minVms: undefined,
    maxVms: undefined,
    warm: undefined,
    sessions: undefined,
    ...extra,
  };
}

describe("agent deploy con secret env", () => {
  it("lleva las banderas tal cual a la petición", async () => {
    const projects = new FakeAgentProjects();
    const path = envFile();
    const outcome = await invoke(projects, [
      "agent",
      "deploy",
      "--secret-env",
      "DATABASE_URL=app/db-url",
      "--secret-env-from",
      "app/config,prefix=APP_",
      "--create-secrets-from",
      path,
      "--check-env",
      "--yes",
    ]);
    expect(outcome.exitCode).toBe(0);
    const request = projects.deployRequests[0];
    expect(request?.flags.secretEnv).toEqual(["DATABASE_URL=app/db-url"]);
    expect(request?.flags.secretEnvFrom).toEqual(["app/config,prefix=APP_"]);
    expect(request?.settings.createSecretsFrom).toBe(path);
    expect(request?.settings.checkEnv).toBe(true);
    expect(outcome.stderr).toContain("TICKETS_API_URL, TICKETS_TOKEN");
    expect(outcome.stdout + outcome.stderr).not.toContain(TOKEN_VALUE);
  });

  it("crear el secreto de un .env sin --yes es un error de uso", async () => {
    const projects = new FakeAgentProjects();
    const outcome = await invoke(projects, ["agent", "deploy", "--create-secrets-from", envFile()]);
    expect(outcome.exitCode).toBe(EXIT_USAGE);
    expect(outcome.stderr).toContain(CREATE_SECRETS_NEEDS_CONFIRMATION);
    expect(projects.deployRequests).toEqual([]);
    expect(outcome.stdout + outcome.stderr).not.toContain(TOKEN_VALUE);
  });

  it("en una terminal pregunta y un sí despliega", async () => {
    const projects = new FakeAgentProjects();
    const outcome = await invoke(
      projects,
      ["agent", "deploy", "--create-secrets-from", envFile()],
      {
        stdin: "y\n",
        tty: true,
      },
    );
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toContain(CREATE_SECRETS_QUESTION);
    expect(projects.deployRequests).toHaveLength(1);
    expect(outcome.stdout + outcome.stderr).not.toContain(TOKEN_VALUE);
  });

  it("en una terminal un no es un error de uso y no despliega", async () => {
    const projects = new FakeAgentProjects();
    const outcome = await invoke(
      projects,
      ["agent", "deploy", "--create-secrets-from", envFile()],
      {
        stdin: "n\n",
        tty: true,
      },
    );
    expect(outcome.exitCode).toBe(EXIT_USAGE);
    expect(outcome.stderr).toContain(CREATE_SECRETS_NEEDS_CONFIRMATION);
    expect(projects.deployRequests).toEqual([]);
  });

  it("con --json no pregunta aunque haya terminal", async () => {
    const projects = new FakeAgentProjects();
    const outcome = await invoke(
      projects,
      ["--json", "agent", "deploy", "--create-secrets-from", envFile()],
      { stdin: "y\n", tty: true },
    );
    expect(outcome.exitCode).toBe(EXIT_USAGE);
    expect(outcome.stdout).not.toContain(CREATE_SECRETS_QUESTION);
    expect(projects.deployRequests).toEqual([]);
  });

  it("las banderas se aplican al proyecto y una mal formada no repite el valor", () => {
    const changed = applyProjectFlags(
      project(),
      flags({ secretEnv: ["DATABASE_URL=app/db-url"], secretEnvFrom: ["app/config"] }),
    );
    expect(changed.secretEnv).toEqual({ DATABASE_URL: "app/db-url" });
    expect(changed.secretEnvFrom.map((source) => source.secret)).toEqual(["app/config"]);
    expect(() =>
      applyProjectFlags(project(), flags({ secretEnv: ["OPENAI_API_KEY=sk-not-a-name"] })),
    ).toThrow(InvalidArgumentError);
    try {
      applyProjectFlags(project(), flags({ secretEnv: ["OPENAI_API_KEY=sk-not-a-name"] }));
    } catch (error) {
      expect((error as Error).message).not.toContain("sk-not-a-name");
    }
  });
});
