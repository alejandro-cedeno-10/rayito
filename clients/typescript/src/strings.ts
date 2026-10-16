/**
 * Recortes de cadenas en tiempo lineal y `repr` de Python.
 *
 * Sustituyen a `replace(/c+$/, "")` y `replace(/^c+|c+$/g, "")`: con un
 * cuantificador anclado al final, el motor de expresiones regulares reintenta
 * desde cada posición de una racha que no llega al final y el coste crece con
 * el cuadrado de la longitud (CodeQL `js/polynomial-redos`). Las cadenas que
 * pasan por aquí las entrega el llamante (URLs, rutas, tokens), así que una
 * entrada larga bloquearía el bucle de eventos del proceso anfitrión.
 */

/** `text` sin la racha final de `char` (un solo carácter). */
export function stripTrailing(text: string, char: string): string {
  let end = text.length;
  while (end > 0 && text[end - 1] === char) {
    end -= 1;
  }
  return text.slice(0, end);
}

/** `text` sin la racha inicial de `char` (un solo carácter). */
export function stripLeading(text: string, char: string): string {
  let start = 0;
  while (start < text.length && text[start] === char) {
    start += 1;
  }
  return text.slice(start);
}

const SINGLE_QUOTE = "'";
const DOUBLE_QUOTE = '"';
const PRINTABLE_MIN = 0x20;
const DELETE_CODE = 0x7f;
const HEX_RADIX = 16;
const HEX_WIDTH = 2;

function escapeRepr(character: string, quote: string): string {
  if (character === "\\" || character === quote) {
    return `\\${character}`;
  }
  if (character === "\n") {
    return "\\n";
  }
  if (character === "\t") {
    return "\\t";
  }
  if (character === "\r") {
    return "\\r";
  }
  const code = character.codePointAt(0) ?? 0;
  if (code < PRINTABLE_MIN || code === DELETE_CODE) {
    return `\\x${code.toString(HEX_RADIX).padStart(HEX_WIDTH, "0")}`;
  }
  return character;
}

/**
 * `repr(str)` de Python: comillas simples, o dobles si el texto lleva una
 * simple y ninguna doble. `stack deploy` imprime así los valores que cambian
 * (`'true' -> 'false'`).
 */
export function pythonStringRepr(text: string): string {
  const quote =
    text.includes(SINGLE_QUOTE) && !text.includes(DOUBLE_QUOTE) ? DOUBLE_QUOTE : SINGLE_QUOTE;
  let body = "";
  for (const character of text) {
    body += escapeRepr(character, quote);
  }
  return `${quote}${body}${quote}`;
}
