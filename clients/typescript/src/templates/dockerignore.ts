/**
 * `.dockerignore` con la semántica de Docker (dominio puro, sin E/S). Espejo
 * de `rayito._templates._dockerignore`: los dos SDKs pasan los vectores de
 * `testdata/templates/dockerignore-vectors.json`.
 *
 * - Cada línea se recorta; las vacías y las que empiezan por `#` se ignoran.
 *   `!` delante niega el patrón; un `!` solo no es un patrón.
 * - El patrón se limpia como `filepath.Clean` (sin `/` repetidas, sin `.`,
 *   `..` resuelto, sin `/` inicial ni final) y queda anclado en la raíz del
 *   contexto: `README.md` no excluye `docs/README.md`.
 * - `*` y `?` nunca cruzan `/`; `[...]` es una clase (`!` o `^` la niegan,
 *   `a-z` es un rango); `\` escapa el carácter siguiente.
 * - Un segmento `**` equivale a cero o más segmentos completos, así que
 *   `**\/.env` excluye también el `.env` de la raíz (el idioma de
 *   `docker init`). Un `**` final exige al menos un segmento. Un `**` pegado
 *   a otros caracteres dentro de un segmento cuenta como `*`.
 * - Un patrón que coincide con un directorio padre excluye todo lo que hay
 *   debajo, y el último patrón que coincide (con la ruta o con un padre)
 *   gana.
 *
 * Sin expresiones regulares: el emparejado es por segmentos, con coste
 * polinómico, así que un `.dockerignore` hostil no dispara un retroceso
 * exponencial ni bloquea el bucle de eventos.
 */

export const DOCKERIGNORE_FILENAME = ".dockerignore";
const PATH_SEPARATOR = "/";
const COMMENT_PREFIX = "#";
const NEGATION_PREFIX = "!";
/** El segmento que equivale a cero o más segmentos completos. */
const GLOBSTAR = "**";
const CURRENT_DIR = ".";
const PARENT_DIR = "..";
const STAR = "*";
const ANY_CHAR = "?";
const ESCAPE = "\\";
const CLASS_OPEN = "[";
const CLASS_CLOSE = "]";
const CLASS_NEGATIONS: ReadonlySet<string> = new Set(["!", "^"]);
const CLASS_RANGE = "-";
const LINE_SEPARATOR = "\n";
/** Docker quita un BOM UTF-8 al principio del fichero (`buildkit` `ReadAll`). */
const UTF8_BOM = "\ufeff";

/** `*` (cualquier tira dentro del segmento) y `?` (un carácter). */
const WILDCARD_STAR = Symbol("star");
const WILDCARD_ANY = Symbol("any");

/** `[...]`: rangos `[desde, hasta]` inclusivos (puntos de código), quizá negados. */
interface CharClass {
  readonly ranges: ReadonlyArray<readonly [number, number]>;
  readonly negated: boolean;
}

/** Un literal de un carácter, un comodín o una clase. */
type Token = string | typeof WILDCARD_STAR | typeof WILDCARD_ANY | CharClass;

interface Pattern {
  /** `null` en el lugar de un segmento `**`. */
  readonly segments: ReadonlyArray<readonly Token[] | null>;
  readonly negated: boolean;
}

/** `filepath.Clean` + quitar la `/` inicial, como hace Docker antes de
 * compilar un patrón. `""` si no queda nada (no es un patrón). */
export function cleanPattern(pattern: string): string {
  const segments: string[] = [];
  for (const segment of pattern.split(PATH_SEPARATOR)) {
    if (!segment || segment === CURRENT_DIR) {
      continue;
    }
    if (segment === PARENT_DIR && segments.length > 0 && segments.at(-1) !== PARENT_DIR) {
      segments.pop();
      continue;
    }
    segments.push(segment);
  }
  return segments.join(PATH_SEPARATOR);
}

/** El carácter en `index` (el siguiente si es un `\` que escapa) y el índice tras él. */
function escapedChar(chars: readonly string[], index: number): [string, number] {
  const char = chars[index] as string;
  if (char === ESCAPE && index + 1 < chars.length) {
    return [chars[index + 1] as string, index + 2];
  }
  return [char, index + 1];
}

/** La clase que abre `chars[start] === "["` y el índice tras su `]`, o `null`
 * si no se cierra (entonces el `[` es un literal). Un `]` justo tras la
 * apertura es un literal, como en `filepath.Match`. */
function readClass(chars: readonly string[], start: number): [CharClass, number] | null {
  let index = start + 1;
  const negated = index < chars.length && CLASS_NEGATIONS.has(chars[index] as string);
  if (negated) {
    index += 1;
  }
  const ranges: Array<[number, number]> = [];
  while (index < chars.length) {
    if (chars[index] === CLASS_CLOSE && ranges.length > 0) {
      return [{ ranges, negated }, index + 1];
    }
    const [low, afterLow] = escapedChar(chars, index);
    index = afterLow;
    let high = low;
    if (
      index + 1 < chars.length &&
      chars[index] === CLASS_RANGE &&
      chars[index + 1] !== CLASS_CLOSE
    ) {
      [high, index] = escapedChar(chars, index + 1);
    }
    ranges.push([low.codePointAt(0) as number, high.codePointAt(0) as number]);
  }
  return null;
}

function tokenize(segment: string): Token[] {
  const chars = Array.from(segment);
  const tokens: Token[] = [];
  let index = 0;
  while (index < chars.length) {
    const char = chars[index] as string;
    if (char === CLASS_OPEN) {
      const parsed = readClass(chars, index);
      if (parsed !== null) {
        tokens.push(parsed[0]);
        index = parsed[1];
        continue;
      }
    }
    if (char === STAR) {
      if (tokens.at(-1) !== WILDCARD_STAR) {
        tokens.push(WILDCARD_STAR);
      }
      index += 1;
    } else if (char === ANY_CHAR) {
      tokens.push(WILDCARD_ANY);
      index += 1;
    } else {
      const [literal, next] = escapedChar(chars, index);
      tokens.push(literal);
      index = next;
    }
  }
  return tokens;
}

function tokenAdmits(token: Token, char: string): boolean {
  if (typeof token === "object") {
    const point = char.codePointAt(0) as number;
    const inside = token.ranges.some(([low, high]) => low <= point && point <= high);
    return inside !== token.negated;
  }
  return token === WILDCARD_ANY || token === char;
}

/** Glob de un solo segmento (`*` = cualquier tira, que aquí nunca lleva `/`):
 * el algoritmo voraz con vuelta al último `*`, O(tokens por nombre). */
function segmentMatches(tokens: readonly Token[], name: readonly string[]): boolean {
  let tokenIndex = 0;
  let nameIndex = 0;
  let starToken = -1;
  let starName = -1;
  while (nameIndex < name.length) {
    if (tokenIndex < tokens.length && tokens[tokenIndex] === WILDCARD_STAR) {
      starToken = tokenIndex;
      starName = nameIndex;
      tokenIndex += 1;
    } else if (
      tokenIndex < tokens.length &&
      tokenAdmits(tokens[tokenIndex] as Token, name[nameIndex] as string)
    ) {
      tokenIndex += 1;
      nameIndex += 1;
    } else if (starToken >= 0) {
      tokenIndex = starToken + 1;
      starName += 1;
      nameIndex = starName;
    } else {
      return false;
    }
  }
  while (tokenIndex < tokens.length && tokens[tokenIndex] === WILDCARD_STAR) {
    tokenIndex += 1;
  }
  return tokenIndex === tokens.length;
}

/** `**\/**` equivale a `**` (y al final sigue exigiendo un segmento):
 * colapsarlos acota el coste de un patrón hostil con miles de `**`. */
function collapseGlobstars(segments: readonly string[]): string[] {
  const collapsed: string[] = [];
  for (const segment of segments) {
    if (segment === GLOBSTAR && collapsed.at(-1) === GLOBSTAR) {
      continue;
    }
    collapsed.push(segment);
  }
  return collapsed;
}

function compile(cleaned: string, negated: boolean): Pattern {
  return {
    segments: collapseGlobstars(cleaned.split(PATH_SEPARATOR)).map((segment) =>
      segment === GLOBSTAR ? null : tokenize(segment),
    ),
    negated,
  };
}

/** Las longitudes `n` tales que el patrón casa con `path[:n]`: las posiciones
 * alcanzables tras recorrer los segmentos del patrón; un `**` abre todas
 * desde la menor (cero o más; uno o más si es el último). Una posición `n`
 * sólo depende de `path[:n]`, así que la misma pasada responde por la ruta y
 * por cada padre. */
function matchEnds(pattern: Pattern, path: ReadonlyArray<readonly string[]>): Set<number> {
  let reachable = new Set<number>([0]);
  const last = pattern.segments.length - 1;
  for (const [index, tokens] of pattern.segments.entries()) {
    const next = new Set<number>();
    if (tokens === null) {
      const start = smallest(reachable) + (index === last ? 1 : 0);
      for (let position = start; position <= path.length; position += 1) {
        next.add(position);
      }
    } else {
      for (const position of reachable) {
        if (position < path.length && segmentMatches(tokens, path[position] as string[])) {
          next.add(position + 1);
        }
      }
    }
    reachable = next;
    if (reachable.size === 0) {
      break;
    }
  }
  return reachable;
}

/** El menor de un conjunto no vacío, sin `Math.min(...set)` (que reparte
 * cada elemento como argumento). */
function smallest(values: ReadonlySet<number>): number {
  let least = Number.POSITIVE_INFINITY;
  for (const value of values) {
    least = Math.min(least, value);
  }
  return least;
}

/** ¿Casa con la ruta o con alguno de sus directorios padre? */
function matchesOrParent(pattern: Pattern, path: ReadonlyArray<readonly string[]>): boolean {
  for (const end of matchEnds(pattern, path)) {
    if (end > 0) {
      return true;
    }
  }
  return false;
}

/** Patrones de `.dockerignore`: `matches(relpath)` dice si excluir esa ruta
 * relativa al contexto (con `/` como separador, igual que Docker). */
export class DockerIgnore {
  readonly #patterns: readonly Pattern[];

  private constructor(patterns: readonly Pattern[]) {
    this.#patterns = patterns;
  }

  static fromText(text: string): DockerIgnore {
    const patterns: Pattern[] = [];
    const body = text.startsWith(UTF8_BOM) ? text.slice(UTF8_BOM.length) : text;
    for (const rawLine of body.split(LINE_SEPARATOR)) {
      const line = rawLine.trim();
      if (!line || line.startsWith(COMMENT_PREFIX)) {
        continue;
      }
      const negated = line.startsWith(NEGATION_PREFIX);
      const cleaned = cleanPattern((negated ? line.slice(1) : line).trim());
      if (cleaned) {
        patterns.push(compile(cleaned, negated));
      }
    }
    return new DockerIgnore(patterns);
  }

  static empty(): DockerIgnore {
    return new DockerIgnore([]);
  }

  /** El último patrón que coincide con `relpath` o con uno de sus directorios
   * padre decide; sin ninguno, la ruta entra. */
  matches(relpath: string): boolean {
    const path = relpath
      .split(PATH_SEPARATOR)
      .filter((segment) => segment)
      .map((segment) => Array.from(segment));
    let excluded = false;
    for (const pattern of this.#patterns) {
      if (pattern.negated !== excluded) {
        continue;
      }
      if (matchesOrParent(pattern, path)) {
        excluded = !pattern.negated;
      }
    }
    return excluded;
  }
}
