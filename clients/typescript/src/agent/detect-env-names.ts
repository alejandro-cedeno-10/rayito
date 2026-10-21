/**
 * Las variables de entorno que un proyecto declara en su `.env.example` (o
 * `.env.sample`, `.env.template`), para avisar de las que no reciben valor al
 * desplegar (`--check-env`, ADR-033 D4). Espejo de
 * `rayito._agent._detect_env_names`: reutiliza el recorrido de la detección
 * (`ProjectIndex`, `envExampleFiles`) y sólo lee nombres, nunca un valor.
 * `DetectionReport` no cambia.
 */

import { DETECTION_RULES, type DetectionRules } from "./detect-rules.js";
import { ProjectIndex, type ProjectTree, WarningSink } from "./detect-scan.js";
import { dotenvNames } from "./secret-env.js";

const ENV_MISSING_WARNING = "env_missing";

/** Los nombres de los ficheros de ejemplo de la raíz, en el orden de
 * `envExampleFiles` y de aparición, sin repetir. */
export function requiredEnvNames(
  tree: ProjectTree,
  rules: DetectionRules = DETECTION_RULES,
): readonly string[] {
  const index = new ProjectIndex(tree, rules, new WarningSink(rules));
  const names: string[] = [];
  for (const file of rules.envExampleFiles) {
    const text = index.hasRootFile(file) ? index.read(file) : undefined;
    if (text !== undefined) {
      names.push(...dotenvNames(text));
    }
  }
  return [...new Set(names)];
}

/** Los nombres de `required` que nada cubre: ni `covered` ni un prefijo de
 * `openPrefixes` (un `secretEnvFrom` sin `keys`; el prefijo vacío lo cubre
 * todo). */
export function missingEnvNames(
  required: readonly string[],
  covered: ReadonlySet<string>,
  openPrefixes: readonly string[] = [],
): readonly string[] {
  return required.filter(
    (name) => !covered.has(name) && !openPrefixes.some((prefix) => name.startsWith(prefix)),
  );
}

/** El aviso `env_missing` de cada nombre, con el texto de las reglas. */
export function envMissingWarnings(
  names: readonly string[],
  rules: DetectionRules = DETECTION_RULES,
): readonly string[] {
  const sink = new WarningSink(rules);
  for (const name of names) {
    sink.add(ENV_MISSING_WARNING, { name });
  }
  return sink.items.map((warning) => warning.detail);
}
