/** El lector SSE del cliente de despliegues (`agent/sse.ts`). Espejo de
 * `test_agent_sse.py`. */

import { describe, expect, test } from "vitest";
import { type SseMessage, SseParser } from "../../src/agent/sse.js";

const encoder = new TextEncoder();
const STREAM = new Uint8Array([
  ...encoder.encode(": comentario\r\n"),
  ...encoder.encode('event: step_started\r\ndata: {"index": 1}\r\n\r\n'),
  ...encoder.encode("data: linea 1\ndata: linea 2\n\n"),
  ...encoder.encode('id: 7\nretry: 100\nevent: done\ndata: {"ñ": 1}\r\r'),
]);
const EXPECTED: SseMessage[] = [
  { event: "step_started", data: '{"index": 1}' },
  { event: "message", data: "linea 1\nlinea 2" },
  { event: "done", data: '{"ñ": 1}' },
];

function parseAll(chunks: Uint8Array[]): SseMessage[] {
  const parser = new SseParser();
  return chunks.flatMap((chunk) => parser.feed(chunk));
}

describe("SseParser", () => {
  test("parses a whole stream", () => {
    expect(parseAll([STREAM])).toEqual(EXPECTED);
  });

  test.each([1, 2, 3, 5, 7, 11])("chunking by %i bytes never changes the messages", (size) => {
    const chunks: Uint8Array[] = [];
    for (let index = 0; index < STREAM.length; index += size) {
      chunks.push(STREAM.slice(index, index + size));
    }
    expect(parseAll(chunks)).toEqual(EXPECTED);
  });

  test("a split utf-8 character is decoded whole", () => {
    const data = encoder.encode("event: text\ndata: ñandú\n\n");
    const cut = data.indexOf(0xc3) + 1;
    expect(parseAll([data.slice(0, cut), data.slice(cut)])).toEqual([
      { event: "text", data: "ñandú" },
    ]);
  });

  test("an incomplete message is dropped", () => {
    expect(parseAll([encoder.encode("event: done\ndata: {}")])).toEqual([]);
    expect(parseAll([encoder.encode("data: a\n\ndata: b")])).toEqual([
      { event: "message", data: "a" },
    ]);
  });

  test("an empty data block dispatches nothing and resets the event", () => {
    expect(parseAll([encoder.encode("event: x\n\ndata: y\n\n")])).toEqual([
      { event: "message", data: "y" },
    ]);
  });

  test("a crlf split between chunks is one line end", () => {
    expect(
      parseAll([encoder.encode("data: a\r"), encoder.encode("\n\r"), encoder.encode("\n")]),
    ).toEqual([{ event: "message", data: "a" }]);
  });
});
