/**
 * Las seis herramientas del servidor MCP una a una, con el cliente oficial del
 * SDK de MCP: lo que comparten los e2e contra AWS real (`tests/e2e/mcp.e2e.test.ts`)
 * y los locales contra Docker + Floci (`tests/local/mcp.local.test.ts`). El
 * transporte (stdio o HTTP) y el sandbox (un MicroVM real o el guest local) los
 * pone quien llama.
 */

import type { Client } from "@modelcontextprotocol/client";
import { expect } from "vitest";

export const TOOL_NAMES = [
  "run_code",
  "run_command",
  "read_file",
  "write_file",
  "list_files",
  "list_sandboxes",
];
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const PLOT_CODE = "import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()";
const TEST_FILE = "/home/user/mcp.txt";
const TEST_CONTENT = "hola mcp";
export const FIRST_CALL_TIMEOUT_MS = 240_000;
export const CALL_TIMEOUT_MS = 120_000;

export type ToolResult = Awaited<ReturnType<Client["callTool"]>>;

export function structured(result: ToolResult): Record<string, unknown> {
  expect(result.isError, JSON.stringify(result.content)).toBeFalsy();
  expect(result.structuredContent).toBeDefined();
  return result.structuredContent as Record<string, unknown>;
}

export function firstJson(result: ToolResult): Record<string, unknown> {
  const first = result.content[0];
  expect(first?.type).toBe("text");
  return JSON.parse((first as { text: string }).text) as Record<string, unknown>;
}

/** Las seis herramientas una a una; devuelve el id del sandbox que creó el servidor. */
export async function exerciseTools(client: Client, templateArn: string): Promise<string> {
  const tools = await client.listTools();
  expect(tools.tools.map((tool) => tool.name)).toEqual(TOOL_NAMES);

  const echoed = await client.callTool(
    { name: "run_command", arguments: { cmd: "echo hola" } },
    { timeout: FIRST_CALL_TIMEOUT_MS },
  );
  expect(structured(echoed)).toMatchObject({ stdout: "hola\n", exit_code: 0 });

  const failed = await client.callTool(
    { name: "run_command", arguments: { cmd: "exit 3" } },
    { timeout: CALL_TIMEOUT_MS },
  );
  expect(structured(failed).exit_code).toBe(3);

  const written = await client.callTool(
    { name: "write_file", arguments: { path: TEST_FILE, content: TEST_CONTENT } },
    { timeout: CALL_TIMEOUT_MS },
  );
  expect(structured(written).size).toBe(TEST_CONTENT.length);
  const read = await client.callTool(
    { name: "read_file", arguments: { path: TEST_FILE } },
    { timeout: CALL_TIMEOUT_MS },
  );
  expect(structured(read).content).toBe(TEST_CONTENT);
  const listed = await client.callTool(
    { name: "list_files", arguments: { path: "/home/user" } },
    { timeout: CALL_TIMEOUT_MS },
  );
  const names = (structured(listed).entries as Array<{ name: string }>).map((entry) => entry.name);
  expect(names).toContain("mcp.txt");

  const state = await client.callTool(
    { name: "run_code", arguments: { code: "answer = 6 * 7" } },
    { timeout: CALL_TIMEOUT_MS },
  );
  expect(state.isError).toBeFalsy();
  const recalled = await client.callTool(
    { name: "run_code", arguments: { code: "answer" } },
    { timeout: CALL_TIMEOUT_MS },
  );
  expect(firstJson(recalled).text).toBe("42");

  const plotted = await client.callTool(
    { name: "run_code", arguments: { code: PLOT_CODE } },
    { timeout: CALL_TIMEOUT_MS },
  );
  expect(plotted.isError, JSON.stringify(plotted.content)).toBeFalsy();
  const image = plotted.content.find((block) => block.type === "image") as
    | { mimeType: string; data: string }
    | undefined;
  expect(image?.mimeType).toBe("image/png");
  expect(Buffer.from(image?.data ?? "", "base64").subarray(0, PNG_SIGNATURE.length)).toEqual(
    PNG_SIGNATURE,
  );
  const results = firstJson(plotted).results as Array<{ mime_types: string[] }>;
  expect(results[0]?.mime_types).toContain("image/png");

  const divided = await client.callTool(
    { name: "run_code", arguments: { code: "1/0" } },
    { timeout: CALL_TIMEOUT_MS },
  );
  expect(divided.isError).toBeFalsy();
  expect((firstJson(divided).error as { name: string }).name).toBe("ZeroDivisionError");

  const sandboxes = await client.callTool(
    { name: "list_sandboxes", arguments: {} },
    { timeout: CALL_TIMEOUT_MS },
  );
  const current = (
    structured(sandboxes).sandboxes as Array<{
      sandbox_id: string;
      current: boolean;
      template: string;
    }>
  ).filter((item) => item.current);
  expect(current).toHaveLength(1);
  expect(current[0]?.template).toBe(templateArn);
  return current[0]?.sandbox_id as string;
}
