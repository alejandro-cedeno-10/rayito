/**
 * Configuración cero de OpenCode (ADR-034 D8): un proyecto con sólo
 * `opencode.json(c)` y `AGENTS.md` despliega sus instrucciones y sus permisos;
 * lo que escribe `rayito-agent.toml` gana, lo que no se puede aplicar se avisa
 * y el informe nunca lleva el texto de los ficheros. Espejo de
 * `test_opencode_zero_config_files.py`.
 */

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, test } from "vitest";
import { stripJsonc } from "../../src/agent/detect-opencode.js";
import { OPENCODE_INSTRUCTIONS_PATH, OpenCodeRuntime } from "../../src/agent/opencode.js";
import { AgentProject } from "../../src/index.js";

const REGION = "us-east-1";
const MODEL = "amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0";
const INSTRUCTIONS = "# Reglas\n\nResponde en español y cita el fichero que lees.\n";
const GATEWAY_URL = "http://127.0.0.1:18001";
const DECODER = new TextDecoder();
const directories: string[] = [];

afterEach(() => {
  for (const directory of directories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function projectDir(
  config: Record<string, unknown> | string,
  agentsMd: string | null = INSTRUCTIONS,
): string {
  const directory = mkdtempSync(join(tmpdir(), "rayito-opencode-zero-"));
  directories.push(directory);
  const isText = typeof config === "string";
  writeFileSync(
    join(directory, isText ? "opencode.jsonc" : "opencode.json"),
    isText ? config : JSON.stringify({ model: MODEL, ...config }),
  );
  if (agentsMd !== null) {
    writeFileSync(join(directory, "AGENTS.md"), agentsMd);
  }
  return directory;
}

function served(project: AgentProject): Map<string, string> {
  const gatewayUrls = Object.fromEntries(
    Object.keys(project.gateways).map((name) => [name, GATEWAY_URL]),
  );
  const files = new OpenCodeRuntime().buildConfig(project.spec, { gatewayUrls, workdir: "/w" });
  return new Map(files.files.map((file) => [file.path, DECODER.decode(file.data)]));
}

function servedPermission(project: AgentProject): Record<string, unknown> {
  const [config] = served(project).values();
  return JSON.parse(config ?? "{}").agent.build.permission;
}

async function detect(directory: string) {
  return AgentProject.detect(directory, undefined, { region: REGION });
}

describe("configuración cero de OpenCode", () => {
  test("AGENTS.md es el AGENTS.md que se sirve", async () => {
    const [project, report] = await detect(projectDir({}));
    expect(project.spec.instructions).toBe(INSTRUCTIONS);
    expect(served(project).get(OPENCODE_INSTRUCTIONS_PATH)).toBe(INSTRUCTIONS);
    expect(report?.toObject().evidence).toContainEqual({
      path: "AGENTS.md",
      rule: "file:AGENTS.md",
    });
  });

  test("los permisos llegan al opencode.json del runtime", async () => {
    const permission = { edit: "deny", bash: { "*": "deny", "git status": "allow" } };
    const [project, report] = await detect(projectDir({ permission }));
    const rules = servedPermission(project);
    expect(rules.edit).toBe("deny");
    expect(rules.bash).toEqual({ "*": "deny", "git status": "allow" });
    expect(report?.toObject().evidence).toContainEqual({
      path: "opencode.json",
      rule: "config:permission",
    });
    expect(report?.warnings).toEqual([]);
  });

  test("ask se sirve como deny con un aviso", async () => {
    const [project, report] = await detect(
      projectDir({ permission: { bash: { "*": "ask", ls: "allow" } } }),
    );
    expect(servedPermission(project).bash).toEqual({ "*": "deny", ls: "allow" });
    expect(report?.warnings.map((warning) => warning.code)).toEqual([
      "opencode_permission_ignored",
    ]);
  });

  test("el fichero de proyecto gana a AGENTS.md y a los permisos", async () => {
    const directory = projectDir({ permission: { edit: "deny" } });
    writeFileSync(
      join(directory, "rayito-agent.toml"),
      '[agent]\ninstructions = "Del fichero de proyecto."\n\n[agent.permissions]\ndefault = "deny"\n',
    );
    const [project] = await detect(directory);
    expect(project.spec.instructions).toBe("Del fichero de proyecto.");
    expect(project.spec.permissions.default).toBe("deny");
    expect(project.spec.permissions.tools.edit).toBeUndefined();
  });

  test("un instructions_file del fichero de proyecto gana a AGENTS.md", async () => {
    const directory = projectDir({ permission: { edit: "deny" } });
    writeFileSync(join(directory, "PROMPT.md"), "Otro prompt.\n");
    writeFileSync(
      join(directory, "rayito-agent.toml"),
      '[agent]\ninstructions_file = "PROMPT.md"\n',
    );
    const [project] = await detect(directory);
    expect(project.spec.instructions).toBe("Otro prompt.\n");
    expect(project.spec.permissions.tools.edit).toBe("deny");
  });

  test("una clave de permiso desconocida se avisa y se ignora", async () => {
    const [project, report] = await detect(
      projectDir({ permission: { edit: "deny", "Remote Tool": "allow" } }),
    );
    expect(project.spec.permissions.tools).toEqual({ edit: "deny" });
    expect(report?.warnings.map((warning) => [warning.code, warning.detail])).toEqual([
      [
        "opencode_permission_ignored",
        "opencode.json: permission.<clave> no es una herramienta con allow, ask o deny " +
          "(o patrones de ellas) y se ignora",
      ],
    ]);
  });

  test("un opencode.jsonc con comentarios y comas finales se lee", async () => {
    const config = `{\n  // modelo\n  "model": "${MODEL}",\n  /* permisos */ "permission": {"edit": "deny",},\n}\n`;
    const [project] = await detect(projectDir(config));
    expect(project.spec.permissions.tools.edit).toBe("deny");
  });

  test("un AGENTS.md vacío no da instrucciones", async () => {
    const [project, report] = await detect(projectDir({}, " \n"));
    expect(project.spec.instructions).toBeUndefined();
    expect(report?.evidence.every((item) => item.rule !== "file:AGENTS.md")).toBe(true);
  });

  test("sin AGENTS.md ni permisos el proyecto no cambia", async () => {
    const [project] = await detect(projectDir({}, null));
    expect(project.spec.instructions).toBeUndefined();
    expect(project.toObject().agent).toBeUndefined();
  });

  test("el informe nunca lleva AGENTS.md ni el texto de los permisos", async () => {
    const marker = "contenido-que-no-debe-salir";
    const [, report] = await detect(
      projectDir(
        { permission: { bash: { [`echo ${marker}`]: "allow" }, [marker]: "allow" } },
        `${marker}\n`,
      ),
    );
    expect(JSON.stringify(report?.toObject())).not.toContain(marker);
  });
});

describe("stripJsonc", () => {
  test.each([
    ['{"a": "x // y"} // fin', '{"a": "x // y"} '],
    ['{"a": "/* no */", /* sí */ "b": 1}', '{"a": "/* no */",   "b": 1}'],
    ['{"a": [1, 2,], "b": "\\",",}', '{"a": [1, 2], "b": "\\","}'],
    ['{"a": 1, // c\n}', '{"a": 1 \n}'],
  ])("sólo toca comentarios y comas finales: %s", (text, expected) => {
    expect(stripJsonc(text)).toBe(expected);
    expect(() => JSON.parse(stripJsonc(text))).not.toThrow();
  });
});
