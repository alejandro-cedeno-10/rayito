/**
 * Lee los `Parameters` de una plantilla empaquetada y los de un `StackComponent`:
 * lo comparten el test de cada componente y el de la pila `iam` de `rayito setup`.
 */

import type { StackComponent } from "../../src/stacks/model.js";

/** Una clave de primer nivel empieza en la columna 0; la de un parámetro en
 * la 2 (`infra/*.yaml` sigue el sangrado de dos espacios de `cfn-lint`). */
const TOP_LEVEL_KEY = /^(\w+):/gm;
const PARAMETER_KEY = /^ {2}(\w+):\s*$/gm;

export function templateParameterNames(template: string): Set<string> {
  // `split` con un grupo alterna [antes, clave, cuerpo, clave, cuerpo, ...].
  const sections = template.split(TOP_LEVEL_KEY);
  const index = sections.indexOf("Parameters");
  const body = index >= 0 ? (sections[index + 1] ?? "") : "";
  return new Set([...body.matchAll(PARAMETER_KEY)].map((match) => match[1] ?? ""));
}

export function componentParameterNames(component: StackComponent): Set<string> {
  return new Set([
    ...(component.parameters ?? []).map((parameter) => parameter.name),
    ...(component.artifacts ?? []).map((artifact) => artifact.parameterKey),
  ]);
}
