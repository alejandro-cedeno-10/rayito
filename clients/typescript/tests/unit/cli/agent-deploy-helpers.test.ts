/**
 * Las ayudas de `rayito agent deploy` (`cli/agent-deploy.ts`,
 * `cli/agent-project-flags.ts`): el log compacto del build, los pares
 * `K=V`, el access token y su fichero, el texto del plan y de la detección, y
 * lo que las banderas cambian de un proyecto. Espejo de
 * `test_agent_deploy_cli.py`; nada toca AWS.
 */

import { mkdirSync, mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { planDeploy } from "../../../src/agent/deploy.js";
import { ScalingOptions } from "../../../src/agent/scaling-domain.js";
import {
  COMPACT_LOG_PROGRESS_LINES,
  CompactBuildLog,
  defaultToken,
  deployToken,
  describePlan,
  detectionLines,
  fleetToken,
  formatUsd,
  PairFlagError,
  parseFlagPairs,
  poolBackendFileFor,
} from "../../../src/cli/agent-deploy.js";
import {
  applyProjectFlags,
  applyServeFlags,
  scalingOf,
} from "../../../src/cli/agent-project-flags.js";
import type { AgentDeployFlags } from "../../../src/cli/ops-ports.js";
import { TOKEN_ENV_VAR, TokenFileError } from "../../../src/cli/tokens.js";
import { InvalidArgumentError } from "../../../src/errors.js";
import { AGENT_TOKEN_FILE_DEFAULT } from "../../../src/limits.js";
import { generateAccessToken } from "../../../src/payload.js";
import { project } from "../agent-project-fake.js";

const NO_FLAGS: AgentDeployFlags = {
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
};

function tempDirectory(): string {
  return mkdtempSync(join(tmpdir(), "rayito-agent-deploy-"));
}

describe("CompactBuildLog", () => {
  it("imprime alertas, progreso y un resumen", () => {
    const printed: string[] = [];
    const log = new CompactBuildLog((line) => printed.push(line));
    for (let position = 1; position <= COMPACT_LOG_PROGRESS_LINES * 2; position += 1) {
      log.log(position === 3 ? "ERROR: paquete roto" : `línea ${position}`);
    }
    log.finish();
    expect(printed[0]).toBe("ERROR: paquete roto");
    expect(printed[1]).toBe(`construyendo la imagen: ${COMPACT_LOG_PROGRESS_LINES} líneas de log`);
    expect(printed[printed.length - 1]).toContain(
      `build: ${COMPACT_LOG_PROGRESS_LINES * 2} líneas`,
    );
    expect(printed).toHaveLength(4);
  });

  it("calla si no hubo build", () => {
    const printed: string[] = [];
    new CompactBuildLog((line) => printed.push(line)).finish();
    expect(printed).toEqual([]);
  });
});

describe("parseFlagPairs", () => {
  it("devuelve un objeto y no repite el valor de un par mal formado", () => {
    expect(parseFlagPairs(["A=1", "B=dos=tres"], "--env")).toEqual({ A: "1", B: "dos=tres" });
    for (const bad of ["sin-igual", "=valor", "clave="]) {
      expect(() => parseFlagPairs([bad], "--secret")).toThrow(PairFlagError);
      try {
        parseFlagPairs([bad], "--secret");
      } catch (error) {
        expect((error as Error).message).not.toContain(bad);
        expect((error as Error).message).toContain("--secret");
      }
    }
  });
});

describe("deployToken", () => {
  it("el de RAYITO_ACCESS_TOKEN no se escribe en ningún sitio", () => {
    const token = generateAccessToken();
    expect(deployToken(undefined, { [TOKEN_ENV_VAR]: token })).toEqual({
      token,
      file: undefined,
    });
  });

  it("--token-file guarda uno nuevo o el del entorno", () => {
    const fromEnv = generateAccessToken();
    const created = join(tempDirectory(), "uno");
    const reused = join(tempDirectory(), "dos");
    const fresh = deployToken(created, {});
    const env = deployToken(reused, { [TOKEN_ENV_VAR]: fromEnv });
    expect(fresh.file).toBe(created);
    expect(readFileSync(created, "utf8")).toBe(fresh.token);
    expect(readFileSync(reused, "utf8")).toBe(fromEnv);
    expect(env.token).toBe(fromEnv);
  });

  it("un --token-file que ya existe es un error y no se toca", () => {
    const existing = join(tempDirectory(), "token");
    writeFileSync(existing, "viejo");
    expect(() => deployToken(existing, {})).toThrow(TokenFileError);
    expect(readFileSync(existing, "utf8")).toBe("viejo");
  });

  it("el fichero por defecto se crea privado y se reutiliza", () => {
    const target = join(tempDirectory(), ".rayito", "agent-token");
    const first = defaultToken(target);
    expect(defaultToken(target)).toBe(first);
    expect(statSync(target).mode & 0o777).toBe(0o600);
  });

  it("un fichero por defecto vacío es un error", () => {
    const target = join(tempDirectory(), "agent-token");
    writeFileSync(target, "\n");
    expect(() => defaultToken(target)).toThrow(TokenFileError);
  });

  it("las plazas calientes van a un fichero sólo si hay alguna", () => {
    const target = join(tempDirectory(), ".rayito", "agent-pool.json");
    expect(poolBackendFileFor(0, target)).toBeUndefined();
    expect(() => statSync(join(target, ".."))).toThrow();
    expect(poolBackendFileFor(2, target)).toBe(target);
    expect(statSync(join(target, "..")).mode & 0o777).toBe(0o700);
  });

  it("el directorio del estado se ignora a sí mismo para que el token no se suba", () => {
    const directory = join(tempDirectory(), ".rayito");
    defaultToken(join(directory, "agent-token"));
    poolBackendFileFor(1, join(directory, "agent-pool.json"));
    expect(readFileSync(join(directory, ".gitignore"), "utf8")).toBe("*\n");
  });

  it("un .gitignore que ya existe no se toca", () => {
    const directory = join(tempDirectory(), ".rayito");
    mkdirSync(directory);
    writeFileSync(join(directory, ".gitignore"), "propio\n");
    defaultToken(join(directory, "agent-token"));
    expect(readFileSync(join(directory, ".gitignore"), "utf8")).toBe("propio\n");
  });

  it("es relativo al directorio de trabajo", () => {
    expect(AGENT_TOKEN_FILE_DEFAULT).toBe(".rayito/agent-token");
  });
});

describe("fleetToken", () => {
  it("--token-file gana al entorno y sin ninguno no hay token", () => {
    const file = join(tempDirectory(), "token");
    writeFileSync(file, "desde-fichero\n");
    expect(fleetToken(file, { [TOKEN_ENV_VAR]: "desde-entorno" })).toBe("desde-fichero");
    expect(fleetToken(undefined, { [TOKEN_ENV_VAR]: "desde-entorno" })).toBe("desde-entorno");
    const previous = process.cwd();
    process.chdir(tempDirectory());
    try {
      expect(fleetToken(undefined, {})).toBeUndefined();
    } finally {
      process.chdir(previous);
    }
  });
});

describe("texto", () => {
  it("el plan nombra orígenes, nunca un nombre de secreto", async () => {
    const plan = await planDeploy(".", {
      project: project(),
      options: { scaling: new ScalingOptions({ maxVms: 2, warmVms: 1 }) },
      region: "us-east-1",
    });
    const text = describePlan(plan).join("\n");
    expect(text).toContain("región: us-east-1");
    expect(text).toContain("VMs: mínimo 1, máximo 2, calientes 1, sesiones shared");
    expect(text).toContain("secreto de la pasarela bedrock: el del proyecto");
    expect(text).not.toContain("bedrock-key");
    expect(text).toContain("coste de lista");
    expect(text).toContain("la memoria se fija en la imagen");
  });

  it("los dólares llevan coma decimal", () => {
    expect(formatUsd(0.1261)).toBe("$0,1261");
  });

  it("la detección omite listas de evidencias y valores vacíos", () => {
    expect(
      detectionLines({ language: "python", framework: "", port: 8000, evidence: ["a.toml"] }),
    ).toEqual(["detectado language: python", "detectado port: 8000"]);
    expect(detectionLines(undefined)).toEqual([]);
  });
});

describe("banderas sobre el proyecto", () => {
  it("el servicio cambia sólo lo que se pasó", () => {
    const the = project();
    expect(applyServeFlags(the, NO_FLAGS)).toBe(the);
    const changed = applyServeFlags(the, {
      ...NO_FLAGS,
      maxSessions: 3,
      idleSeconds: 120,
      timeoutMs: 600_000,
      port: 9100,
    });
    expect(changed.serve.maxConcurrency).toBe(3);
    expect(changed.serve.timeoutMs).toBe(600_000);
    expect(changed.serve.port).toBe(9100);
    expect(changed.serve.idle?.maxIdleSeconds).toBe(120);
  });

  it("--queue y --queue-timeout fijan la cola del servicio y la validan", () => {
    const changed = applyServeFlags(project(), { ...NO_FLAGS, queue: 4, queueTimeoutMs: 1000 });
    expect([changed.serve.queueMax, changed.serve.queueTimeoutMs]).toEqual([4, 1000]);
    expect(() => applyServeFlags(project(), { ...NO_FLAGS, queue: -1 })).toThrow(
      InvalidArgumentError,
    );
  });

  it("--memory pasa por el proyecto y lo valida", () => {
    expect(applyProjectFlags(project(), { ...NO_FLAGS, memory: 4096 }).memoryMib).toBe(4096);
    expect(() => applyProjectFlags(project(), { ...NO_FLAGS, memory: 128 })).toThrow(
      InvalidArgumentError,
    );
  });

  it("--env se suma al entorno del proyecto y rechaza un secreto", () => {
    const changed = applyProjectFlags(project(), { ...NO_FLAGS, env: { A: "b" } });
    expect(changed.env).toEqual({ A: "b" });
    expect(() => applyProjectFlags(project(), { ...NO_FLAGS, env: { API_KEY: "x" } })).toThrow(
      InvalidArgumentError,
    );
  });

  it("--allow-out fija la política de salida del proyecto", () => {
    const changed = applyProjectFlags(project(), { ...NO_FLAGS, allowOut: ["api.example.com"] });
    expect(changed.network?.allowOut).toEqual(["api.example.com"]);
  });

  it("el escalado de las banderas se apoya en el del proyecto", () => {
    expect(scalingOf(project(), NO_FLAGS)).toBeUndefined();
    const scaling = scalingOf(project(), {
      ...NO_FLAGS,
      maxVms: 3,
      warm: 1,
      sessions: "dedicated",
    });
    expect([scaling?.minVms, scaling?.maxVms, scaling?.warmVms, scaling?.sessions]).toEqual([
      1,
      3,
      1,
      "dedicated",
    ]);
    expect(() => scalingOf(project(), { ...NO_FLAGS, minVms: 3, maxVms: 2 })).toThrow(
      InvalidArgumentError,
    );
  });
});
