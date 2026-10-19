/**
 * Espejo de `test_m15_stacks_template_parameters.py` (revisión del PR #77):
 * cada `StackComponent` soportado declara exactamente los `Parameters` de su
 * plantilla empaquetada, porque `OptionalStacks` rechaza cualquier nombre que
 * el componente no liste (como pasó con `AllowWrite` de `efs-volumes`).
 */

import { describe, expect, test } from "vitest";
import { loadTemplate } from "../../src/stacks/packaging.js";
import { COMPONENTS } from "../../src/stacks/registry.js";
import { componentParameterNames, templateParameterNames } from "./m15-template-parameters.js";

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
