/**
 * `tests/e2e/image-cleanup.ts` sin AWS: el teardown del e2e de templates
 * borra cada imagen que construyó, tolera las que nunca llegaron a crearse,
 * reintenta mientras la imagen está ocupada y devuelve como fallo lo que no
 * pudo borrar, sin cortar el resto.
 */

import { DeleteMicrovmImageCommand, GetMicrovmImageCommand } from "@aws-sdk/client-lambda-microvms";
import { describe, expect, test } from "vitest";
import {
  BuiltImages,
  DEFAULT_TIMEOUT_MS,
  POLL_INTERVAL_MS,
  RETRY_BACKOFF_MS,
} from "../e2e/image-cleanup.js";

function awsError(name: string): Error {
  const error = new Error(name);
  error.name = name;
  return error;
}

type Outcome = Error | string | undefined;

/** Cada llamada consume el siguiente resultado de su guion (el último se
 * repite); sin guion, borrar funciona y la imagen ya no existe. */
class FakeMicrovms {
  readonly calls: [string, string][] = [];

  constructor(
    readonly deletes: Record<string, Outcome[]> = {},
    readonly states: Record<string, Outcome[]> = {},
  ) {}

  async send(command: unknown): Promise<unknown> {
    if (command instanceof DeleteMicrovmImageCommand) {
      const arn = command.input.imageIdentifier as string;
      this.calls.push(["delete", arn]);
      const outcome = FakeMicrovms.next(this.deletes[arn] ?? [undefined]);
      if (outcome instanceof Error) {
        throw outcome;
      }
      return { state: "DELETING" };
    }
    if (command instanceof GetMicrovmImageCommand) {
      const arn = command.input.imageIdentifier as string;
      this.calls.push(["get", arn]);
      const outcome = FakeMicrovms.next(
        this.states[arn] ?? [awsError("ResourceNotFoundException")],
      );
      if (outcome instanceof Error) {
        throw outcome;
      }
      return { state: outcome };
    }
    throw new Error("comando inesperado");
  }

  static next(script: Outcome[]): Outcome {
    return script.length > 1 ? script.shift() : script[0];
  }
}

function images(microvms: FakeMicrovms): {
  built: BuiltImages;
  sleeps: number[];
  clock: { now: number };
} {
  const clock = { now: 0 };
  const sleeps: number[] = [];
  const built = new BuiltImages({
    client: microvms,
    resolveArn: async (name) => `arn:${name}`,
    sleep: async (ms) => {
      sleeps.push(ms);
      clock.now += ms;
    },
    now: () => clock.now,
  });
  return { built, sleeps, clock };
}

describe("BuiltImages (teardown del e2e de templates)", () => {
  test("every tracked image is deleted and waited for", async () => {
    const microvms = new FakeMicrovms({}, { "arn:a": ["DELETING", "DELETING", "DELETED"] });
    const { built, sleeps } = images(microvms);
    expect(built.track("a")).toBe("a");
    built.track("b");

    expect(await built.deleteAll()).toEqual([]);
    expect(microvms.calls).toContainEqual(["delete", "arn:a"]);
    expect(microvms.calls).toContainEqual(["delete", "arn:b"]);
    expect(sleeps).toEqual([POLL_INTERVAL_MS, POLL_INTERVAL_MS]);
    expect(built.names).toEqual([]);
  });

  test("an image the build never created is fine", async () => {
    const microvms = new FakeMicrovms({ "arn:x": [awsError("ResourceNotFoundException")] });
    const { built } = images(microvms);
    built.track("x");
    expect(await built.deleteAll()).toEqual([]);
    expect(microvms.calls).toEqual([["delete", "arn:x"]]);
  });

  test("a busy image is retried with backoff", async () => {
    const microvms = new FakeMicrovms({
      "arn:a": [awsError("ConflictException"), awsError("ThrottlingException"), undefined],
    });
    const { built, sleeps } = images(microvms);
    built.track("a");
    expect(await built.deleteAll()).toEqual([]);
    expect(sleeps).toEqual(RETRY_BACKOFF_MS.slice(0, 2));
  });

  test("a failure is reported and the rest still deleted", async () => {
    const microvms = new FakeMicrovms(
      {
        "arn:denied": [awsError("AccessDeniedException")],
        "arn:busy": [awsError("ConflictException")],
      },
      { "arn:stuck": ["DELETE_FAILED"] },
    );
    const { built } = images(microvms);
    for (const name of ["denied", "busy", "stuck", "ok"]) {
      built.track(name);
    }

    expect(await built.deleteAll()).toEqual([
      "denied: AccessDeniedException",
      "busy: ConflictException",
      "stuck: DELETE_FAILED",
    ]);
    expect(microvms.calls).toContainEqual(["delete", "arn:ok"]);
    const busyDeletes = microvms.calls.filter(([op, arn]) => op === "delete" && arn === "arn:busy");
    expect(busyDeletes).toHaveLength(RETRY_BACKOFF_MS.length + 1);
  });

  test("waiting gives up after the timeout", async () => {
    const microvms = new FakeMicrovms({}, { "arn:slow": ["DELETING"] });
    const { built, clock } = images(microvms);
    built.track("slow");
    const [failure] = await built.deleteAll();
    expect(failure).toMatch(/^slow: sigue DELETING/);
    expect(clock.now).toBeGreaterThanOrEqual(DEFAULT_TIMEOUT_MS);
  });
});
