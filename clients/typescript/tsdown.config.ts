import { defineConfig, type UserConfig } from "tsdown";

const PEERS_NOT_BUNDLED = ["@aws-sdk/client-dynamodb", "@aws-sdk/client-secrets-manager"];
// `rayito/cdk` (ADR-032): `aws-cdk-lib` y `constructs` son peers opcionales que
// sólo importa la entrada `cdk`; nunca se empaquetan (ni sus subrutas).
const CDK_PEERS_NOT_BUNDLED = [/^aws-cdk-lib(\/.*)?$/, /^constructs$/];
const EXTERNAL = [...PEERS_NOT_BUNDLED, ...CDK_PEERS_NOT_BUNDLED];

// ESM: la biblioteca y los ejecutables `rayito` y `rayito-mcp` (ADR-027, D1) en
// una sola construcción, para que compartan el chunk con el SDK en vez de
// duplicarlo. Los ejecutables son sólo ESM y `index` nunca los importa
// (`sideEffects: false`).
const esm: UserConfig = {
  entry: {
    index: "src/index.ts",
    e2b: "src/e2b/index.ts",
    cli: "src/cli/main.ts",
    mcp: "src/mcp/main.ts",
    cdk: "src/cdk/index.ts",
  },
  format: ["esm"],
  platform: "node",
  target: "node20",
  dts: true,
  fixedExtension: true,
  sourcemap: true,
  clean: true,
  // Peers opcionales (ADR-014): sólo `loadOptionalPeer` los importa, en
  // tiempo de ejecución y dentro de la función activada; nunca se empaquetan.
  external: EXTERNAL,
};

const cjs: UserConfig = {
  entry: { index: "src/index.ts", e2b: "src/e2b/index.ts", cdk: "src/cdk/index.ts" },
  format: ["cjs"],
  platform: "node",
  target: "node20",
  dts: true,
  fixedExtension: true,
  sourcemap: true,
  clean: false,
  external: EXTERNAL,
};

export default defineConfig([esm, cjs]);
