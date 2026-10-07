/**
 * `BuildError`/`TemplateError` de `rayito/e2b` son las clases nativas que
 * lanza `Template.build()`: un `instanceof` con cualquiera de los dos
 * imports atrapa el mismo error.
 */

import { describe, expect, test } from "vitest";
import { BuildError, SandboxError, TemplateError } from "../../src/e2b/index.js";
import * as native from "../../src/index.js";

describe("rayito/e2b build errors", () => {
  test("are the native classes", () => {
    expect(BuildError).toBe(native.BuildError);
    expect(TemplateError).toBe(native.TemplateError);
  });

  test("a native build failure is an instance of the shim classes", () => {
    const build = new native.BuildError("fallo", { reason: "build_quota" });
    expect(build).toBeInstanceOf(BuildError);
    expect(build).toBeInstanceOf(SandboxError);
    expect(new native.TemplateError("nombre inválido")).toBeInstanceOf(TemplateError);
  });
});
