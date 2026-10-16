/**
 * Temas del panel: la paleta, los radios, las fuentes y los espacios salen
 * de variables CSS (tokens de diseño), y un tema de usuario es un fichero
 * pequeño con los valores de esos tokens.
 *
 * Un tema se valida antes de servirse y no puede inyectar CSS ni scripts:
 *
 * - Las claves son una lista cerrada (`COLOR_TOKENS`, `LENGTH_TOKENS` y
 *   `FONT_TOKENS`); una clave desconocida se rechaza.
 * - Cada valor debe cumplir la gramática de su clase: color (`#rgb`,
 *   `#rrggbb`, `#rrggbbaa`, `rgb()`/`hsl()` numéricos), longitud (número con
 *   `px`, `rem` o `em`) o lista de familias tipográficas (nombres sueltos o
 *   entre comillas, sin `url()` ni `@font-face`: no hay fuentes remotas).
 * - El CSS lo genera este módulo desde los valores ya validados: el texto
 *   del fichero nunca se copia tal cual.
 *
 * Dominio puro, sin ficheros ni sockets: la cáscara local lee los ficheros y
 * llama a `parseTheme`. Espejo de `rayito._dashboard._theme`; los vectores de
 * `testdata/dashboard/theme-vectors.json` los ejecutan los dos.
 */

import { InvalidArgumentError } from "../errors.js";
import { DASHBOARD_THEME_MAX_BYTES } from "../limits.js";
import type { Logo } from "./logo.js";

export const THEME_FORMAT_VERSION = 1 as const;
export const BUILTIN_THEME_NAME = "rayito" as const;
export const CUSTOM_THEME_NAME = "custom" as const;
export const THEME_NAME_MAX_CHARS = 40 as const;
export const SHOWN_KEY_MAX_CHARS = 40 as const;
const FONT_NAME_MAX_CHARS = 40;
const FONT_LIST_MAX_EXTRA_NAMES = 7;

/** Los colores que cambian entre el esquema claro y el oscuro. */
export const COLOR_TOKENS = [
  "bg",
  "surface",
  "surface-raised",
  "border",
  "border-strong",
  "fg",
  "fg-muted",
  "primary",
  "on-primary",
  "accent",
  "accent-strong",
  "focus",
  "ok",
  "warning",
  "critical",
  "info",
  "chart-1",
  "chart-2",
  "chart-3",
] as const;
/** Radios y espacios: iguales en los dos esquemas. */
export const LENGTH_TOKENS = [
  "radius",
  "radius-lg",
  "space-1",
  "space-2",
  "space-3",
  "space-4",
  "space-6",
  "sidebar-width",
] as const;
export const FONT_TOKENS = ["font-sans", "font-mono"] as const;
export const ALL_TOKENS: readonly string[] = [...COLOR_TOKENS, ...LENGTH_TOKENS, ...FONT_TOKENS];

type TokenKind = "color" | "length" | "font";
const KIND_BY_TOKEN: ReadonlyMap<string, TokenKind> = new Map<string, TokenKind>([
  ...COLOR_TOKENS.map((token): [string, TokenKind] => [token, "color"]),
  ...LENGTH_TOKENS.map((token): [string, TokenKind] => [token, "length"]),
  ...FONT_TOKENS.map((token): [string, TokenKind] => [token, "font"]),
]);
const SCHEMES = ["dark", "light"] as const;
const SECTION_SHARED = "tokens";
const JSON_KEYS: ReadonlySet<string> = new Set(["version", "name", SECTION_SHARED, ...SCHEMES]);

const NUMBER = String.raw`\d{1,3}(?:\.\d{1,3})?`;
const CHANNEL_SEPARATOR = String.raw`(?:\s*,\s*|\s+)`;
const FONT_NAME = `(?:"[A-Za-z0-9 _-]{1,${FONT_NAME_MAX_CHARS}}"|[A-Za-z0-9_-]{1,${FONT_NAME_MAX_CHARS}})`;

function wholeMatch(source: string): RegExp {
  return new RegExp(`^(?:${source})$`);
}

const VALUE_PATTERNS: Readonly<Record<TokenKind, RegExp>> = {
  color: wholeMatch(
    `#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})` +
      String.raw`|(?:rgb|hsl)a?\(\s*${NUMBER}(?:deg|%)?${CHANNEL_SEPARATOR}${NUMBER}%?` +
      String.raw`${CHANNEL_SEPARATOR}${NUMBER}%?(?:\s*[,/]\s*(?:${NUMBER}%?|0?\.\d{1,3}))?\s*\)`,
  ),
  length: wholeMatch(`0|${NUMBER}(?:px|rem|em)`),
  font: wholeMatch(String.raw`${FONT_NAME}(?:\s*,\s*${FONT_NAME}){0,${FONT_LIST_MAX_EXTRA_NAMES}}`),
};
const NAME_PATTERN = wholeMatch(`[A-Za-z0-9][A-Za-z0-9 _.-]{0,${THEME_NAME_MAX_CHARS - 1}}`);
const KEY_PATTERN = wholeMatch(`[A-Za-z0-9_-]{1,${SHOWN_KEY_MAX_CHARS}}`);

const CSS_COMMENT = /\/\*[\s\S]*?\*\//g;
const CSS_BLOCK = /([^{}]+)\{([^{}]*)\}/g;
const CSS_DECLARATION = /^--([A-Za-z0-9-]+)\s*:\s*([^;{}]+)$/;
const CSS_SECTIONS: ReadonlyMap<string, string> = new Map([
  [':root[data-theme="dark"]', "dark"],
  [':root[data-theme="light"]', "light"],
  [":root", SECTION_SHARED],
]);

type TokenValues = Readonly<Record<string, string>>;
type Document = Readonly<Record<string, unknown>>;

/** Lo que la SPA sabe del tema por `/api/v1/meta`: el nombre y si hay logo. */
export interface Branding {
  readonly name: string;
  readonly logo: boolean;
}

export const DEFAULT_BRANDING: Branding = { name: BUILTIN_THEME_NAME, logo: false };

/**
 * Los valores de tokens ya validados: `shared` (radios, espacios, fuentes y
 * los colores de los dos esquemas) y un conjunto de colores por esquema, que
 * gana sobre `shared`.
 */
export class Theme {
  readonly name: string;
  readonly shared: TokenValues;
  readonly dark: TokenValues;
  readonly light: TokenValues;

  constructor(options: {
    readonly name?: string;
    readonly shared?: TokenValues;
    readonly dark?: TokenValues;
    readonly light?: TokenValues;
  }) {
    this.name = options.name ?? CUSTOM_THEME_NAME;
    this.shared = options.shared ?? {};
    this.dark = options.dark ?? {};
    this.light = options.light ?? {};
  }

  /**
   * La hoja de estilos que se sirve en `/theme.css`: los mismos selectores
   * que los temas de fábrica (`tokens.css`), para ganar por orden y no por
   * especificidad. El esquema oscuro es el de base, el claro entra con
   * `data-theme="light"` o, en automático, con el sistema.
   */
  css(): string {
    const blocks: string[] = [];
    const schemes: ReadonlyArray<readonly [string, string, TokenValues]> = [
      ["dark", ':root, :root[data-theme="dark"]', this.dark],
      ["light", ':root[data-theme="light"]', this.light],
    ];
    for (const [scheme, selector, colors] of schemes) {
      const body = declarations({ ...this.shared, ...colors });
      if (body.length === 0) {
        continue;
      }
      blocks.push(`/* ${scheme} */\n${selector} {\n${body.join("\n")}\n}`);
      if (scheme === "light") {
        blocks.push(
          "@media (prefers-color-scheme: light) {\n" +
            '  :root:not([data-theme="dark"]) {\n' +
            `${body.map((line) => `  ${line}`).join("\n")}\n  }\n}`,
        );
      }
    }
    return `/* Tema «${this.name}» (generado) */\n${blocks.join("\n")}\n`;
  }
}

function declarations(values: TokenValues): string[] {
  return ALL_TOKENS.filter((key) => key in values).map((key) => `  --${key}: ${values[key]};`);
}

function shown(key: string): string {
  return KEY_PATTERN.test(key) ? key : "(clave no válida)";
}

function isRecord(value: unknown): value is Document {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validated(section: string, values: unknown, allowed: readonly string[]): TokenValues {
  if (!isRecord(values)) {
    throw new InvalidArgumentError(`tema: «${section}» debe ser un objeto de clave y valor`);
  }
  const checked: Record<string, string> = {};
  for (const [key, value] of Object.entries(values)) {
    if (!allowed.includes(key)) {
      throw new InvalidArgumentError(`tema: clave desconocida en «${section}»: ${shown(key)}`);
    }
    const kind = KIND_BY_TOKEN.get(key) as TokenKind;
    if (typeof value !== "string" || !VALUE_PATTERNS[kind].test(value.trim())) {
      throw new InvalidArgumentError(`tema: valor no válido para ${key} (se espera ${kind})`);
    }
    checked[key] = value.trim();
  }
  return checked;
}

/**
 * Valida el texto de un tema (`format` es `json` o `css`) y lo devuelve.
 * Lanza `InvalidArgumentError` con un mensaje que nombra la clave o la
 * sección, nunca el valor.
 */
export function parseTheme(text: string, options: { readonly format: string }): Theme {
  if (new TextEncoder().encode(text).length > DASHBOARD_THEME_MAX_BYTES) {
    throw new InvalidArgumentError(`tema: pesa más de ${DASHBOARD_THEME_MAX_BYTES} bytes`);
  }
  if (options.format === "json") {
    return themeOf(loadJson(text));
  }
  if (options.format === "css") {
    return themeOf(loadCss(text));
  }
  throw new InvalidArgumentError("tema: el formato debe ser .json o .css");
}

function loadJson(text: string): Document {
  let document: unknown;
  try {
    document = JSON.parse(text);
  } catch {
    throw new InvalidArgumentError("tema: el JSON no es válido");
  }
  if (!isRecord(document)) {
    throw new InvalidArgumentError("tema: la raíz debe ser un objeto");
  }
  return document;
}

/**
 * Un CSS de tema es sólo bloques `:root`, `:root[data-theme="dark"]` y
 * `:root[data-theme="light"]` con declaraciones `--token: valor;`. Cualquier
 * otra cosa (reglas `@`, selectores, `url()`) se rechaza.
 */
function loadCss(text: string): Document {
  const stripped = text.replace(CSS_COMMENT, "");
  const document: Record<string, unknown> = { version: THEME_FORMAT_VERSION };
  let consumed = 0;
  for (const block of stripped.matchAll(CSS_BLOCK)) {
    if (stripped.slice(consumed, block.index).trim() !== "") {
      throw new InvalidArgumentError("tema: el CSS sólo admite bloques :root");
    }
    consumed = block.index + block[0].length;
    const section = CSS_SECTIONS.get((block[1] ?? "").split(/\s+/).filter(Boolean).join(" "));
    if (section === undefined) {
      throw new InvalidArgumentError("tema: selector CSS no admitido");
    }
    document[section] = cssValues(block[2] ?? "");
  }
  if (stripped.slice(consumed).trim() !== "") {
    throw new InvalidArgumentError("tema: el CSS sólo admite bloques :root");
  }
  return document;
}

function cssValues(body: string): Record<string, string> {
  const values: Record<string, string> = {};
  for (const declaration of body.split(";")) {
    if (declaration.trim() === "") {
      continue;
    }
    const found = CSS_DECLARATION.exec(declaration.trim());
    if (found === null) {
      throw new InvalidArgumentError("tema: declaración CSS no admitida");
    }
    values[found[1] as string] = (found[2] as string).trim();
  }
  return values;
}

function sectionOf(document: Document, key: string): unknown {
  return Object.hasOwn(document, key) ? document[key] : {};
}

function themeOf(document: Document): Theme {
  for (const key of Object.keys(document)) {
    if (!JSON_KEYS.has(key)) {
      throw new InvalidArgumentError(`tema: clave desconocida: ${shown(key)}`);
    }
  }
  if (document.version !== THEME_FORMAT_VERSION) {
    throw new InvalidArgumentError(`tema: «version» debe ser ${THEME_FORMAT_VERSION}`);
  }
  const name = Object.hasOwn(document, "name") ? document.name : CUSTOM_THEME_NAME;
  if (typeof name !== "string" || !NAME_PATTERN.test(name)) {
    throw new InvalidArgumentError(
      "tema: «name» admite letras, números, espacio, punto, guion y guion bajo " +
        `(hasta ${THEME_NAME_MAX_CHARS})`,
    );
  }
  return new Theme({
    name,
    shared: validated(SECTION_SHARED, sectionOf(document, SECTION_SHARED), ALL_TOKENS),
    dark: validated("dark", sectionOf(document, "dark"), COLOR_TOKENS),
    light: validated("light", sectionOf(document, "light"), COLOR_TOKENS),
  });
}

/**
 * El aspecto que sirve una cáscara: el tema de usuario y el logo, ambos
 * opcionales. Sin ellos, valen los temas de fábrica.
 */
export class Appearance {
  readonly theme: Theme | undefined;
  readonly logo: Logo | undefined;

  constructor(
    options: { readonly theme?: Theme | undefined; readonly logo?: Logo | undefined } = {},
  ) {
    this.theme = options.theme;
    this.logo = options.logo;
  }

  get branding(): Branding {
    return { name: this.theme?.name ?? BUILTIN_THEME_NAME, logo: this.logo !== undefined };
  }

  get css(): string {
    return this.theme?.css() ?? "";
  }
}
