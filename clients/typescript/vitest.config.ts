import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    projects: [
      {
        test: {
          name: "unit",
          include: ["tests/unit/**/*.test.ts"],
          testTimeout: 20_000,
        },
      },
      {
        test: {
          name: "e2e",
          include: ["tests/e2e/**/*.e2e.test.ts"],
          testTimeout: 900_000,
          hookTimeout: 300_000,
        },
      },
      {
        // `make local-e2e` (dev/local/compose.yaml): un único guest aloja un
        // sandbox a la vez, así que los ficheros van de uno en uno.
        test: {
          name: "local",
          include: ["tests/local/**/*.local.test.ts"],
          testTimeout: 600_000,
          hookTimeout: 600_000,
          fileParallelism: false,
        },
      },
    ],
  },
});
