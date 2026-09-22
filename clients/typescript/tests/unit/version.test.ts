import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { expect, test } from "vitest";
import { VERSION } from "../../src/version.js";

test("VERSION equals package.json", () => {
  const packageJson = JSON.parse(
    readFileSync(fileURLToPath(new URL("../../package.json", import.meta.url)), "utf8"),
  ) as { version: string };
  expect(VERSION).toBe(packageJson.version);
  expect(VERSION).toBe("0.2.0");
});
