/**
 * El logo del panel: un PNG por su firma o un SVG por su estructura, sin
 * código ni referencias externas. Dominio puro. Rechaza (no sanea) lo que un
 * SVG podría hacer fuera de dibujar: DTD y entidades, scripts,
 * `foreignObject`, animaciones, manejadores `on*`, `javascript:`/`data:` y
 * cualquier `href` o `url()` que no sea un fragmento local `#id`. Se sirve
 * además con una CSP que no ejecuta nada. Espejo de la parte de logo de
 * `rayito._dashboard._theme`.
 *
 * No hay analizador de XML en Node, y una dependencia para validar un logo no
 * compensa: `parseSvg` es un lector estricto del subconjunto de XML que usa
 * un SVG (elementos, atributos entre comillas, texto, CDATA, comentarios y
 * las cinco entidades predefinidas). Cualquier cosa que no reconozca, incluidas
 * las instrucciones de proceso, se rechaza.
 */

import { InvalidArgumentError } from "../errors.js";
import { DASHBOARD_LOGO_MAX_BYTES } from "../limits.js";

export const PNG_SIGNATURE = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
export const CONTENT_TYPE_PNG = "image/png" as const;
export const CONTENT_TYPE_SVG = "image/svg+xml" as const;
/**
 * Elementos de un SVG que ejecutan código, incrustan otro documento o animan
 * atributos (un `<animate>` puede poner un `javascript:` en un `href`).
 */
export const SVG_FORBIDDEN_TAGS: ReadonlySet<string> = new Set([
  "script",
  "foreignobject",
  "iframe",
  "object",
  "embed",
  "audio",
  "video",
  "animate",
  "animatetransform",
  "animatemotion",
  "set",
]);
const SVG_ROOT_TAG = "svg";
const INVALID_LOGO = "logo: no es un PNG ni un SVG válido";
const SVG_DOCTYPE = /<!\s*(?:DOCTYPE|ENTITY)/i;
const SVG_EXTERNAL_URL = /url\(\s*['"]?\s*(?!#)/i;
const SVG_IMPORT = /@import/i;
const SVG_SCRIPT_URL = /(?:java|vb)script\s*:|data\s*:/i;

/** Un logo ya validado: PNG o SVG sin código ni referencias externas. */
export interface Logo {
  readonly contentType: string;
  readonly data: Uint8Array;
}

/**
 * Valida el logo por su contenido (no por la extensión): un PNG por su firma,
 * un SVG por su estructura. Lanza `InvalidArgumentError`.
 */
export function parseLogo(data: Uint8Array): Logo {
  if (data.length > DASHBOARD_LOGO_MAX_BYTES) {
    throw new InvalidArgumentError(`logo: pesa más de ${DASHBOARD_LOGO_MAX_BYTES} bytes`);
  }
  if (PNG_SIGNATURE.every((byte, index) => data[index] === byte)) {
    return { contentType: CONTENT_TYPE_PNG, data };
  }
  checkSvg(data);
  return { contentType: CONTENT_TYPE_SVG, data };
}

function checkSvg(data: Uint8Array): void {
  if (SVG_DOCTYPE.test(Buffer.from(data).toString("latin1"))) {
    throw new InvalidArgumentError("logo: el SVG no puede llevar DOCTYPE ni entidades");
  }
  const elements = parseSvg(data);
  if (elements[0] === undefined || localName(elements[0].tag) !== SVG_ROOT_TAG) {
    throw new InvalidArgumentError(INVALID_LOGO);
  }
  for (const element of elements) {
    checkSvgElement(element);
  }
}

function localName(name: string): string {
  return name.slice(name.lastIndexOf(":") + 1).toLowerCase();
}

function checkSvgElement(element: SvgElement): void {
  const tag = localName(element.tag);
  if (SVG_FORBIDDEN_TAGS.has(tag)) {
    throw new InvalidArgumentError(`logo: el SVG no admite el elemento ${tag}`);
  }
  if (tag === "style" && unsafeStyle(element.text)) {
    throw new InvalidArgumentError("logo: el SVG no admite estilos con recursos externos");
  }
  for (const [name, value] of element.attributes) {
    const attribute = localName(name);
    if (attribute.startsWith("on")) {
      throw new InvalidArgumentError("logo: el SVG no admite manejadores de eventos");
    }
    if (attribute === "href" && !value.startsWith("#")) {
      throw new InvalidArgumentError("logo: el SVG no admite referencias externas");
    }
    if (SVG_SCRIPT_URL.test(value) || unsafeStyle(value)) {
      throw new InvalidArgumentError("logo: el SVG no admite referencias externas");
    }
  }
}

function unsafeStyle(text: string): boolean {
  return SVG_EXTERNAL_URL.test(text) || SVG_IMPORT.test(text);
}

/**
 * Un elemento leído: su nombre con prefijo, sus atributos ya sin entidades y
 * todo el texto que contiene (el suyo y el de sus descendientes).
 */
export interface SvgElement {
  readonly tag: string;
  readonly attributes: ReadonlyArray<readonly [string, string]>;
  text: string;
}

const NAME_START = "[A-Za-z_:]";
const NAME_REST = "[A-Za-z0-9_.:-]*";
const NAME = `${NAME_START}${NAME_REST}`;
const ATTRIBUTE = new RegExp(`\\s+(${NAME})\\s*=\\s*(?:"([^<"]*)"|'([^<']*)')`, "y");
const START_TAG = new RegExp(`<(${NAME})`, "y");
const END_TAG = new RegExp(`</(${NAME})\\s*>`, "y");
const TAG_CLOSE = /\s*(\/?)>/y;
const XML_DECLARATION = /<\?xml\s[^<>]*\?>/y;
const ENTITY = /&(?:(amp|lt|gt|quot|apos)|#(\d{1,7})|#x([0-9a-fA-F]{1,6}));/y;
const HEX_RADIX = 16;
const DECIMAL_RADIX = 10;
const MAX_CODE_POINT = 0x10ffff;
const PREDEFINED_ENTITIES: Readonly<Record<string, string>> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
};

function fail(): never {
  throw new InvalidArgumentError(INVALID_LOGO);
}

function matchAt(pattern: RegExp, text: string, index: number): RegExpExecArray | null {
  pattern.lastIndex = index;
  return pattern.exec(text);
}

function decodeEntities(raw: string): string {
  if (!raw.includes("&")) {
    return raw;
  }
  let decoded = "";
  let index = 0;
  while (index < raw.length) {
    const char = raw[index] as string;
    if (char !== "&") {
      decoded += char;
      index += 1;
      continue;
    }
    const found = matchAt(ENTITY, raw, index);
    if (found === null) {
      fail();
    }
    decoded += entityText(found);
    index += found[0].length;
  }
  return decoded;
}

function entityText(found: RegExpExecArray): string {
  if (found[1] !== undefined) {
    return PREDEFINED_ENTITIES[found[1]] as string;
  }
  const codePoint =
    found[2] === undefined
      ? Number.parseInt(found[3] as string, HEX_RADIX)
      : Number.parseInt(found[2], DECIMAL_RADIX);
  if (codePoint === 0 || codePoint > MAX_CODE_POINT) {
    fail();
  }
  return String.fromCodePoint(codePoint);
}

function readAttributes(
  text: string,
  start: number,
): { readonly attributes: Array<readonly [string, string]>; readonly end: number } {
  const attributes: Array<readonly [string, string]> = [];
  const seen = new Set<string>();
  let index = start;
  for (;;) {
    const found = matchAt(ATTRIBUTE, text, index);
    if (found === null) {
      return { attributes, end: index };
    }
    const name = found[1] as string;
    if (seen.has(name)) {
      fail();
    }
    seen.add(name);
    attributes.push([name, decodeEntities(found[2] ?? found[3] ?? "")]);
    index += found[0].length;
  }
}

/**
 * Lee un documento SVG a la lista plana de sus elementos, en orden de
 * documento. Estricto: etiquetas bien anidadas, un solo elemento raíz, sin
 * texto fuera de él y sin instrucciones de proceso salvo la declaración XML.
 */
export function parseSvg(data: Uint8Array): SvgElement[] {
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(data);
  } catch {
    return fail();
  }
  return new SvgReader(text.startsWith("﻿") ? text.slice(1) : text).read();
}

class SvgReader {
  readonly #text: string;
  readonly #elements: SvgElement[] = [];
  readonly #open: SvgElement[] = [];
  #index = 0;
  #rootClosed = false;

  constructor(text: string) {
    this.#text = text;
  }

  read(): SvgElement[] {
    const declaration = matchAt(XML_DECLARATION, this.#text, 0);
    this.#index = declaration === null ? 0 : declaration[0].length;
    while (this.#index < this.#text.length) {
      this.#step();
    }
    if (this.#open.length > 0 || this.#elements.length === 0) {
      fail();
    }
    return this.#elements;
  }

  #step(): void {
    if (this.#text[this.#index] !== "<") {
      this.#readText();
    } else if (this.#text.startsWith("<!--", this.#index)) {
      this.#skip("-->", "<!--".length);
    } else if (this.#text.startsWith("<![CDATA[", this.#index)) {
      this.#readCdata();
    } else if (this.#text.startsWith("</", this.#index)) {
      this.#readEndTag();
    } else {
      this.#readStartTag();
    }
  }

  #skip(terminator: string, prefixLength: number): void {
    const end = this.#text.indexOf(terminator, this.#index + prefixLength);
    if (end < 0) {
      fail();
    }
    this.#index = end + terminator.length;
  }

  #readText(): void {
    const next = this.#text.indexOf("<", this.#index);
    const end = next < 0 ? this.#text.length : next;
    const raw = this.#text.slice(this.#index, end);
    this.#index = end;
    if (this.#open.length === 0) {
      if (raw.trim() !== "") {
        fail();
      }
      return;
    }
    this.#appendText(decodeEntities(raw));
  }

  #readCdata(): void {
    const start = this.#index + "<![CDATA[".length;
    const end = this.#text.indexOf("]]>", start);
    if (end < 0 || this.#open.length === 0) {
      fail();
    }
    this.#appendText(this.#text.slice(start, end));
    this.#index = end + "]]>".length;
  }

  #appendText(text: string): void {
    for (const element of this.#open) {
      element.text += text;
    }
  }

  #readEndTag(): void {
    const found = matchAt(END_TAG, this.#text, this.#index);
    const current = this.#open.pop();
    if (found === null || current === undefined || current.tag !== found[1]) {
      fail();
    }
    this.#rootClosed = this.#open.length === 0;
    this.#index += found[0].length;
  }

  #readStartTag(): void {
    const start = matchAt(START_TAG, this.#text, this.#index);
    if (start === null || this.#rootClosed) {
      fail();
    }
    const { attributes, end } = readAttributes(this.#text, this.#index + start[0].length);
    const close = matchAt(TAG_CLOSE, this.#text, end);
    if (close === null) {
      fail();
    }
    const element: SvgElement = { tag: start[1] as string, attributes, text: "" };
    this.#elements.push(element);
    this.#index = end + close[0].length;
    if (close[1] === "/") {
      this.#rootClosed = this.#open.length === 0;
    } else {
      this.#open.push(element);
    }
  }
}
