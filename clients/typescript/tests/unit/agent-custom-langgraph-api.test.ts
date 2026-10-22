/**
 * `new HttpApp({ protocol: "langgraph-api" })` (ADR-033 D5): validación de
 * `assistantId`, la tabla `[runtime.app]` canónica, lo que el SDK manda al host
 * y el camino desde la detección con `--protocol langgraph-api` hasta la
 * configuración del host. Comparte
 * `testdata/agent-langgraph-api/host-config.json` con
 * `test_agent_custom_http_app_langgraph.py`; el host (Python) se prueba allí.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, test } from "vitest";
import { CustomRuntime, HTTP_APP_PROTOCOLS, HttpApp } from "../../src/agent/custom.js";
import { detect } from "../../src/agent/detect-domain.js";
import { LocalProjectTree } from "../../src/agent/detect-fs.js";
import { AgentModel, AgentSpec } from "../../src/agent/domain.js";
import { directWiring } from "../../src/agent/runtime.js";
import { InvalidArgumentError } from "../../src/errors.js";
import { AGENT_LANGGRAPH_HEALTH_PATH, AGENT_LANGGRAPH_INVOKE_PATH } from "../../src/limits.js";

const TESTDATA = join(import.meta.dirname, "..", "..", "..", "..", "testdata");
const DETECTION_CASE = join(
  TESTDATA,
  "agent-detection",
  "cases",
  "python-langgraph-json-langgraph-api-override",
);
const HOST_ONLY_KEYS = new Set(["workdir", "model", "app_dir", "path_prepend"]);

interface ValidCase {
  readonly name: string;
  readonly app: Record<string, unknown>;
  readonly canonical: Record<string, unknown>;
  readonly hostOptions: Record<string, unknown>;
}

interface InvalidCase {
  readonly name: string;
  readonly app: Record<string, unknown>;
}

const VECTORS = JSON.parse(
  readFileSync(join(TESTDATA, "agent-langgraph-api", "host-config.json"), "utf8"),
) as { readonly cases: ValidCase[]; readonly invalid: InvalidCase[] };

const spec = (): AgentSpec =>
  new AgentSpec({
    model: new AgentModel({ provider: "bedrock", id: "m", gateway: "gw", region: "us-east-1" }),
  });

function hostOptions(runtime: CustomRuntime): Record<string, unknown> {
  const host = runtime.hostConfig(spec(), { wiring: directWiring(), workdir: "/home/user" });
  return Object.fromEntries(
    Object.entries(host.options).filter(([key]) => !HOST_ONLY_KEYS.has(key)),
  );
}

describe("HttpApp con langgraph-api", () => {
  test.each(VECTORS.cases.map((item) => [item.name, item] as const))(
    "la tabla y las opciones del host siguen el vector: %s",
    (_name, item) => {
      const app = HttpApp.fromMapping(item.app);
      expect(app.toMapping()).toEqual(item.canonical);
      expect(HttpApp.fromMapping(app.toMapping())).toEqual(app);
      expect(hostOptions(new CustomRuntime({ app }))).toEqual(item.hostOptions);
    },
  );

  test.each(VECTORS.invalid.map((item) => [item.name, item] as const))(
    "una tabla inválida se rechaza: %s",
    (_name, item) => {
      expect(() => HttpApp.fromMapping(item.app)).toThrow(InvalidArgumentError);
    },
  );

  test("langgraph-api es un protocolo más, al final", () => {
    expect(HTTP_APP_PROTOCOLS).toEqual([
      "agentcore",
      "json",
      "openai-chat",
      "raw",
      "langgraph-api",
    ]);
  });

  test.each(["agentcore", "json", "openai-chat", "raw"] as const)(
    "los demás protocolos conservan sus rutas y su tabla: %s",
    (protocol) => {
      const app = new HttpApp({ command: ["x"], port: 9100, protocol });
      expect(app.assistantId).toBeUndefined();
      expect([app.servedHealthPath, app.servedInvokePath]).toEqual([
        app.healthPath,
        app.invokePath,
      ]);
      expect(app.toMapping()).not.toHaveProperty("assistant_id");
      expect(hostOptions(new CustomRuntime({ app }))).not.toHaveProperty("assistant_id");
    },
  );

  test("las rutas que usa el host con langgraph-api", () => {
    const app = new HttpApp({
      command: ["langgraph", "dev"],
      port: 8000,
      protocol: "langgraph-api",
      assistantId: "a",
    });
    expect(app.servedHealthPath).toBe(AGENT_LANGGRAPH_HEALTH_PATH);
    expect(app.servedInvokePath).toBe(AGENT_LANGGRAPH_INVOKE_PATH);
    expect(app.hidesOwnRoutes).toBe(false);
  });

  test.each([
    [AGENT_LANGGRAPH_HEALTH_PATH, false],
    ["/salud", true],
  ])("la salud %s de langgraph-api no es una ruta propia salvo otra", (healthPath, hidden) => {
    const app = new HttpApp({
      command: ["langgraph", "dev"],
      port: 8000,
      protocol: "langgraph-api",
      assistantId: "a",
      healthPath,
    });
    expect(app.hidesOwnRoutes).toBe(hidden);
    const other = new HttpApp({ command: ["x"], port: 8000, protocol: "agentcore", healthPath });
    expect(other.hidesOwnRoutes).toBe(true);
  });

  test("un proyecto detectado acaba en la configuración del host de langgraph-api", () => {
    const report = detect(new LocalProjectTree(join(DETECTION_CASE, "tree")), {
      protocol: "langgraph-api",
    });
    const table = report.runtimeTable();
    expect(table.kind).toBe("custom");
    const options = hostOptions(CustomRuntime.fromOptions(table));
    expect(options.protocol).toBe("langgraph-api");
    expect(options.assistant_id).toBe("agent");
    expect(options.health_path).toBe(AGENT_LANGGRAPH_HEALTH_PATH);
    expect(options.invoke_path).toBe(AGENT_LANGGRAPH_INVOKE_PATH);
    expect((options.command as string[]).slice(0, 2)).toEqual(["langgraph", "dev"]);
  });
});
