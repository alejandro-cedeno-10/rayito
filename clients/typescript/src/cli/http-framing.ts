/**
 * Delimitación de mensajes HTTP/1.1 para `rayito sandbox proxy` (dominio
 * puro, sin E/S): dónde acaba el cuerpo de una petición o de una respuesta
 * (RFC 9112 §6.3), para que el proxy reenvíe exactamente un mensaje y nada de
 * lo que venga detrás. Espejo de `rayito.cli._http_framing`; los dos
 * ejecutan los vectores de `testdata/cli/proxy-vectors.json`.
 *
 * - Petición: `Transfer-Encoding` cuya última codificación es `chunked` →
 *   troceado; `Content-Length` válido → esa longitud; ninguno → sin cuerpo.
 *   Las ambigüedades que un intermediario no debe resolver por su cuenta
 *   (`Transfer-Encoding` junto a `Content-Length`, un `Transfer-Encoding` que
 *   no acaba en `chunked`, varios `Content-Length` distintos o no numéricos)
 *   son `MalformedFramingError`: el proxy responde `400` sin reenviar nada.
 * - Respuesta: `1xx`, `204`, `304` o la respuesta a un `HEAD` no llevan
 *   cuerpo; si no, `chunked`, `Content-Length` o, sin ninguno de los dos,
 *   hasta que el upstream cierre.
 */

export const TRANSFER_ENCODING_HEADER_NAME = "Transfer-Encoding";
export const CONTENT_LENGTH_HEADER_NAME = "Content-Length";
const CHUNKED_CODING = "chunked";
const HEAD_METHOD = "HEAD";
const LIST_SEPARATOR = ",";
const CHUNK_EXTENSION_SEPARATOR = ";";
const CHUNK_LINE_END = "\r\n";
const HEX_RADIX = 16;
/** RFC 9112 §7.1: el tamaño de un trozo es un número hexadecimal sin signo. */
const HEX_DIGITS = /^[0-9a-fA-F]+$/;
const DECIMAL_DIGITS = /^[0-9]+$/;
/**
 * RFC 9110 §5.6.3 `OWS`: lo único que se recorta alrededor de un valor de
 * cabecera o de un elemento de lista. `String.trim()` quita también ` `
 * o `﻿`, y un `Content-Length: 12 ` tendría longitud 12 aquí y un
 * upstream estricto lo rechazaría.
 */
const OWS_CHARACTERS: ReadonlySet<string> = new Set([" ", "\t"]);
/** RFC 9110 §15.2: `1xx` nunca lleva cuerpo. */
const INFORMATIONAL_MIN = 100;
const INFORMATIONAL_MAX = 199;
export const SWITCHING_PROTOCOLS = 101;
const NO_CONTENT = 204;
const NOT_MODIFIED = 304;
const STATUS_LINE = /^HTTP\/1\.[01] (\d{3})(?: .*)?$/;

/** La delimitación del cuerpo es ambigua o inválida. Nunca lleva el contenido de la petición. */
export class MalformedFramingError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MalformedFramingError";
  }
}

export type BodyKind = "none" | "length" | "chunked" | "until-close";

export interface BodyFraming {
  readonly kind: BodyKind;
  readonly length: number;
}

export const NO_BODY: BodyFraming = { kind: "none", length: 0 };
const CHUNKED_BODY: BodyFraming = { kind: "chunked", length: 0 };
const UNTIL_CLOSE_BODY: BodyFraming = { kind: "until-close", length: 0 };

/** `text` sin los espacios y tabuladores (`OWS`) de los extremos. */
export function trimOws(text: string): string {
  let start = 0;
  let end = text.length;
  while (start < end && OWS_CHARACTERS.has(text.charAt(start))) {
    start += 1;
  }
  while (end > start && OWS_CHARACTERS.has(text.charAt(end - 1))) {
    end -= 1;
  }
  return text.slice(start, end);
}

function listValues(values: readonly string[]): string[] {
  return values.flatMap((value) =>
    value.split(LIST_SEPARATOR).map((item) => trimOws(item).toLowerCase()),
  );
}

/** El número que un campo de longitud lleva, acotado a lo que `Number` representa sin pérdida. */
function safeInteger(digits: string, radix: number, message: string): number {
  const value = Number.parseInt(digits, radix);
  if (!Number.isSafeInteger(value)) {
    throw new MalformedFramingError(message);
  }
  return value;
}

/**
 * La longitud común de todos los `Content-Length` (RFC 9110 §8.6 admite
 * repeticiones idénticas), `undefined` sin ninguno.
 */
function contentLength(values: readonly string[]): number | undefined {
  if (values.length === 0) {
    return undefined;
  }
  const items = new Set(listValues(values));
  if (items.size !== 1) {
    throw new MalformedFramingError("varios Content-Length distintos");
  }
  const [item = ""] = items;
  if (!DECIMAL_DIGITS.test(item)) {
    throw new MalformedFramingError("Content-Length no numérico");
  }
  return safeInteger(item, 10, "Content-Length demasiado grande");
}

/** El cuerpo de una petición a partir de todos sus `Transfer-Encoding` y `Content-Length`. */
export function requestBodyFraming(
  transferEncodings: readonly string[],
  contentLengths: readonly string[],
): BodyFraming {
  if (transferEncodings.length > 0) {
    if (contentLengths.length > 0) {
      throw new MalformedFramingError("Transfer-Encoding junto a Content-Length");
    }
    const codings = listValues(transferEncodings).filter((coding) => coding !== "");
    if (codings.at(-1) !== CHUNKED_CODING) {
      throw new MalformedFramingError("Transfer-Encoding que no acaba en chunked");
    }
    return CHUNKED_BODY;
  }
  const length = contentLength(contentLengths);
  return length === undefined || length === 0 ? NO_BODY : { kind: "length", length };
}

/** El código de una línea de estado HTTP/1.x; `MalformedFramingError` si no lo es. */
export function responseStatus(statusLine: string): number {
  const match = STATUS_LINE.exec(statusLine);
  if (match === null) {
    throw new MalformedFramingError("línea de estado inválida");
  }
  return Number.parseInt(match[1] ?? "", 10);
}

export function isInformational(status: number): boolean {
  return status >= INFORMATIONAL_MIN && status <= INFORMATIONAL_MAX;
}

export interface ResponseFramingInput {
  readonly requestMethod: string;
  readonly transferEncodings: readonly string[];
  readonly contentLengths: readonly string[];
}

function declaredResponseLength(contentLengths: readonly string[]): number | undefined {
  try {
    return contentLength(contentLengths);
  } catch (error) {
    if (error instanceof MalformedFramingError) {
      return undefined;
    }
    throw error;
  }
}

/**
 * El cuerpo de una respuesta (RFC 9112 §6.3). Un `Content-Length` inválido
 * del upstream se trata como "hasta que cierre", que nunca lee de más.
 */
export function responseBodyFraming(status: number, input: ResponseFramingInput): BodyFraming {
  if (isInformational(status) || status === NO_CONTENT || status === NOT_MODIFIED) {
    return NO_BODY;
  }
  if (input.requestMethod.toUpperCase() === HEAD_METHOD) {
    return NO_BODY;
  }
  if (input.transferEncodings.length > 0) {
    return listValues(input.transferEncodings).at(-1) === CHUNKED_CODING
      ? CHUNKED_BODY
      : UNTIL_CLOSE_BODY;
  }
  const length = declaredResponseLength(input.contentLengths);
  if (length === undefined) {
    return UNTIL_CLOSE_BODY;
  }
  return length === 0 ? NO_BODY : { kind: "length", length };
}

/** El tamaño de una línea `chunk-size [; ext] CRLF` (RFC 9112 §7.1), ya decodificada como latin-1. */
export function chunkSize(line: string): number {
  const withoutLineEnd = line.endsWith(CHUNK_LINE_END)
    ? line.slice(0, -CHUNK_LINE_END.length)
    : line;
  const [size = ""] = withoutLineEnd.split(CHUNK_EXTENSION_SEPARATOR, 1);
  const digits = trimOws(size);
  if (!HEX_DIGITS.test(digits)) {
    throw new MalformedFramingError("chunk-size inválido");
  }
  return safeInteger(digits, HEX_RADIX, "chunk-size demasiado grande");
}
