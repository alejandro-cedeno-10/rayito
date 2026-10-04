/**
 * Espejo de `test_m15_stacks_template_parameters.py` (revisión del PR #77):
 * cada `StackComponent` soportado declara exactamente los `Parameters` de su
 * plantilla empaquetada, porque `OptionalStacks` rechaza cualquier nombre que
 * el componente no liste (como pasó con `AllowWrite` de `efs-volumes`).
 */

import { describe, expect, test } from "vitest";
import type { StackComponent } from "../../src/stacks/model.js";
import { loadTemplate } from "../../src/stacks/packaging.js";
import { COMPONENTS } from "../../src/stacks/registry.js";

/** Una clave de primer nivel empieza en la columna 0; la de un parámetro en
 * la 2 (`infra/*.yaml` sigue el sangrado de dos espacios de `cfn-lint`). */
const TOP_LEVEL_KEY = /^(\w+):/gm;
const PARAMETER_KEY = /^ {2}(\w+):\s*$/gm;

function templateParameterNames(template: string): Set<string> {
  // `split` con un grupo alterna [antes, clave, cuerpo, clave, cuerpo, ...].
  const sections = template.split(TOP_LEVEL_KEY);
  const index = sections.indexOf("Parameters");
  const body = index >= 0 ? (sections[index + 1] ?? "") : "";
  return new Set([...body.matchAll(PARAMETER_KEY)].map((match) => match[1] ?? ""));
}

function componentParameterNames(component: StackComponent): Set<string> {
  return new Set([
    ...(component.parameters ?? []).map((parameter) => parameter.name),
    ...(component.artifacts ?? []).map((artifact) => artifact.parameterKey),
  ]);
}

const SUPPORTED = COMPONENTS.filter((component) => component.supported !== false);

describe("stacks: component parameters vs template", () => {
  test.each(SUPPORTED.map((component) => [component.name, component] as const))(
    "%s declares exactly its template's Parameters",
    async (_name, component) => {
      const template = await loadTemplate(component);
      expect(componentParameterNames(component)).toEqual(templateParameterNames(template));
    },
  );
});
