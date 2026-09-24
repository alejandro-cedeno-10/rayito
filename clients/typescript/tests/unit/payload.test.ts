import { createHash } from "node:crypto";
import { describe, expect, test } from "vitest";
import { InvalidArgumentError } from "../../src/errors.js";
import { RUN_HOOK_PAYLOAD_MAX_CHARS } from "../../src/limits.js";
import {
  accessTokenSha256,
  buildRunHookPayload,
  decodeAccessToken,
  encodeAccessToken,
  generateAccessToken,
  validateAccessToken,
  validatedEnvs,
} from "../../src/payload.js";
import { ACCESS_TOKEN, ACCESS_TOKEN_SECRET } from "./helpers.js";

describe("access token", () => {
  test("generated tokens are url-safe, unpadded and unique", () => {
    const first = generateAccessToken();
    const second = generateAccessToken();
    expect(first).not.toBe(second);
    expect(first).toHaveLength(43);
    expect(first).not.toMatch(/[=+/]/);
  });

  test("token_sha256 hashes the decoded bytes like rayd", () => {
    const secret = new Uint8Array([0, 1, 2, ...Array.from({ length: 29 }, (_, i) => i + 3)]);
    const token = encodeAccessToken(secret);
    expect(token).not.toContain("=");
    expect(decodeAccessToken(token)).toEqual(secret);
    expect(decodeAccessToken(`${token}==`)).toEqual(secret);
    expect(accessTokenSha256(token)).toBe(createHash("sha256").update(secret).digest("hex"));
    expect(accessTokenSha256(token)).not.toBe(createHash("sha256").update(token).digest("hex"));
  });

  test("the vector shared with the Python suite hashes to the same value", () => {
    expect(accessTokenSha256(ACCESS_TOKEN)).toBe(
      createHash("sha256").update(ACCESS_TOKEN_SECRET).digest("hex"),
    );
    expect(ACCESS_TOKEN).toBe(Buffer.from(ACCESS_TOKEN_SECRET).toString("base64url"));
  });

  test.each(["", "t", "not base64!", "unit-test-access-token", "dG9rZW5=", "dG9rZW4+", "dG9rZW4/"])(
    "non-canonical base64url tokens are rejected: %j",
    (token) => {
      expect(() => validateAccessToken(token)).toThrow(InvalidArgumentError);
      expect(() => validateAccessToken(token)).toThrow(/base64url/);
    },
  );
});

describe("runHookPayload", () => {
  test("carries the hash, never the token, with sorted compact keys", () => {
    const secret = new TextEncoder().encode("s3cret-token-bytes");
    const token = encodeAccessToken(secret);
    const text = buildRunHookPayload({ accessToken: token, envs: { A: "1" } });
    expect(JSON.parse(text)).toEqual({
      v: 1,
      token_sha256: createHash("sha256").update(secret).digest("hex"),
      user: "user",
      workdir: "/home/user",
      envs: { A: "1" },
    });
    expect(text).not.toContain(token);
    expect(text).toBe(
      `{"envs":{"A":"1"},"token_sha256":"${accessTokenSha256(token)}","user":"user","v":1,"workdir":"/home/user"}`,
    );
  });

  test("envs are omitted when empty and sorted when present", () => {
    const plain = buildRunHookPayload({ accessToken: ACCESS_TOKEN });
    expect(plain).not.toContain("envs");
    expect(buildRunHookPayload({ accessToken: ACCESS_TOKEN, envs: {} })).toBe(plain);
    const sorted = buildRunHookPayload({ accessToken: ACCESS_TOKEN, envs: { Z: "1", A: "2" } });
    expect(sorted.indexOf('"A"')).toBeLessThan(sorted.indexOf('"Z"'));
  });

  test("non-ASCII is escaped like json.dumps in Python", () => {
    const text = buildRunHookPayload({ accessToken: ACCESS_TOKEN, envs: { NAME: "ñ" } });
    expect(text).toContain('"\\u00f1"');
    expect(JSON.parse(text).envs.NAME).toBe("ñ");
  });

  test("user and workdir overrides are honoured", () => {
    const parsed = JSON.parse(
      buildRunHookPayload({ accessToken: ACCESS_TOKEN, user: "root", workdir: "/root" }),
    );
    expect([parsed.user, parsed.workdir]).toEqual(["root", "/root"]);
  });

  test("payloads over 4096 characters are refused before any AWS call", () => {
    const envs = { BIG: "x".repeat(RUN_HOOK_PAYLOAD_MAX_CHARS) };
    expect(() => buildRunHookPayload({ accessToken: ACCESS_TOKEN, envs })).toThrow(
      InvalidArgumentError,
    );
    expect(() => buildRunHookPayload({ accessToken: ACCESS_TOKEN, envs })).toThrow(
      /files\.write\(\)/,
    );
  });

  test("the lifecycle block is sorted snake_case, byte-identical to the Python payload", () => {
    const text = buildRunHookPayload({
      accessToken: ACCESS_TOKEN,
      lifecycle: { timeoutS: 60, capS: 900, onTimeout: "kill", autoResume: false },
    });
    expect(text).toBe(
      '{"lifecycle":{"auto_resume":false,"cap_s":900,"on_timeout":"kill","timeout_s":60},' +
        '"token_sha256":"859e5de6b4aec7dd6f241f258e87bbe1bed8483bac575e01a020a85a6af7ee14",' +
        '"user":"user","v":1,"workdir":"/home/user"}',
    );
    expect(buildRunHookPayload({ accessToken: ACCESS_TOKEN })).not.toContain("lifecycle");
  });

  test("the network block is present only when enforcing, and never carries rules", () => {
    const text = buildRunHookPayload({ accessToken: ACCESS_TOKEN, networkEnforce: true });
    expect(text).toBe(
      '{"network":{"enforce":true},' +
        '"token_sha256":"859e5de6b4aec7dd6f241f258e87bbe1bed8483bac575e01a020a85a6af7ee14",' +
        '"user":"user","v":1,"workdir":"/home/user"}',
    );
    const plain = buildRunHookPayload({ accessToken: ACCESS_TOKEN });
    expect(buildRunHookPayload({ accessToken: ACCESS_TOKEN, networkEnforce: false })).toBe(plain);
    expect(plain).not.toContain("network");
  });

  test("an empty access token is refused", () => {
    expect(() => buildRunHookPayload({ accessToken: "" })).toThrow(InvalidArgumentError);
  });

  test("validatedEnvs refuses empty keys and non-string values", () => {
    expect(validatedEnvs({ A: "1" })).toEqual({ A: "1" });
    expect(() => validatedEnvs({ "": "1" })).toThrow(InvalidArgumentError);
    expect(() => validatedEnvs({ A: 1 as unknown as string })).toThrow(InvalidArgumentError);
  });
});
