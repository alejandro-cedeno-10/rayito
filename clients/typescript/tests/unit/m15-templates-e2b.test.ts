/**
 * Shim `rayito/e2b` `Template` (m15-templates): la firma de build de E2B JS
 * (`alias`, `skipCache`, `cpuCount`, `memoryMB`) se traduce a la nativa;
 * `bucket`/`region` llegan del cliente `new E2B({...})`. Espejo de
 * `test_m15_templates_e2b.py`.
 */

import { afterEach, describe, expect, test, vi } from "vitest";
import { E2B, Template } from "../../src/e2b/index.js";
import { resolveMemoryMb } from "../../src/e2b/template.js";
import { InvalidArgumentError } from "../../src/errors.js";
import { Template as NativeTemplate } from "../../src/templates/dsl.js";

function spyNative(): { calls: Array<[string, Record<string, unknown>]> } {
  const calls: Array<[string, Record<string, unknown>]> = [];
  const record = async (_template: unknown, name: string, options: object) => {
    calls.push([name, options as Record<string, unknown>]);
    return { templateId: name, buildId: name, alias: name } as never;
  };
  vi.spyOn(NativeTemplate, "build").mockImplementation(record);
  vi.spyOn(NativeTemplate, "buildInBackground").mockImplementation(record);
  return { calls };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("e2b Template shim", () => {
  test("build accepts E2B's { alias, skipCache }", async () => {
    const { calls } = spyNative();
    await Template.build(new Template().fromBaseImage(), {
      alias: "mi-template",
      skipCache: true,
      bucket: "b",
    });
    expect(calls[0]?.[0]).toBe("mi-template");
    expect(calls[0]?.[1]).toMatchObject({ force: true, bucket: "b" });
  });

  test("the client binds region and bucket; the call wins", async () => {
    const { calls } = spyNative();
    const client = new E2B({ region: "eu-west-1", bucket: "bound-bucket" });
    await client.Template.build(new Template().fromBaseImage(), "mi-template");
    await client.Template.buildInBackground(new Template().fromBaseImage(), "otro", {
      bucket: "call-wins",
    });
    expect(calls[0]?.[1]).toMatchObject({ region: "eu-west-1", bucket: "bound-bucket" });
    expect(calls[1]?.[1]).toMatchObject({ bucket: "call-wins" });
  });

  test("without a bucket the error names the option", async () => {
    expect(() => Template.build(new Template().fromBaseImage(), "mi-template")).toThrow(
      /new E2B\(\{ bucket \}\)/,
    );
  });

  test("memoryMB rounds up with a warning and cpuCount warns", async () => {
    const warn = vi.spyOn(process, "emitWarning").mockImplementation(() => undefined);
    const { calls } = spyNative();
    await Template.build(new Template().fromBaseImage(), "t", {
      bucket: "b",
      memoryMB: 1500,
      cpuCount: 4,
    });
    expect(calls[0]?.[1]).toMatchObject({ memoryMb: 2048 });
    const messages = warn.mock.calls.map(([message]) => String(message));
    expect(messages.some((message) => message.includes("redondeado a 2048"))).toBe(true);
    expect(messages.some((message) => message.startsWith("cpuCount ignorado"))).toBe(true);
  });

  test("resolveMemoryMb rejects a size above the maximum and keeps exact sizes", () => {
    expect(() => resolveMemoryMb(16384)).toThrow(InvalidArgumentError);
    expect(resolveMemoryMb(1024)).toBe(1024);
    expect(resolveMemoryMb(undefined)).toBeUndefined();
  });
});
