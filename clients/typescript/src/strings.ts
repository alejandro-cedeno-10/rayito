/**
 * Recortes de cadenas en tiempo lineal.
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
