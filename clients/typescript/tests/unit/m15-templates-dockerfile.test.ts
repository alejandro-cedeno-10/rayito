/**
 * `src/templates/dockerfile.ts` (m15-templates): espejo de
 * `test_m15_templates_dockerfile.py`.
 */

import { describe, expect, test } from "vitest";
import { BuildError } from "../../src/errors.js";
import {
  composeDockerfile,
  LAYER_BEGIN_MARKER,
  renderAppendedLayer,
} from "../../src/templates/dockerfile.js";
import { Template } from "../../src/templates/dsl.js";

const BASE_DOCKERFILE =
  'FROM scratch\nCOPY rayd /usr/local/bin/rayd\nCMD ["/usr/local/bin/rayd"]\n';

describe("templates/dockerfile", () => {
  test("renderAppendedLayer has no FROM and keeps step order", () => {
    const spec = new Template().pipInstall("pandas").copy("app/", "/srv/app/").spec;
    const rendered = renderAppendedLayer(spec);
    expect(rendered).not.toContain("FROM");
    const lines = rendered.split("\n").filter((line) => line && !line.startsWith("#"));
    expect(lines).toEqual([
      "RUN python3 -m pip install --no-cache-dir --break-system-packages pandas",
      'COPY ["__rayito_context/app/", "/srv/app/"]',
    ]);
  });

  test("compose inserts the layer before the final CMD and repeats it", () => {
    const spec = new Template().pipInstall("pandas").spec;
    const composed = composeDockerfile(BASE_DOCKERFILE, spec);
    const lines = composed.trimEnd().split("\n");
    expect(lines[0]).toBe("FROM scratch");
    expect(lines).toContain(
      "RUN python3 -m pip install --no-cache-dir --break-system-packages pandas",
    );
    expect(lines.at(-2)).toBe("USER root");
    expect(lines.at(-1)).toBe('CMD ["/usr/local/bin/rayd"]');
    expect(lines.indexOf("COPY rayd /usr/local/bin/rayd")).toBeLessThan(
      lines.indexOf("RUN python3 -m pip install --no-cache-dir --break-system-packages pandas"),
    );
  });

  test("compose adds the template.json COPY only when start is set", () => {
    const withoutStart = composeDockerfile(
      BASE_DOCKERFILE,
      new Template().pipInstall("pandas").spec,
    );
    expect(withoutStart).not.toContain("__rayito_template.json");

    const withStart = composeDockerfile(
      BASE_DOCKERFILE,
      new Template().setStartCmd("python app.py").spec,
    );
    expect(withStart).toContain('COPY ["__rayito_template.json", "/etc/rayito/template.json"]');
  });

  test("compose without a terminal instruction raises BuildError", () => {
    expect(() => composeDockerfile("FROM scratch\nRUN echo hi\n", new Template().spec)).toThrow(
      BuildError,
    );
  });

  test("rebuilding an already composed image does not stack layers", () => {
    const first = composeDockerfile(BASE_DOCKERFILE, new Template().pipInstall("pandas").spec);
    const second = composeDockerfile(first, new Template().copy("app/", "/srv/app/").spec);
    expect(second.split(LAYER_BEGIN_MARKER).length - 1).toBe(1);
    expect(second).not.toContain("pip install");
    expect(second).toContain('COPY ["__rayito_context/app/", "/srv/app/"]');
  });
});
