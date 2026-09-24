import { create } from "@bufbuild/protobuf";
import { Code } from "@connectrpc/connect";
import { describe, expect, test } from "vitest";
import { ChartType, type LineChart } from "../../src/charts.js";
import {
  InvalidArgumentError,
  NotFoundError,
  SandboxError,
  TimeoutError,
} from "../../src/errors.js";
import { ExecuteEventSchema, ExecutionResultSchema } from "../../src/gen/rayito/v1/code_pb.js";
import { type OutputMessage, Result } from "../../src/models.js";
import {
  buildCreateContextRequest,
  buildExecuteRequest,
  buildReattachRequest,
  ExecutionBuilder,
  executeDeadlineMs,
  languageDefaultContextId,
  normalizeLanguage,
  resultFromProto,
  validateCode,
  validateCwd,
  validateLanguage,
} from "../../src/sandbox/code.js";
import { Sandbox } from "../../src/sandbox/sandbox.js";
import {
  DATAFRAME_DATA,
  DATAFRAME_TEXT,
  endEvent,
  OMITTED_NOTE,
  ONE_PIXEL_PNG_BASE64,
  resultEvent,
  stdoutEvent,
} from "./fake/code.js";
import { createTestSandbox, sleep, waitUntil } from "./helpers.js";

describe("pure helpers", () => {
  test("validators and requests", () => {
    expect(validateCode("x")).toBe("x");
    expect(() => validateCode("x".repeat(1_048_577))).toThrow(InvalidArgumentError);
    expect(() => validateCode(5)).toThrow(InvalidArgumentError);
    expect(validateLanguage(undefined)).toBe("python");
    expect(validateLanguage("")).toBe("python");
    expect(validateLanguage("Bash")).toBe("bash");
    expect(validateLanguage("js")).toBe("javascript");
    expect(() => validateLanguage("ruby")).toThrow(InvalidArgumentError);
    expect(normalizeLanguage(undefined)).toBeUndefined();
    expect(normalizeLanguage("")).toBeUndefined();
    expect(normalizeLanguage("Python")).toBe("python");
    expect(normalizeLanguage("JS")).toBe("javascript");
    expect(() => normalizeLanguage("r")).toThrow(InvalidArgumentError);
    const bash = buildExecuteRequest("echo 1", { language: "bash" });
    expect(bash.language).toBe("bash");
    expect(bash.contextId).toBeUndefined();
    expect(buildExecuteRequest("x").language).toBeUndefined();
    expect(buildExecuteRequest("x", { language: "" }).language).toBeUndefined();
    expect(buildExecuteRequest("x", { language: "Python" }).language).toBe("python");
    expect(() => buildExecuteRequest("x", { contextId: "default", language: "bash" })).toThrow(
      InvalidArgumentError,
    );
    expect(languageDefaultContextId(bash)).toBe("default-bash");
    expect(languageDefaultContextId(buildExecuteRequest("x", { language: "python" }))).toBe(
      "default",
    );
    expect(languageDefaultContextId(buildExecuteRequest("x"))).toBeUndefined();
    expect(validateCwd(undefined)).toBeUndefined();
    expect(validateCwd("")).toBeUndefined();
    expect(validateCwd("/tmp")).toBe("/tmp");
    expect(() => validateCwd("tmp")).toThrow(InvalidArgumentError);
    const execute = buildExecuteRequest("1+1", {
      contextId: "ctx",
      envs: { A: "1" },
      timeoutMs: 2000,
    });
    expect(execute.contextId).toBe("ctx");
    expect(execute.envs).toEqual({ A: "1" });
    expect(execute.timeoutMs).toBe(2000n);
    expect(buildExecuteRequest("x").timeoutMs).toBe(300_000n);
    expect(buildExecuteRequest("x").contextId).toBeUndefined();
    const context = buildCreateContextRequest({ cwd: "/tmp", envs: { B: "2" } });
    expect(context.language).toBe("python");
    expect(context.cwd).toBe("/tmp");
    expect(context.envs).toEqual({ B: "2" });
    expect(executeDeadlineMs(300_000)).toBe(315_000);
    expect(executeDeadlineMs(0)).toBeUndefined();
    const reattach = buildReattachRequest("default", "exec-0123456789abcdef", 4);
    expect(reattach.fromSeq).toBe(4n);
    expect(() => buildReattachRequest("default", "bad", 0)).toThrow(InvalidArgumentError);
    expect(() => buildReattachRequest("default", "exec-0123456789abcdef", -1)).toThrow(
      InvalidArgumentError,
    );
  });

  test.each([
    ["python", "python"],
    ["Bash", "bash"],
    ["javascript", "javascript"],
    ["JS", "javascript"],
    ["typescript", "typescript"],
    ["TypeScript", "typescript"],
    ["ts", "typescript"],
    ["TS", "typescript"],
  ])("normalizeLanguage(%j) is %j", (given, canonical) => {
    expect(normalizeLanguage(given)).toBe(canonical);
    expect(validateLanguage(given)).toBe(canonical);
  });

  test.each(["tsx", "r", "java", "ruby", " bash"])("normalizeLanguage rejects %j", (given) => {
    expect(() => normalizeLanguage(given)).toThrow(InvalidArgumentError);
  });

  test("typescript routes to its lazy default context", () => {
    const request = buildExecuteRequest("1", { language: "ts" });
    expect(request.language).toBe("typescript");
    expect(request.contextId).toBeUndefined();
    expect(languageDefaultContextId(request)).toBe("default-typescript");
    expect(buildCreateContextRequest({ language: "TypeScript" }).language).toBe("typescript");
    expect(() => buildExecuteRequest("1", { contextId: "default", language: "ts" })).toThrow(
      InvalidArgumentError,
    );
  });

  test("resultFromProto parses json, data and chart, keeps raw and extra", () => {
    const result = resultFromProto(
      create(ExecutionResultSchema, {
        isMainResult: true,
        text: DATAFRAME_TEXT,
        data: JSON.stringify(DATAFRAME_DATA),
        json: "{not json",
        chart: JSON.stringify({ type: "line", title: "t" }),
        extra: { "rayito/omitted": OMITTED_NOTE },
      }),
    );
    expect(result).toBeInstanceOf(Result);
    expect(result.data).toEqual(DATAFRAME_DATA);
    expect(result.json).toBe("{not json");
    expect((result.chart as LineChart).type).toBe(ChartType.LINE);
    expect(result.isMainResult).toBe(true);
    expect(result.formats()).toEqual(["text", "json", "data", "chart", "rayito/omitted"]);
    expect(result.raw["e2b/data"]).toBe(JSON.stringify(DATAFRAME_DATA));
    expect(result.raw["rayito/omitted"]).toBe(OMITTED_NOTE);
  });

  test("ExecutionBuilder enforces the protocol", () => {
    const builder = new ExecutionBuilder();
    expect(() => builder.reattachRequest()).toThrow(SandboxError);
    expect(() => builder.finish()).toThrow(SandboxError);
    const started = create(ExecuteEventSchema, {
      event: {
        case: "started",
        value: { executionId: "exec-0123456789abcdef", executionCount: 1n },
      },
      seq: 1n,
    });
    expect(builder.feed(started)).toBe(false);
    expect(() => builder.feed(started)).toThrow(/segundo started/);
    const out = stdoutEvent("a");
    out.seq = 2n;
    builder.feed(out);
    expect(builder.reattachRequest().fromSeq).toBe(3n);
    const end = endEvent(1);
    end.seq = 3n;
    expect(builder.feed(end)).toBe(true);
    expect(() => builder.feed(out)).toThrow(/después del end/);
    expect(builder.finish().logs.stdout).toEqual(["a"]);
    const rich = new ExecutionBuilder();
    expect(() => rich.feed(create(ExecuteEventSchema, {}))).toThrow(/desconocido/);
    expect(rich.feed(resultEvent({ text: "1" }, { isMainResult: true }))).toBe(false);
    expect(rich.execution.text).toBe("1");
  });
});

describe("runCode", () => {
  test("acceptance sequence against the fake", async () => {
    const { sandbox } = await createTestSandbox();
    expect((await sandbox.runCode("x = 42")).text).toBeUndefined();
    expect((await sandbox.runCode("x")).text).toBe("42");
    const printed = await sandbox.runCode("print(x)");
    expect(printed.logs.stdout.join("")).toContain("42");
    expect(printed.text).toBeUndefined();
    const plot = await sandbox.runCode("plot");
    expect(plot.results[0]?.png).toBe(ONE_PIXEL_PNG_BASE64);
    expect(plot.results[0]?.chart).toBeDefined();
    expect(plot.results[0]?.formats()).toEqual(["png", "chart"]);
    expect((plot.results[0]?.chart as LineChart | undefined)?.title).toBe("plot");
    const failed = await sandbox.runCode("1/0");
    expect(failed.error?.name).toBe("ZeroDivisionError");
    expect(failed.error?.traceback).toContain("ZeroDivisionError");
    expect(failed.executionCount).toBeGreaterThan(0);
    const slow = await sandbox.runCode("slow 10", { timeoutMs: 2000 });
    expect(slow.error?.name).toBe("ExecutionTimeout");
  });

  test("callbacks receive typed messages", async () => {
    const { sandbox } = await createTestSandbox();
    const seen: OutputMessage[] = [];
    const results: Result[] = [];
    const errors: string[] = [];
    await sandbox.runCode("print-then-sleep", {
      timeoutMs: 100,
      onStdout: (message) => seen.push(message),
    });
    expect(seen.every((message) => message.error === false && message.timestamp > 0)).toBe(true);
    expect(seen.map((message) => message.line).join("")).toContain("tick");
    await sandbox.runCode("stderr", { onStderr: (message) => seen.push(message) });
    expect(seen.at(-1)).toMatchObject({ line: "warn\n", error: true });
    await sandbox.runCode("many", { onResult: (result) => results.push(result) });
    expect(results.map((result) => result.text)).toEqual(["one", "two", "three"]);
    await sandbox.runCode("raise ValueError('boom')", {
      onError: (error) => errors.push(error.name),
    });
    expect(errors).toEqual(["ValueError"]);
  });

  test("wire fields: timeout, envs, context and the Execute deadline", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const envs = await sandbox.runCode("envs-echo", { envs: { B: "2", A: "1" }, timeoutMs: 5000 });
    expect(envs.logs.stdout.join("")).toBe('{"A":"1","B":"2"}\n');
    expect(rayd.code.executeRequests[0]?.timeoutMs).toBe(5000n);
    expect(rayd.code.executeDeadlines[0]).toBe(20_000);
    await sandbox.runCode("x = 1", { timeoutMs: 0 });
    expect(rayd.code.executeDeadlines[1]).toBeUndefined();
    await sandbox.runCode("x = 1", { requestTimeoutMs: 7000 });
    expect(rayd.code.executeDeadlines[2]).toBe(7000);
    await expect(sandbox.runCode("x", { context: "missing" })).rejects.toBeInstanceOf(
      NotFoundError,
    );
    await expect(sandbox.runCode("x", { context: "" })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await expect(
      sandbox.runCode("sleep", { timeoutMs: 100, requestTimeoutMs: 50 }),
    ).rejects.toBeInstanceOf(TimeoutError);
  });

  test("leaving early cancels the stream so the agent interrupts the cell", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    await expect(
      sandbox.runCode("print-then-sleep", {
        onStdout: () => {
          throw new Error("stop here");
        },
      }),
    ).rejects.toThrow("stop here");
    await waitUntil(() => rayd.code.executions[0]?.interrupted === true);
  });

  test("toJSON has the Python shape and results round-trip", async () => {
    const { sandbox } = await createTestSandbox();
    const execution = await sandbox.runCode("df");
    const json = execution.toJSON() as {
      results: Array<Record<string, unknown>>;
      execution_count: number;
    };
    expect(json.results[0]).toMatchObject({ is_main_result: true, "text/plain": DATAFRAME_TEXT });
    expect(json.execution_count).toBeGreaterThan(0);
    expect(execution.results[0]?.data).toEqual(DATAFRAME_DATA);
    const omitted = await sandbox.runCode("omitted");
    expect(omitted.results[0]?.formats()).toEqual(["rayito/omitted"]);
    const badJson = await sandbox.runCode("bad-json");
    expect(badJson.results[0]?.json).toBe("{not json");
  });

  test("Execute uses the unary transport", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    await sandbox.runCode("x = 1");
    expect(Sandbox.coreOf(sandbox).streamTransportOpened).toBe(false);
    expect(rayd.sessions).toBe(1);
  });
});

describe("code contexts", () => {
  test("create, run in, list, remove; default is protected", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const ctx = await sandbox.createCodeContext({ cwd: "/tmp" });
    expect(ctx.id).toMatch(/^ctx-[0-9a-f]{12}$/);
    expect(ctx.cwd).toBe("/tmp");
    expect(ctx.language).toBe("python");
    expect(rayd.code.createDeadlines[0]).toBe(90_000);
    expect((await sandbox.runCode("2*2", { context: ctx })).text).toBe("4");
    const listed = await sandbox.listCodeContexts();
    expect(listed.map((item) => item.id)).toEqual(["default", ctx.id]);
    await sandbox.removeCodeContext(ctx.id);
    expect((await sandbox.listCodeContexts()).map((item) => item.id)).toEqual(["default"]);
    await expect(sandbox.removeCodeContext("default")).rejects.toBeInstanceOf(InvalidArgumentError);
    await expect(sandbox.removeCodeContext(ctx)).rejects.toBeInstanceOf(NotFoundError);
    await expect(sandbox.createCodeContext({ cwd: "/nope" })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await expect(sandbox.createCodeContext({ language: "ruby" })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
  });

  test("restart loses the namespace and kernel gate is a SandboxError", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    await sandbox.runCode("y = 7");
    expect((await sandbox.runCode("y")).text).toBe("7");
    await sandbox.restartCodeContext("default");
    expect(rayd.code.restartRequests).toEqual(["default"]);
    expect((await sandbox.runCode("y")).error?.name).toBe("NameError");
    rayd.code.kernelGate = "booting";
    const gated = await sandbox.runCode("y").catch((error: unknown) => error);
    expect(gated).toBeInstanceOf(SandboxError);
    expect((gated as Error).message).toContain("kernel");
    rayd.code.kernelGate = undefined;
    await sleep(10);
    expect((await sandbox.runCode("1+1")).text).toBe("2");
  });
});

describe("languages (M7)", () => {
  test("routing against the fake", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    {
      const bash = await sandbox.runCode("echo hi", { language: "Bash" });
      expect(bash.logs.stdout.join("")).toBe("hi\n");
      expect(bash.error).toBeUndefined();
      expect(rayd.code.executeRequests.at(-1)?.language).toBe("bash");
      expect(rayd.code.executeRequests.at(-1)?.contextId).toBeUndefined();
      expect(rayd.code.executions.at(-1)?.contextId).toBe("default-bash");
      const javascript = await sandbox.runCode("1 + 1", { language: "js" });
      expect(javascript.text).toBe("2");
      expect(rayd.code.executeRequests.at(-1)?.language).toBe("javascript");
      expect(rayd.code.executions.at(-1)?.contextId).toBe("default-javascript");
      const typescript = await sandbox.runCode("1 + 1", { language: "ts" });
      expect(typescript.text).toBe("2");
      expect(rayd.code.executeRequests.at(-1)?.language).toBe("typescript");
      expect(rayd.code.executeRequests.at(-1)?.contextId).toBeUndefined();
      expect(rayd.code.executions.at(-1)?.contextId).toBe("default-typescript");
      await sandbox.runCode("x");
      expect(rayd.code.executeRequests.at(-1)?.language).toBeUndefined();
      expect(rayd.code.lazyContexts).toEqual([
        "default-bash",
        "default-javascript",
        "default-typescript",
      ]);
      const listed = (await sandbox.listCodeContexts()).map((ctx) => [ctx.id, ctx.language]);
      expect(listed[0]).toEqual(["default", "python"]);
      expect(listed).toContainEqual(["default-bash", "bash"]);
      expect(listed).toContainEqual(["default-javascript", "javascript"]);
      expect(listed).toContainEqual(["default-typescript", "typescript"]);
      const tsContext = await sandbox.createCodeContext({ language: "TypeScript" });
      expect(tsContext.language).toBe("typescript");
      expect(rayd.code.createRequests.at(-1)?.language).toBe("typescript");
      const ctx = await sandbox.createCodeContext({ language: "bash" });
      expect(ctx.language).toBe("bash");
      expect(rayd.code.createRequests.at(-1)?.language).toBe("bash");
      const inContext = await sandbox.runCode("echo ctx", { context: ctx });
      expect(inContext.logs.stdout.join("")).toBe("ctx\n");
      expect(rayd.code.executions.at(-1)?.contextId).toBe(ctx.id);
    }
  });

  test("invalid language combinations never reach the fake", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    {
      const executes = rayd.code.executeRequests.length;
      const creates = rayd.code.createRequests.length;
      await expect(sandbox.runCode("1", { language: "r" })).rejects.toBeInstanceOf(
        InvalidArgumentError,
      );
      await expect(sandbox.runCode("1", { language: "tsx" })).rejects.toBeInstanceOf(
        InvalidArgumentError,
      );
      await expect(sandbox.createCodeContext({ language: "tsx" })).rejects.toBeInstanceOf(
        InvalidArgumentError,
      );
      await expect(
        sandbox.runCode("1", { language: "bash", context: "default" }),
      ).rejects.toBeInstanceOf(InvalidArgumentError);
      await expect(
        sandbox.runCode("1", { language: "ts", context: "default" }),
      ).rejects.toBeInstanceOf(InvalidArgumentError);
      expect(rayd.code.executeRequests.length).toBe(executes);
      expect(rayd.code.createRequests.length).toBe(creates);
      await sandbox.runCode("echo hi", { language: "bash" });
      const executions = rayd.code.executions.length;
      await expect(
        sandbox.runCode("echo $A", { language: "bash", envs: { A: "1" } }),
      ).rejects.toBeInstanceOf(InvalidArgumentError);
      expect(rayd.code.executions.length).toBe(executions);
    }
  });

  test.each(["bash", "javascript", "typescript"])(
    "%s on an image that does not ship it is InvalidArgumentError naming the poly image",
    async (language) => {
      const { sandbox, rayd } = await createTestSandbox();
      rayd.code.languages = new Set(["python"]);
      const error = await sandbox.runCode("1 + 1", { language }).catch((e) => e);
      expect(error).toBeInstanceOf(InvalidArgumentError);
      expect((error as InvalidArgumentError).grpcCode).toBe(Code.Unimplemented);
      expect((error as Error).message).toContain("rayito-base-poly");
      expect(rayd.code.executeRequests.at(-1)?.language).toBe(language);
      expect(rayd.code.lazyContexts).toEqual([]);
      const created = await sandbox.createCodeContext({ language }).catch((e) => e);
      expect(created).toBeInstanceOf(InvalidArgumentError);
      expect((created as InvalidArgumentError).grpcCode).toBe(Code.Unimplemented);
      expect((created as Error).message).toContain("rayito-base-poly");
      expect(rayd.code.createRequests.at(-1)?.language).toBe(language);
      expect((await sandbox.listCodeContexts()).map((ctx) => ctx.id)).toEqual(["default"]);
    },
  );
});
