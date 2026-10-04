/**
 * Dominio puro de `m15-events-webhooks`: derivación de `k_sbx` contra los
 * vectores compartidos con Rust y Python.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  eventRecordType,
  eventType,
  sandboxExecutionId,
} from "../../src/lifecycle-events/domain.js";
import { deriveSandboxKey } from "../../src/lifecycle-events/keys.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "../../../..");
const VECTORS = JSON.parse(
  readFileSync(resolve(REPO_ROOT, "testdata/lifecycle-events/mac-vectors.json"), "utf8"),
) as {
  key_derivation: Array<{ stack_key_hex: string; sandbox_id: string; expected_k_sbx_hex: string }>;
};

describe("deriveSandboxKey", () => {
  it("matches the shared vectors", () => {
    for (const vector of VECTORS.key_derivation) {
      const stackKey = Buffer.from(vector.stack_key_hex, "hex");
      const derived = deriveSandboxKey(stackKey, vector.sandbox_id);
      expect(derived.toString("hex")).toBe(vector.expected_k_sbx_hex);
    }
  });

  it("never produces the same key for a different sandbox id", () => {
    const stackKey = Buffer.from("a-stack-secret", "utf8");
    const keyA = deriveSandboxKey(stackKey, "sbx-a");
    const keyB = deriveSandboxKey(stackKey, "sbx-b");
    expect(keyA.equals(keyB)).toBe(false);
  });
});

describe("event helpers", () => {
  it("builds the e2b-compatible type and execution id", () => {
    const record = {
      eventId: "evt-1",
      sandboxId: "sbx-1",
      kind: "killed" as const,
      killReason: "request" as const,
      generation: 3,
      occurredAtMs: 42,
      imageArn: "arn:test",
      imageVersion: "1",
    };
    expect(eventRecordType(record)).toBe("sandbox.lifecycle.killed");
    expect(sandboxExecutionId(record)).toBe("sbx-1#3");
    expect(eventType("created")).toBe("sandbox.lifecycle.created");
  });
});
