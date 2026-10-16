/**
 * Temas y logo del panel: los vectores de `testdata/dashboard/theme-vectors.json`
 * (los ejecuta también Python, `test_dashboard_theme.py`), los temas de
 * ejemplo de `examples/dashboard-theme/` y los topes de tamaño. Se aceptan los
 * válidos y se rechazan, sin copiar el valor al mensaje, las claves
 * desconocidas y cualquier intento de inyectar CSS, scripts o recursos
 * remotos. El CSS que se sirve sale sólo de valores validados.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { PNG_SIGNATURE, parseLogo } from "../../src/dashboard/logo.js";
import {
  ALL_TOKENS,
  Appearance,
  COLOR_TOKENS,
  FONT_TOKENS,
  LENGTH_TOKENS,
  parseTheme,
} from "../../src/dashboard/theme.js";
import { InvalidArgumentError } from "../../src/errors.js";
import { DASHBOARD_LOGO_MAX_BYTES, DASHBOARD_THEME_MAX_BYTES } from "../../src/limits.js";
import { loadDashboardVectors } from "./fake/dashboard-world.js";

interface ThemeCase {
  readonly id: string;
  readonly format: string;
  readonly text?: string;
  readonly document?: unknown;
  readonly expected?: {
    readonly name: string;
    readonly shared: Record<string, string>;
    readonly dark: Record<string, string>;
    readonly light: Record<string, string>;
    readonly css: string;
  };
  readonly message?: string;
  readonly messageExcludes?: string[];
}

interface LogoCase {
  readonly id: string;
  readonly base64: string;
  readonly contentType?: string;
  readonly message?: string;
}

const VECTORS = loadDashboardVectors<{
  themes: { valid: ThemeCase[]; invalid: ThemeCase[] };
  logos: { valid: LogoCase[]; invalid: LogoCase[] };
}>("theme-vectors.json");
const EXAMPLES = join(import.meta.dirname, "..", "..", "..", "..", "examples", "dashboard-theme");

function textOf(testCase: ThemeCase): string {
  return testCase.text ?? JSON.stringify(testCase.document);
}

function messageOf(action: () => unknown): string {
  try {
    action();
  } catch (error) {
    expect(error).toBeInstanceOf(InvalidArgumentError);
    return (error as Error).message;
  }
  throw new Error("se esperaba InvalidArgumentError");
}

describe("shared theme vectors", () => {
  for (const testCase of VECTORS.themes.valid) {
    it(`accepts ${testCase.id}`, () => {
      const theme = parseTheme(textOf(testCase), { format: testCase.format });
      const expected = testCase.expected as NonNullable<ThemeCase["expected"]>;
      expect(theme.name).toBe(expected.name);
      expect(theme.shared).toEqual(expected.shared);
      expect(theme.dark).toEqual(expected.dark);
      expect(theme.light).toEqual(expected.light);
      expect(theme.css()).toBe(expected.css);
    });
  }

  for (const testCase of VECTORS.themes.invalid) {
    it(`rejects ${testCase.id}`, () => {
      const message = messageOf(() => parseTheme(textOf(testCase), { format: testCase.format }));
      expect(message).toBe(testCase.message);
      for (const forbidden of testCase.messageExcludes ?? []) {
        expect(message).not.toContain(forbidden);
      }
    });
  }
});

describe("shared logo vectors", () => {
  for (const testCase of VECTORS.logos.valid) {
    it(`accepts ${testCase.id}`, () => {
      const logo = parseLogo(new Uint8Array(Buffer.from(testCase.base64, "base64")));
      expect(logo.contentType).toBe(testCase.contentType);
    });
  }

  for (const testCase of VECTORS.logos.invalid) {
    it(`rejects ${testCase.id}`, () => {
      const message = messageOf(() =>
        parseLogo(new Uint8Array(Buffer.from(testCase.base64, "base64"))),
      );
      expect(message).toBe(testCase.message);
    });
  }
});

describe("theme examples and limits", () => {
  it("the example themes are valid and equivalent", () => {
    const fromJson = parseTheme(readFileSync(join(EXAMPLES, "aurora.json"), "utf8"), {
      format: "json",
    });
    const fromCss = parseTheme(readFileSync(join(EXAMPLES, "aurora.css"), "utf8"), {
      format: "css",
    });
    expect(fromJson.name).toBe("aurora");
    expect({ shared: fromJson.shared, dark: fromJson.dark, light: fromJson.light }).toEqual({
      shared: fromCss.shared,
      dark: fromCss.dark,
      light: fromCss.light,
    });
    expect(new Set(Object.keys(fromJson.dark))).toEqual(new Set(COLOR_TOKENS));
    expect(new Set(Object.keys(fromJson.light))).toEqual(new Set(COLOR_TOKENS));
  });

  it("the example logo is valid", () => {
    const logo = parseLogo(new Uint8Array(readFileSync(join(EXAMPLES, "aurora-logo.svg"))));
    expect(logo.contentType).toBe("image/svg+xml");
  });

  it("rejects a theme over the size cap and an unknown format", () => {
    expect(() => parseTheme(" ".repeat(DASHBOARD_THEME_MAX_BYTES + 1), { format: "json" })).toThrow(
      InvalidArgumentError,
    );
    expect(() => parseTheme("{}", { format: "yaml" })).toThrow(InvalidArgumentError);
  });

  it("rejects a logo over the size cap", () => {
    const oversized = new Uint8Array(DASHBOARD_LOGO_MAX_BYTES + 1);
    oversized.set(PNG_SIGNATURE);
    expect(() => parseLogo(oversized)).toThrow(InvalidArgumentError);
  });

  it("an empty theme serves no CSS and the built-in name", () => {
    expect(new Appearance().css).toBe("");
    expect(new Appearance().branding).toEqual({ name: "rayito", logo: false });
    const empty = new Appearance({ theme: parseTheme('{"version": 1}', { format: "json" }) });
    expect(empty.css.includes("{")).toBe(false);
  });

  it("every documented token is covered", () => {
    expect(new Set(ALL_TOKENS)).toEqual(
      new Set([...COLOR_TOKENS, ...LENGTH_TOKENS, ...FONT_TOKENS]),
    );
  });

  it("an SVG that is not well-formed XML is rejected", () => {
    const encoded = (text: string): Uint8Array => new TextEncoder().encode(text);
    for (const broken of [
      '<svg xmlns="http://www.w3.org/2000/svg"><g></svg>',
      '<svg xmlns="http://www.w3.org/2000/svg" a="1" a="2"/>',
      '<svg xmlns="http://www.w3.org/2000/svg">&unknown;</svg>',
      '<svg xmlns="http://www.w3.org/2000/svg"/><svg/>',
      '<?xml-stylesheet href="https://example.com/a.css"?><svg xmlns="http://www.w3.org/2000/svg"/>',
      'text<svg xmlns="http://www.w3.org/2000/svg"/>',
      '<svg xmlns="http://www.w3.org/2000/svg"><!-- sin cerrar </svg>',
      "<svg",
    ]) {
      expect(() => parseLogo(encoded(broken)), broken).toThrow(InvalidArgumentError);
    }
  });

  it("entities in attributes are decoded before the checks", () => {
    const encoded = new TextEncoder().encode(
      '<svg xmlns="http://www.w3.org/2000/svg"><a href="&#35;a" title="&#106;avascript:alert(1)"/></svg>',
    );
    expect(() => parseLogo(encoded)).toThrow(InvalidArgumentError);
  });

  it("accepts an XML declaration, comments, CDATA and namespaced attributes", () => {
    const svg =
      '<?xml version="1.0" encoding="UTF-8"?>\n<!-- logo -->\n' +
      '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">' +
      '<style><![CDATA[.a { fill: #fff; }]]></style><use xlink:href="#a"/></svg>';
    expect(parseLogo(new TextEncoder().encode(svg)).contentType).toBe("image/svg+xml");
  });
});
