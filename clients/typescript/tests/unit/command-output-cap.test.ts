/**
 * Tope de la salida que el cliente guarda de un comando o una PTY
 * (`maxOutputBytes`, `COMMAND_OUTPUT_MAX_BYTES` por defecto): se guarda la
 * cola, lo descartado se cuenta y `truncated` lo dice; los callbacks reciben
 * siempre todo. Espejo de `test_command_output_cap.py`.
 */

import { describe, expect, test } from "vitest";
import { CommandExitError, InvalidArgumentError } from "../../src/errors.js";
import { COMMAND_OUTPUT_MAX_BYTES } from "../../src/limits.js";
import {
  DecodedStream,
  OutputAccumulator,
  validateMaxOutputBytes,
} from "../../src/sandbox/commands.js";
import { CHUNK_SIZE } from "./fake/process.js";
import { createTestSandbox } from "./helpers.js";

const bytes = (text: string): Uint8Array => new TextEncoder().encode(text);

describe("DecodedStream / OutputAccumulator cap", () => {
  test("keeps only the tail and counts the rest; the callback sees everything", () => {
    const seen: string[] = [];
    const stream = new DecodedStream((text) => seen.push(text), 10);
    for (const piece of ["0123456", "789ab", "cdef"]) {
      stream.feed(bytes(piece));
    }
    stream.flush();
    expect(stream.text).toBe("6789abcdef");
    expect(stream.droppedBytes).toBe(6);
    expect(stream.truncated).toBe(true);
    expect(seen.join("")).toBe("0123456789abcdef");
  });

  test("under the cap nothing is truncated", () => {
    const stream = new DecodedStream(undefined, 10);
    stream.feed(bytes("hola"));
    expect(stream.text).toBe("hola");
    expect(stream.truncated).toBe(false);
  });

  test("a zero cap keeps nothing", () => {
    const seen: string[] = [];
    const stream = new DecodedStream((text) => seen.push(text), 0);
    stream.feed(bytes("señal"));
    stream.flush();
    expect(stream.text).toBe("");
    expect(seen.join("")).toBe("señal");
  });

  test("PTY bytes are bounded too", () => {
    const accumulator = new OutputAccumulator({ maxBytes: 4 });
    for (let seq = 1; seq <= 5; seq += 1) {
      accumulator.feedTerminal(seq, bytes("ab"));
    }
    expect(accumulator.stdout).toBe("abab");
    expect(accumulator.truncated).toBe(true);
    expect(accumulator.lastSeq).toBe(5);
  });

  test("the default cap is the shared limit", () => {
    expect(COMMAND_OUTPUT_MAX_BYTES).toBe(64 * 1024 * 1024);
  });

  test.each([-1, 1.5, Number.NaN])("invalid caps are rejected (%s)", (value) => {
    expect(() => validateMaxOutputBytes(value)).toThrow(InvalidArgumentError);
  });
});

describe("commands.run with maxOutputBytes", () => {
  test("keeps the tail and flags truncation", async () => {
    const { sandbox } = await createTestSandbox();
    let seen = 0;
    const result = await sandbox.commands.run(`big ${3 * CHUNK_SIZE}`, {
      maxOutputBytes: CHUNK_SIZE,
      onStdout: (text) => {
        seen += text.length;
      },
    });
    expect(result.stdout.length).toBe(CHUNK_SIZE);
    expect(result.truncated).toBe(true);
    expect(seen).toBe(3 * CHUNK_SIZE);
  });

  test("under the cap the result has no truncated flag", async () => {
    const { sandbox } = await createTestSandbox();
    const result = await sandbox.commands.run("echo hola");
    expect(result).toEqual({ stdout: "hola\n", stderr: "", exitCode: 0, error: undefined });
  });

  test("a negative cap fails before any RPC", async () => {
    const { sandbox, rayd } = await createTestSandbox();
    const before = rayd.process.startRequests.length;
    await expect(sandbox.commands.run("echo x", { maxOutputBytes: -1 })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    expect(rayd.process.startRequests.length).toBe(before);
  });

  test("a failing command carries the flag", async () => {
    const error = new CommandExitError("x", { exitCode: 1, truncated: true });
    expect(error.truncated).toBe(true);
    expect(new CommandExitError("x", { exitCode: 1 }).truncated).toBe(false);
  });
});
