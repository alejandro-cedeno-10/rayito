/**
 * Las seis herramientas del servidor MCP con el cliente oficial en memoria
 * contra el `rayd` falso y el plano de control falso: cada herramienta una a
 * una, el ciclo de vida del `SandboxLease`, la tabla de errores y la higiene
 * de logging. Espejo de `test_mcp_server.py`.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CapacityError,
  SandboxNotFoundError,
  SandboxStateError,
  UnimplementedError,
} from "../../../src/errors.js";
import { McpSettings } from "../../../src/mcp/settings.js";
import { Commands } from "../../../src/sandbox/commands.js";
import { ONE_PIXEL_PNG_BASE64 } from "../fake/code.js";
import { type FakeControlPlane, IMAGE_ARN, JWE, SANDBOX_ID } from "../fake/control-plane.js";
import { awsSdkError } from "../fake/dashboard.js";
import { errorText, type Fixture, firstJson, startFixture } from "./support.js";

const CODE_MARKER = "MARKER_CODE_7f3a";
const FILE_MARKER = "MARKER_FILE_9c1d";

afterEach(() => {
  vi.restoreAllMocks();
});

function launches(plane: FakeControlPlane): number {
  return plane.callsTo("runMicrovm").length;
}

function terminations(plane: FakeControlPlane): number {
  return plane.callsTo("terminateMicrovm").length;
}

async function connected(fixture?: Fixture) {
  const active = fixture ?? (await startFixture());
  return { fixture: active, client: await active.connect() };
}

describe("list_sandboxes", () => {
  it("nunca crea un sandbox", async () => {
    const { fixture, client } = await connected();
    fixture.plane.addListed("x", "RUNNING");
    fixture.plane.addListed("y", "SUSPENDED");
    const result = await client.callTool({ name: "list_sandboxes", arguments: {} });
    expect(result.isError).toBeFalsy();
    const sandboxes = (result.structuredContent as { sandboxes: Array<Record<string, unknown>> })
      .sandboxes;
    expect(sandboxes.map((item) => item.sandbox_id)).toEqual(["x", "y"]);
    expect(sandboxes.map((item) => item.state)).toEqual(["RUNNING", "SUSPENDED"]);
    expect(sandboxes.every((item) => item.current === false)).toBe(true);
    expect(sandboxes.every((item) => item.template === IMAGE_ARN)).toBe(true);
    expect(launches(fixture.plane)).toBe(0);
  });

  it("marca el sandbox actual", async () => {
    const { fixture, client } = await connected();
    await client.callTool({ name: "run_command", arguments: { cmd: "echo hola" } });
    fixture.plane.listed.length = 0;
    fixture.plane.addListed("other", "RUNNING");
    fixture.plane.addListed(SANDBOX_ID, "RUNNING");
    const result = await client.callTool({ name: "list_sandboxes", arguments: {} });
    const sandboxes = (result.structuredContent as { sandboxes: Array<{ current: boolean }> })
      .sandboxes;
    expect(sandboxes.map((item) => item.current)).toEqual([false, true]);
  });
});

describe("run_command", () => {
  it("el primer comando crea el sandbox y el segundo lo reutiliza", async () => {
    const { fixture, client } = await connected();
    const first = await client.callTool({ name: "run_command", arguments: { cmd: "echo hola" } });
    const second = await client.callTool({ name: "run_command", arguments: { cmd: "echo otra" } });
    expect(first.isError).toBeFalsy();
    expect(first.structuredContent).toEqual({
      stdout: "hola\n",
      stderr: "",
      exit_code: 0,
      truncated: false,
    });
    expect((second.structuredContent as { stdout: string }).stdout).toBe("otra\n");
    expect(launches(fixture.plane)).toBe(1);
  });

  it("dos primeras llamadas concurrentes crean un solo sandbox", async () => {
    const { fixture, client } = await connected();
    const results = await Promise.all([
      client.callTool({ name: "run_command", arguments: { cmd: "echo a" } }),
      client.callTool({ name: "run_command", arguments: { cmd: "echo b" } }),
    ]);
    const outputs = results.map((r) => (r.structuredContent as { stdout: string }).stdout).sort();
    expect(outputs).toEqual(["a\n", "b\n"]);
    expect(launches(fixture.plane)).toBe(1);
  });

  it("los exit codes son datos", async () => {
    const { client } = await connected();
    const exitThree = await client.callTool({ name: "run_command", arguments: { cmd: "exit 3" } });
    const unknown = await client.callTool({ name: "run_command", arguments: { cmd: "nosuchcmd" } });
    expect(exitThree.isError).toBeFalsy();
    expect((exitThree.structuredContent as { exit_code: number }).exit_code).toBe(3);
    expect(unknown.isError).toBeFalsy();
    const output = unknown.structuredContent as { exit_code: number; stderr: string };
    expect(output.exit_code).toBe(127);
    expect(output.stderr).toContain("command not found");
  });

  it("respeta cwd y el timeout", async () => {
    const { fixture, client } = await connected();
    const result = await client.callTool({
      name: "run_command",
      arguments: { cmd: "pwd", cwd: "/tmp" },
    });
    const timedOut = await client.callTool({
      name: "run_command",
      arguments: { cmd: "sleep 10", timeout: 1 },
    });
    expect((result.structuredContent as { stdout: string }).stdout).toBe("/tmp\n");
    expect(fixture.rayd.process.startRequests[0]?.process?.cwd).toBe("/tmp");
    expect(errorText(timedOut)).toContain("superó el timeout de 1 s");
  });

  it("rechaza un timeout fuera de rango antes de tocar AWS", async () => {
    const { fixture, client } = await connected();
    const result = await client.callTool({
      name: "run_command",
      arguments: { cmd: "echo x", timeout: 0 },
    });
    expect(result.isError).toBe(true);
    expect(launches(fixture.plane)).toBe(0);
  });
});

describe("ficheros", () => {
  it("escribe, lee y lista; un fichero ausente es un error de la herramienta", async () => {
    const { client } = await connected();
    const written = await client.callTool({
      name: "write_file",
      arguments: { path: "/home/user/a.txt", content: "hola" },
    });
    const read = await client.callTool({
      name: "read_file",
      arguments: { path: "/home/user/a.txt" },
    });
    const listed = await client.callTool({
      name: "list_files",
      arguments: { path: "/home/user" },
    });
    const missing = await client.callTool({
      name: "read_file",
      arguments: { path: "/home/user/missing.txt" },
    });
    expect(written.structuredContent).toEqual({ path: "/home/user/a.txt", size: 4 });
    expect(read.structuredContent).toEqual({
      path: "/home/user/a.txt",
      content: "hola",
      size: 4,
      truncated: false,
    });
    const entries = (
      listed.structuredContent as { entries: Array<{ name: string; type: string; size: number }> }
    ).entries;
    const entry = entries.find((candidate) => candidate.name === "a.txt");
    expect(entry).toMatchObject({ type: "file", size: 4 });
    expect(errorText(missing)).toContain("path not found");
  });

  it("list_files usa /home/user y profundidad 1 por defecto y rechaza depth fuera de rango", async () => {
    const { fixture, client } = await connected();
    const listed = await client.callTool({ name: "list_files", arguments: {} });
    expect((listed.structuredContent as { path: string }).path).toBe("/home/user");
    const deep = await client.callTool({ name: "list_files", arguments: { depth: 6 } });
    expect(deep.isError).toBe(true);
    expect(launches(fixture.plane)).toBe(1);
  });
});

describe("run_code", () => {
  it("devuelve texto, imagen y errores del kernel como datos", async () => {
    const { client } = await connected();
    const text = await client.callTool({ name: "run_code", arguments: { code: "1+1" } });
    const plot = await client.callTool({ name: "run_code", arguments: { code: "plot" } });
    const error = await client.callTool({
      name: "run_code",
      arguments: { code: "raise ValueError('x')" },
    });
    expect(text.isError).toBeFalsy();
    expect(firstJson(text).text).toBe("2");
    expect(firstJson(text).error).toBeNull();
    expect(plot.isError).toBeFalsy();
    expect((firstJson(plot).results as Array<{ mime_types: string[] }>)[0]?.mime_types).toContain(
      "image/png",
    );
    expect(plot.content[1]).toMatchObject({
      type: "image",
      mimeType: "image/png",
      data: ONE_PIXEL_PNG_BASE64,
    });
    expect(error.isError).toBeFalsy();
    expect((firstJson(error).error as { name: string }).name).toBe("ValueError");
  });

  it("conserva el estado del kernel entre llamadas", async () => {
    const { client } = await connected();
    await client.callTool({ name: "run_code", arguments: { code: "x = 42" } });
    const result = await client.callTool({ name: "run_code", arguments: { code: "x" } });
    expect(firstJson(result).text).toBe("42");
  });
});

describe("ciclo de vida del lease", () => {
  it("sin template es un error de la herramienta y no toca AWS", async () => {
    const fixture = await startFixture({ settings: new McpSettings({}) });
    const client = await fixture.connect();
    const command = await client.callTool({ name: "run_command", arguments: { cmd: "echo x" } });
    const listed = await client.callTool({ name: "list_sandboxes", arguments: {} });
    expect(errorText(command)).toContain("RAYITO_TEMPLATE");
    expect(errorText(listed)).toContain("RAYITO_TEMPLATE");
    expect(fixture.plane.calls).toEqual([]);
  });

  it("un fallo de creación se informa y la siguiente llamada reintenta", async () => {
    const { fixture, client } = await connected();
    fixture.plane.runMicrovmError = new CapacityError("no hay capacidad");
    const failed = await client.callTool({ name: "run_command", arguments: { cmd: "echo hola" } });
    expect(errorText(failed)).toBe("no se pudo crear el sandbox: no hay capacidad");
    fixture.plane.runMicrovmError = undefined;
    const retried = await client.callTool({ name: "run_command", arguments: { cmd: "echo hola" } });
    expect(retried.isError).toBeFalsy();
  });

  it("no termina nada si nunca se creó un sandbox", async () => {
    const { fixture, client } = await connected();
    await client.listTools();
    await fixture.app.lease.close();
    expect(terminations(fixture.plane)).toBe(0);
  });

  it("termina el sandbox exactamente una vez al cerrar", async () => {
    const { fixture, client } = await connected();
    await client.callTool({ name: "run_command", arguments: { cmd: "echo hola" } });
    await fixture.app.lease.close();
    await fixture.app.lease.close();
    expect(terminations(fixture.plane)).toBe(1);
  });

  it("un sandbox desaparecido reinicia el lease", async () => {
    const { fixture, client } = await connected();
    await client.callTool({ name: "run_command", arguments: { cmd: "echo hola" } });
    const gone = new SandboxNotFoundError(`el sandbox ${SANDBOX_ID} está TERMINATED: timeout`);
    vi.spyOn(Commands.prototype, "run").mockRejectedValueOnce(gone);
    const vanished = await client.callTool({
      name: "run_command",
      arguments: { cmd: "echo hola" },
    });
    const recreated = await client.callTool({
      name: "run_command",
      arguments: { cmd: "echo hola" },
    });
    const text = errorText(vanished);
    expect(text).toContain(`el sandbox ${SANDBOX_ID} ya no existe`);
    expect(text).toContain("la siguiente llamada crea uno nuevo");
    expect(recreated.isError).toBeFalsy();
    expect(launches(fixture.plane)).toBe(2);
  });

  it("un sandbox en transición pide reintentar sin reiniciar el lease", async () => {
    const { fixture, client } = await connected();
    await client.callTool({ name: "run_command", arguments: { cmd: "echo hola" } });
    const suspending = new SandboxStateError(
      "el sandbox está suspending; reintenta cuando vuelva a RUNNING",
    );
    vi.spyOn(Commands.prototype, "run").mockRejectedValueOnce(suspending);
    const inTransition = await client.callTool({
      name: "run_command",
      arguments: { cmd: "echo hola" },
    });
    const retried = await client.callTool({ name: "run_command", arguments: { cmd: "echo otra" } });
    const text = errorText(inTransition);
    expect(text).toContain("en transición");
    expect(text).toContain("reintenta");
    expect(text).not.toContain("ya no existe");
    expect((retried.structuredContent as { stdout: string }).stdout).toBe("otra\n");
    expect(launches(fixture.plane)).toBe(1);
  });

  it("una función no disponible es un error de la herramienta y no reinicia el lease", async () => {
    const { fixture, client } = await connected();
    await client.callTool({ name: "run_command", arguments: { cmd: "echo hola" } });
    vi.spyOn(Commands.prototype, "run").mockRejectedValueOnce(
      new UnimplementedError("run_command", "la imagen es anterior a 0.3.0"),
    );
    const result = await client.callTool({ name: "run_command", arguments: { cmd: "echo hola" } });
    const retried = await client.callTool({ name: "run_command", arguments: { cmd: "echo otra" } });
    const text = errorText(result);
    expect(text).toContain("run_command no está disponible");
    expect(text).not.toContain("ya no existe");
    expect(text).not.toContain("en transición");
    expect(retried.isError).toBeFalsy();
    expect(launches(fixture.plane)).toBe(1);
  });

  it("un fallo inesperado sólo expone el nombre del error", async () => {
    const { client } = await connected();
    await client.callTool({ name: "run_command", arguments: { cmd: "echo hola" } });
    vi.spyOn(Commands.prototype, "run").mockRejectedValueOnce(
      new RangeError("detalle interno con /ruta/secreta"),
    );
    const result = await client.callTool({ name: "run_command", arguments: { cmd: "echo hola" } });
    const text = errorText(result);
    expect(text).toContain("RangeError");
    expect(text).not.toContain("secreta");
  });

  it("un error crudo de AWS sale con su código y el mensaje redactado", async () => {
    const { client } = await connected();
    await client.callTool({ name: "run_command", arguments: { cmd: "echo hola" } });
    const failure = awsSdkError("denegado para AKIAIOSFODNN7EXAMPLE", "AccessDeniedException");
    vi.spyOn(Commands.prototype, "run").mockRejectedValueOnce(failure);
    const result = await client.callTool({ name: "run_command", arguments: { cmd: "echo hola" } });
    const text = errorText(result);
    expect(text).toContain("AccessDeniedException");
    expect(text).not.toContain("AKIAIOSFODNN7EXAMPLE");
  });
});

describe("higiene de logging", () => {
  it("nunca registra código, comandos, contenidos ni tokens", async () => {
    const fixture = await startFixture({
      settings: new McpSettings({ template: IMAGE_ARN, idleSeconds: 0, logLevel: "DEBUG" }),
    });
    const client = await fixture.connect();
    await client.callTool({ name: "run_code", arguments: { code: `print('${CODE_MARKER}')` } });
    await client.callTool({
      name: "write_file",
      arguments: { path: "/home/user/m.txt", content: FILE_MARKER },
    });
    await client.callTool({ name: "run_command", arguments: { cmd: `echo ${CODE_MARKER}` } });
    await fixture.app.lease.close();
    const logged = fixture.stderr.join("");
    expect(logged).toContain("run_code");
    expect(logged).not.toContain(CODE_MARKER);
    expect(logged).not.toContain(FILE_MARKER);
    expect(logged).not.toContain(JWE);
  });
});
