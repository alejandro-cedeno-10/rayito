/**
 * `src/cli/closed-pipe.ts`: un `EPIPE` en la salida termina la CLI en
 * silencio con `EXIT_FAILURE`, como la de Python
 * (`test_cli_closed_pipe.py`); cualquier otro error del stream se relanza.
 */

import { EventEmitter } from "node:events";
import { describe, expect, test } from "vitest";
import { exitOnClosedPipe, isBrokenPipe } from "../../../src/cli/closed-pipe.js";
import { EXIT_FAILURE } from "../../../src/cli/exit-codes.js";

function errnoError(code: string): NodeJS.ErrnoException {
  return Object.assign(new Error(`write ${code}`), { code });
}

describe("closed pipe", () => {
  test("an EPIPE exits quietly with the failure code", () => {
    const stream = new EventEmitter();
    const exits: number[] = [];
    exitOnClosedPipe(stream, (code) => exits.push(code));

    stream.emit("error", errnoError("EPIPE"));

    expect(exits).toEqual([EXIT_FAILURE]);
  });

  test("any other stream error is not swallowed", () => {
    const stream = new EventEmitter();
    const exits: number[] = [];
    exitOnClosedPipe(stream, (code) => exits.push(code));

    expect(() => stream.emit("error", errnoError("EIO"))).toThrow("write EIO");
    expect(exits).toEqual([]);
  });

  test("only the EPIPE code counts as a closed pipe", () => {
    expect(isBrokenPipe(errnoError("EPIPE"))).toBe(true);
    expect(isBrokenPipe(errnoError("ECONNRESET"))).toBe(false);
    expect(isBrokenPipe("EPIPE")).toBe(false);
    expect(isBrokenPipe(undefined)).toBe(false);
  });
});
