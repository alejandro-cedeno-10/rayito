/**
 * Lector incremental de Server-Sent Events (ADR-028, D6). Espejo de
 * `rayito._agent._sse`: bytes que llegan a trozos del cuerpo de
 * `POST /invocations` con `Accept: text/event-stream` salen como mensajes
 * `{ event, data }`. Puro (sin E/S).
 *
 * Sigue la sección 9.2 del estándar HTML (WHATWG, "Server-sent events"): las
 * líneas acaban en `\n`, `\r` o `\r\n`; `data:` se acumula con saltos de
 * línea; `event:` fija el tipo (por defecto `message`); un comentario (`:`) y
 * los campos `id`/`retry` se ignoran; una línea en blanco cierra el mensaje.
 * Un mensaje incompleto al cerrar el stream se descarta, como manda el
 * estándar.
 */

export const DEFAULT_SSE_EVENT = "message";

/** Un mensaje SSE completo. */
export interface SseMessage {
  readonly event: string;
  readonly data: string;
}

const LF = 0x0a;
const CR = 0x0d;

export class SseParser {
  #partial: Uint8Array = new Uint8Array(0);
  #event = "";
  #data: string[] = [];
  #afterCr = false;
  readonly #decoder = new TextDecoder("utf-8");

  /** Alimenta con un trozo; devuelve los mensajes que completó. Guarda en
   * bytes la línea a medias, así que un carácter UTF-8 partido entre dos
   * trozos se decodifica bien. */
  feed(chunk: Uint8Array): SseMessage[] {
    let data = chunk;
    if (this.#afterCr && data[0] === LF) {
      data = data.subarray(1);
    }
    if (data.length > 0) {
      this.#afterCr = data[data.length - 1] === CR;
    }
    const joined = new Uint8Array(this.#partial.length + data.length);
    joined.set(this.#partial, 0);
    joined.set(data, this.#partial.length);
    const messages: SseMessage[] = [];
    let start = 0;
    for (let index = 0; index < joined.length; index += 1) {
      const byte = joined[index];
      if (byte !== LF && byte !== CR) {
        continue;
      }
      const message = this.#line(this.#decoder.decode(joined.subarray(start, index)));
      if (message !== undefined) {
        messages.push(message);
      }
      if (byte === CR && joined[index + 1] === LF) {
        index += 1;
      }
      start = index + 1;
    }
    this.#partial = joined.slice(start);
    return messages;
  }

  #line(line: string): SseMessage | undefined {
    if (line === "") {
      return this.#dispatch();
    }
    if (line.startsWith(":")) {
      return undefined;
    }
    const separator = line.indexOf(":");
    const name = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) {
      value = value.slice(1);
    }
    if (name === "event") {
      this.#event = value;
    } else if (name === "data") {
      this.#data.push(value);
    }
    return undefined;
  }

  #dispatch(): SseMessage | undefined {
    if (this.#data.length === 0) {
      this.#event = "";
      return undefined;
    }
    const message = {
      event: this.#event === "" ? DEFAULT_SSE_EVENT : this.#event,
      data: this.#data.join("\n"),
    };
    this.#event = "";
    this.#data = [];
    return message;
  }
}
