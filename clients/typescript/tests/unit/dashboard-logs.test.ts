/**
 * El adaptador de logs del panel local: lee el stream del sandbox con las
 * funciones de `rayito sandbox logs`, acota las líneas y sólo reporta que hay
 * más; sin logs (grupo inexistente) devuelve una página vacía. Espejo de
 * `cli/test_dashboard_logs.py`.
 */

import { describe, expect, it } from "vitest";
import { CloudWatchLogsSource } from "../../src/dashboard/logs.js";
import { AuthenticationError } from "../../src/errors.js";
import { DASHBOARD_LOG_LINE_MAX_CHARS } from "../../src/limits.js";
import { sandboxInfo } from "../../src/models.js";
import {
  type DescribeStreamsRequest,
  type EventsPage,
  type GetEventsRequest,
  LogGroupNotFoundError,
  type LogsApi,
  type StreamsPage,
  tailEvents,
} from "../../src/sandbox-logs/service.js";
import { awsSdkError } from "./fake/dashboard.js";

const SANDBOX = "microvm-x";
const GROUP = "/rayito/rayito-base";
const STREAM = "2026/09/15[1.0]microvm-x";
const TIMESTAMP_MS = Date.UTC(2026, 8, 15, 14, 40);
const IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base";
const INFO = sandboxInfo({
  sandboxId: SANDBOX,
  state: "RUNNING",
  endpoint: "abc.lambda-microvm.us-east-1.on.aws",
  template: IMAGE_ARN,
  templateVersion: "1.0",
  startedAt: new Date(Date.UTC(2026, 8, 15, 14, 39)),
  maximumDurationSeconds: 3600,
});
const PLANE = { getMicrovm: async () => INFO };

function events(...messages: string[]): EventsPage {
  return {
    events: messages.map((message) => ({ timestamp: TIMESTAMP_MS, message })),
    nextForwardToken: undefined,
    nextBackwardToken: "start",
  };
}

/** Responde en orden y anota lo que se le pidió; lo que sobre o falte es un fallo del test. */
class ScriptedLogs implements LogsApi {
  readonly streamRequests: DescribeStreamsRequest[] = [];
  readonly eventRequests: GetEventsRequest[] = [];

  constructor(
    private readonly streams: Array<StreamsPage | Error>,
    private readonly pages: Array<EventsPage | Error> = [],
  ) {}

  async describeStreams(request: DescribeStreamsRequest): Promise<StreamsPage> {
    this.streamRequests.push(request);
    return answer(this.streams.shift());
  }

  async getEvents(request: GetEventsRequest): Promise<EventsPage> {
    this.eventRequests.push(request);
    return answer(this.pages.shift());
  }
}

function answer<T>(next: T | Error | undefined): T {
  if (next === undefined) {
    throw new Error("llamada sin respuesta preparada");
  }
  if (next instanceof Error) {
    throw next;
  }
  return next;
}

function named(...names: string[]): StreamsPage {
  return { names, nextToken: undefined };
}

describe("CloudWatchLogsSource", () => {
  it("reads the stream and trims each line", async () => {
    const longLine = "x".repeat(DASHBOARD_LOG_LINE_MAX_CHARS + 50);
    const logs = new ScriptedLogs([named(STREAM)], [events("hola\n", longLine), events()]);
    const page = await new CloudWatchLogsSource(logs, PLANE).read(SANDBOX, { limit: 10 });

    expect(page.truncated).toBe(false);
    expect(page.fallback).toBe(false);
    expect(page.lines[0]?.message).toBe("hola");
    expect(page.lines[1]?.message).toHaveLength(DASHBOARD_LOG_LINE_MAX_CHARS);
    expect(page.lines[0]?.stream).toBe(STREAM);
    expect(page.lines[0]?.timestamp).toBe("2026-09-15T14:40:00.000Z");
    expect(
      logs.eventRequests.map((request) => [request.limit, request.nextToken, request.fromEnd]),
    ).toEqual([
      [11, undefined, true],
      [9, "start", true],
    ]);
  });

  it("says when there are older lines and keeps the newest", async () => {
    const logs = new ScriptedLogs([named(STREAM)], [events("a", "b", "c")]);
    const page = await new CloudWatchLogsSource(logs, PLANE).read(SANDBOX, { limit: 2 });
    expect(page.lines.map((line) => line.message)).toEqual(["b", "c"]);
    expect(page.truncated).toBe(true);
  });

  it("flags the name search fallback", async () => {
    const found = "2099/01/01[1.0]microvm-x";
    const logs = new ScriptedLogs([named(), named(found)], [events("a"), events()]);
    const page = await new CloudWatchLogsSource(logs, PLANE).read(SANDBOX, { limit: 10 });
    expect(page.fallback).toBe(true);
    expect(page.lines[0]?.stream).toBe(found);
  });

  it("does not flag the expected stream", async () => {
    const logs = new ScriptedLogs([named(STREAM)], [events("a"), events()]);
    const page = await new CloudWatchLogsSource(logs, PLANE).read(SANDBOX, { limit: 10 });
    expect(page.fallback).toBe(false);
  });

  it("a missing log group is an empty page", async () => {
    const logs = new ScriptedLogs([new LogGroupNotFoundError()]);
    const page = await new CloudWatchLogsSource(logs, PLANE).read(SANDBOX, { limit: 10 });
    expect(page).toEqual({ lines: [], truncated: false, fallback: false });
  });

  it("a sandbox without logs (no stream anywhere) is an empty page", async () => {
    const logs = new ScriptedLogs([named(), named()]);
    const page = await new CloudWatchLogsSource(logs, PLANE).read(SANDBOX, { limit: 10 });
    expect(page.lines).toEqual([]);
  });

  it("the log group can be overridden", async () => {
    const logs = new ScriptedLogs([named(), named()]);
    const page = await new CloudWatchLogsSource(logs, PLANE, { logGroup: "/custom" }).read(
      SANDBOX,
      {
        limit: 10,
      },
    );
    expect(page.lines).toEqual([]);
    expect(logs.streamRequests.map((request) => request.group)).toEqual(["/custom", "/custom"]);
  });

  it("a denied logs call is an SDK authentication error with the AWS code only", async () => {
    const logs = new ScriptedLogs([
      awsSdkError("detalle interno 123456789012", "AccessDeniedException"),
    ]);
    const error = await new CloudWatchLogsSource(logs, PLANE).read(SANDBOX, { limit: 10 }).then(
      () => undefined,
      (caught: unknown) => caught as AuthenticationError,
    );
    expect(error).toBeInstanceOf(AuthenticationError);
    expect(error?.awsCode).toBe("AccessDeniedException");
    expect(error?.message).not.toContain("123456789012");
  });
});

describe("tailEvents", () => {
  it("walks backwards until it has the limit and keeps chronological order", async () => {
    const logs = new ScriptedLogs(
      [],
      [
        {
          events: [{ timestamp: 3, message: "c" }],
          nextForwardToken: undefined,
          nextBackwardToken: "t1",
        },
        {
          events: [{ timestamp: 2, message: "b" }],
          nextForwardToken: undefined,
          nextBackwardToken: "t2",
        },
        {
          events: [{ timestamp: 1, message: "a" }],
          nextForwardToken: undefined,
          nextBackwardToken: "t3",
        },
      ],
    );
    const tail = await tailEvents(logs, { group: GROUP, stream: STREAM, limit: 3 });
    expect(tail.map((event) => event.message)).toEqual(["a", "b", "c"]);
    expect(logs.eventRequests.map((request) => request.limit)).toEqual([3, 2, 1]);
  });

  it("stops when the token repeats (start of the stream)", async () => {
    const page: EventsPage = {
      events: [{ timestamp: 1, message: "a" }],
      nextForwardToken: undefined,
      nextBackwardToken: "same",
    };
    const logs = new ScriptedLogs([], [page, page]);
    const tail = await tailEvents(logs, { group: GROUP, stream: STREAM, limit: 10 });
    expect(tail.map((event) => event.message)).toEqual(["a", "a"]);
  });
});
