import { defineConfig } from "tsdown";

export default defineConfig({
  entry: { index: "src/index.ts", e2b: "src/e2b/index.ts" },
  format: ["esm", "cjs"],
  platform: "node",
  target: "node20",
  dts: true,
  fixedExtension: true,
  sourcemap: true,
  clean: true,
  // Peers opcionales (ADR-014): sólo `loadOptionalPeer` los importa, en
  // tiempo de ejecución y dentro de la función activada; nunca se empaquetan.
  external: ["@aws-sdk/client-secrets-manager"],
});
