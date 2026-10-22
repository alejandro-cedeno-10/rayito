/**
 * Configuración cero de OpenCode (ADR-034, design.md D10): lo que un proyecto
 * que sólo trae `opencode.json` (u `opencode.jsonc`) y `AGENTS.md` aporta a su
 * tabla `[agent]` cuando el fichero de proyecto no la escribe.
 *
 * - `AGENTS.md` de la raíz pasa a ser `agent.instructions_file`.
 * - El bloque `permission` de la configuración pasa a `agent.permissions`
 *   (`permissionsFromOpenCode`); lo que no se aplica tal cual se avisa con
 *   `opencode_permission_ignored`.
 *
 * Las reglas son datos (`zeroConfig` de `agent-detection.json`). El informe
 * sólo guarda la evidencia (`file:AGENTS.md`, `config:permission`), nunca el
 * texto de los ficheros. Espejo de `rayito._agent._detect_opencode`.
 */

import type { DetectionEvidence } from "./detect-report.js";
import type { DetectionRules, ZeroConfigRule } from "./detect-rules.js";
import type { ProjectIndex, WarningSink } from "./detect-scan.js";
import { permissionsFromOpenCode } from "./opencode.js";

/** La clave de `[agent]` con el fichero de instrucciones y la de los permisos. */
export const INSTRUCTIONS_FILE_KEY = "instructions_file";
export const PERMISSIONS_KEY = "permissions";
/** El aviso de una entrada de `permission` que no se aplica tal cual. */
export const PERMISSION_WARNING = "opencode_permission_ignored";
/** Una clave de `permission` sólo se nombra en el aviso si tiene esta forma;
 * si no, se nombra con `UNNAMED_PERMISSION_KEY`, así que el aviso nunca copia
 * texto arbitrario del fichero. */
const SHOWN_KEY_PATTERN = /^[A-Za-z0-9_*.-]{1,64}$/;
export const UNNAMED_PERMISSION_KEY = "<clave>";
const STRING_QUOTE = '"';
const ESCAPE = "\\";
const LINE_COMMENT = "//";
const BLOCK_COMMENT_OPEN = "/*";
const BLOCK_COMMENT_CLOSE = "*/";
const TRAILING_CLOSER = /^\s*[}\]]/;

/** La tabla `[agent]` que aporta la configuración del framework (vacía si no
 * aporta nada) y la evidencia de cada clave. */
export interface ZeroConfig {
  readonly agent: Readonly<Record<string, unknown>>;
  readonly evidence: readonly DetectionEvidence[];
}

/** `text` sin comentarios (`//` y `/* *\/`) ni comas finales fuera de las
 * cadenas: JSON con comentarios, como lo lee OpenCode. Mismo resultado que
 * `strip_jsonc` en Python. */
export function stripJsonc(text: string): string {
  return withoutTrailingCommas(withoutComments(text));
}

function withoutComments(text: string): string {
  const out: string[] = [];
  let index = 0;
  let inString = false;
  while (index < text.length) {
    const char = text.charAt(index);
    if (inString) {
      if (char === ESCAPE) {
        out.push(text.slice(index, index + 2));
        index += 2;
        continue;
      }
      inString = char !== STRING_QUOTE;
    } else if (char === STRING_QUOTE) {
      inString = true;
    } else if (text.startsWith(LINE_COMMENT, index)) {
      const end = text.indexOf("\n", index);
      index = end < 0 ? text.length : end;
      continue;
    } else if (text.startsWith(BLOCK_COMMENT_OPEN, index)) {
      const end = text.indexOf(BLOCK_COMMENT_CLOSE, index + BLOCK_COMMENT_OPEN.length);
      index = end < 0 ? text.length : end + BLOCK_COMMENT_CLOSE.length;
      out.push(" ");
      continue;
    }
    out.push(char);
    index += 1;
  }
  return out.join("");
}

function withoutTrailingCommas(text: string): string {
  const out: string[] = [];
  let index = 0;
  let inString = false;
  while (index < text.length) {
    const char = text.charAt(index);
    if (inString) {
      if (char === ESCAPE) {
        out.push(text.slice(index, index + 2));
        index += 2;
        continue;
      }
      inString = char !== STRING_QUOTE;
    } else if (char === STRING_QUOTE) {
      inString = true;
    } else if (char === "," && TRAILING_CLOSER.test(text.slice(index + 1))) {
      index += 1;
      continue;
    }
    out.push(char);
    index += 1;
  }
  return out.join("");
}

/** El documento de un `opencode.json(c)`, o `undefined` si no se entiende. */
export function parseJsonc(text: string): unknown {
  try {
    return JSON.parse(stripJsonc(text)) as unknown;
  } catch {
    return undefined;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Lo que la configuración de `framework` aporta a `[agent]`, si sus reglas
 * (`zeroConfig`) lo describen; nada para el resto. */
export function zeroConfig(
  index: ProjectIndex,
  rules: DetectionRules,
  framework: string,
  sink: WarningSink,
): ZeroConfig {
  const spec = rules.zeroConfig[framework];
  if (spec === undefined || typeof spec === "string") {
    return { agent: {}, evidence: [] };
  }
  const agent: Record<string, unknown> = {};
  const evidence: DetectionEvidence[] = [];
  const name = spec.instructionsFile;
  const text = index.hasRootFile(name) ? index.read(name) : undefined;
  if (text !== undefined && text.trim() !== "") {
    agent[INSTRUCTIONS_FILE_KEY] = name;
    evidence.push({ path: name, rule: `file:${name}` });
  }
  const permissions = permissionsOf(index, spec, sink);
  if (permissions !== undefined) {
    agent[PERMISSIONS_KEY] = permissions.table;
    evidence.push({ path: permissions.file, rule: `config:${spec.permissionKey}` });
  }
  return { agent, evidence };
}

/** El fichero de configuración (el primero de `configFiles` que hay en la
 * raíz) y su tabla de permisos, o `undefined` si no aporta ninguna. */
function permissionsOf(
  index: ProjectIndex,
  spec: ZeroConfigRule,
  sink: WarningSink,
): { readonly file: string; readonly table: Readonly<Record<string, unknown>> } | undefined {
  const file = spec.configFiles.find((name) => index.hasRootFile(name));
  const text = file === undefined ? undefined : index.read(file);
  if (file === undefined || text === undefined) {
    return undefined;
  }
  const document = parseJsonc(text);
  if (!isRecord(document)) {
    sink.add("file_unreadable", { file });
    return undefined;
  }
  const block = document[spec.permissionKey];
  if (block === undefined || block === null) {
    return undefined;
  }
  const found = permissionsFromOpenCode(block, { askAction: spec.askAction });
  for (const [key, reason] of found.notes) {
    const shown = SHOWN_KEY_PATTERN.test(key) ? key : UNNAMED_PERMISSION_KEY;
    sink.add(PERMISSION_WARNING, { file, key: shown, reason: spec.reasons[reason] ?? reason });
  }
  return Object.keys(found.table).length > 0 ? { file, table: found.table } : undefined;
}
