/**
 * `secretEnv` y `secretEnvFrom` (ADR-033 D4): los vectores que Python y el
 * host comparten (`testdata/agent-secret-env/`), las banderas, las tablas del
 * fichero de proyecto, los nombres reservados, la especificación de
 * `RAYITO_SECRET_ENV`, la expansión, el lector de `.env` y los nombres de
 * `.env.example`. Todo es puro: ninguna prueba crea un cliente de AWS.
 */

import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { parse as parseToml } from "smol-toml";
import { describe, expect, test } from "vitest";
import {
  envMissingWarnings,
  missingEnvNames,
  requiredEnvNames,
} from "../../src/agent/detect-env-names.js";
import { AgentProject, AgentProjectError } from "../../src/agent/project.js";
import {
  dotenvNames,
  expandReference,
  expandSource,
  isReservedName,
  parseDotenv,
  referenceOf,
  resolveSecretEnv,
  SECRET_ENV_INVALID,
  SECRET_ENV_INVALID_KEY,
  type SecretEnvValue,
  secretEnvNames,
  secretIds,
  specVariable,
} from "../../src/agent/secret-env.js";
import {
  parseSecretEnvFlag,
  parseSecretEnvFlags,
  parseSecretEnvFromFlag,
  parseSecretEnvFromFlags,
  withSecretEnv,
} from "../../src/agent/secret-env-project.js";
import {
  AgentError,
  InvalidArgumentError,
  SecretEnvRef,
  SecretEnvSource,
} from "../../src/index.js";
import {
  AGENT_SECRET_ENV_MAX_SECRETS,
  AGENT_SECRET_ENV_MAX_TOTAL_BYTES,
  AGENT_SECRET_ENV_RESERVED_NAMES,
  AGENT_SECRET_ENV_RESERVED_PREFIXES,
  AGENT_SECRET_ENV_SPEC_VERSION,
} from "../../src/limits.js";
import { project } from "./agent-project-fake.js";

const REPO = join(import.meta.dirname, "..", "..", "..", "..");
const DATA = join(REPO, "testdata", "agent-secret-env");

async function vector<T>(name: string): Promise<T> {
  return JSON.parse(await readFile(join(DATA, name), "utf8")) as T;
}

interface ProjectError {
  readonly code: string;
  readonly path: string;
}
interface FlagCase {
  readonly flag: string;
  readonly value: string;
  readonly expected?: unknown;
  readonly error?: ProjectError;
}
interface TableCase {
  readonly name: string;
  readonly toml: string;
  readonly expected?: Record<string, unknown>;
  readonly error?: ProjectError;
}
interface ExpandCase {
  readonly name: string;
  readonly ref?: Record<string, unknown>;
  readonly source?: Record<string, unknown>;
  readonly secretString: string;
  readonly expected?: unknown;
  readonly error?: string;
}
interface SpecProject {
  readonly secret_env: Record<string, string | { secret: string; key?: string }>;
  readonly secret_env_from: { secret: string; prefix?: string; keys?: string[] }[];
  readonly readable_secrets: string[];
}
interface SpecCase {
  readonly name: string;
  readonly project: SpecProject;
  readonly expected: { readonly variable: string | null; readonly readableSecretArns: string[] };
}

const parse = await vector<{ flagCases: FlagCase[]; tableCases: TableCase[] }>("parse.json");
const expand = await vector<{ refCases: ExpandCase[]; sourceCases: ExpandCase[] }>("expand.json");
const reserved = await vector<{
  names: string[];
  prefixes: string[];
  cases: { name: string; reserved: boolean }[];
}>("reserved.json");
const spec = await vector<{ region: string; account: string; cases: SpecCase[] }>("spec.json");
const dotenv = await vector<{
  cases: { name: string; text: string; expected: Record<string, string>; names: string[] }[];
}>("dotenv.json");
const envNames = await vector<{
  cases: {
    name: string;
    files: Record<string, string>;
    covered: string[];
    openPrefixes: string[];
    required: string[];
    missing: string[];
  }[];
}>("env-names.json");
const MINIMAL = await readFile(
  join(REPO, "testdata", "agent-project", "valid", "minimal.toml"),
  "utf8",
);

function errorOf(action: () => unknown): AgentProjectError {
  try {
    action();
  } catch (error) {
    if (error instanceof AgentProjectError) {
      return error;
    }
    throw error;
  }
  throw new Error("se esperaba un AgentProjectError");
}

function referenceDocument(value: SecretEnvValue): Record<string, string> {
  const reference = referenceOf(value);
  return reference.key === undefined
    ? { secret: reference.secret }
    : { secret: reference.secret, key: reference.key };
}

function projectValues(document: SpecProject): {
  secretEnv: Record<string, SecretEnvValue>;
  sources: SecretEnvSource[];
} {
  const secretEnv = Object.fromEntries(
    Object.entries(document.secret_env).map(([name, value]) => [
      name,
      typeof value === "string" ? value : new SecretEnvRef(value),
    ]),
  );
  const sources = document.secret_env_from.map(
    (item) => new SecretEnvSource({ secret: item.secret, prefix: item.prefix, keys: item.keys }),
  );
  return { secretEnv, sources };
}

describe("parse.json", () => {
  for (const testCase of parse.flagCases) {
    test(`${testCase.flag} ${testCase.value}`, () => {
      const run = (): unknown => {
        if (testCase.flag === "--secret-env") {
          const [name, value] = parseSecretEnvFlag(testCase.value);
          return { name, ref: referenceDocument(value) };
        }
        const source = parseSecretEnvFromFlag(testCase.value, 0);
        return { secret: source.secret, prefix: source.prefix, keys: source.keys ?? null };
      };
      if (testCase.error !== undefined) {
        const error = errorOf(run);
        expect({ code: error.code, path: error.path }).toEqual(testCase.error);
        return;
      }
      expect(run()).toEqual(testCase.expected);
    });
  }

  for (const testCase of parse.tableCases) {
    test(testCase.name, () => {
      const document = parseToml(`${MINIMAL}\n${testCase.toml}`) as Record<string, unknown>;
      const load = () => AgentProject.fromObject(document, { baseDir: "." });
      if (testCase.error !== undefined) {
        const error = errorOf(load);
        expect({ code: error.code, path: error.path }).toEqual(testCase.error);
        return;
      }
      const canonical = load().toObject();
      for (const [key, value] of Object.entries(testCase.expected ?? {})) {
        expect(canonical[key], key).toEqual(value);
      }
    });
  }

  for (const pasted of ["sk-not-a-name-either", "tok_NotARealValue123", "MY KEY=app/x"]) {
    test(`a flag without a valid name never repeats it: ${pasted}`, () => {
      const error = errorOf(() => parseSecretEnvFlag(pasted));
      expect(error.path).toBe("secret_env");
      expect(error.message).not.toContain(pasted.split("=")[0] as string);
    });
  }

  test("a flag error never repeats the pasted value", () => {
    const error = errorOf(() => parseSecretEnvFlag("OPENAI_API_KEY=sk-not-a-name"));
    expect(error.message).not.toContain("sk-not-a-name");
  });
});

describe("the project", () => {
  test("a project without secret env has no new keys", () => {
    const canonical = project().toObject();
    expect(canonical).not.toHaveProperty("secret_env");
    expect(canonical).not.toHaveProperty("secret_env_from");
  });

  test("flags add to the project's secret env", () => {
    const base = project({ secretEnv: { DATABASE_URL: "app/db-url" } });
    const changed = withSecretEnv(
      base,
      parseSecretEnvFlags(["LLM_GATEWAY_KEY=llm#api_key"]),
      parseSecretEnvFromFlags(["app/config,prefix=APP_,keys=DB_HOST"]),
    );
    expect(changed.secretEnv).toEqual({
      DATABASE_URL: "app/db-url",
      LLM_GATEWAY_KEY: new SecretEnvRef({ secret: "llm", key: "api_key" }),
    });
    expect(changed.secretEnvFrom).toEqual([
      new SecretEnvSource({ secret: "app/config", prefix: "APP_", keys: ["DB_HOST"] }),
    ]);
    expect(withSecretEnv(base, {}, [])).toBe(base);
  });

  const invalidCases: [string, Record<string, unknown>, string, string][] = [
    ["reserved name", { secretEnv: { HOME: "app/home" } }, "invalid_value", "secret_env.HOME"],
    ["bad name", { secretEnv: { "db-url": "app/db-url" } }, "invalid_value", "secret_env.db-url"],
    [
      "pasted value",
      { secretEnv: { KEY_NAME: "AKIAEXAMPLE" } },
      "secret_value",
      "secret_env.KEY_NAME",
    ],
    ["not a string", { secretEnv: { DB: 3 } }, "invalid_type", "secret_env.DB"],
    ["conflict", { env: { DB: "x" }, secretEnv: { DB: "app/db" } }, "conflict", "secret_env.DB"],
    [
      "reserved prefix",
      { secretEnvFrom: [new SecretEnvSource({ secret: "app/config", prefix: "LD_" })] },
      "invalid_value",
      "secret_env_from[0].prefix",
    ],
    [
      "reserved key",
      { secretEnvFrom: [new SecretEnvSource({ secret: "app/config", keys: ["PATH"] })] },
      "invalid_value",
      "secret_env_from[0].keys",
    ],
    ["not a source", { secretEnvFrom: ["app/config"] }, "invalid_type", "secret_env_from[0]"],
    [
      "key outside printable ascii",
      { secretEnv: { DB: new SecretEnvRef({ secret: "app/db", key: "clé con espacio" }) } },
      "invalid_value",
      "secret_env.DB",
    ],
  ];
  for (const [label, fields, code, path] of invalidCases) {
    test(`a project built in code validates its secret env: ${label}`, () => {
      const error = errorOf(() => project(fields as never));
      expect([error.code, error.path]).toEqual([code, path]);
    });
  }

  test("more secrets than the limit are refused", () => {
    const many = Object.fromEntries(
      Array.from({ length: AGENT_SECRET_ENV_MAX_SECRETS }, (_, index) => [
        `VAR_${index}`,
        `app/secret-${index}`,
      ]),
    );
    project({ secretEnv: many });
    const error = errorOf(() => project({ secretEnv: { ...many, ONE_MORE: "app/one-more" } }));
    expect([error.code, error.path]).toEqual(["invalid_value", "secret_env"]);
  });

  test("the value objects check their types", () => {
    expect(() => new SecretEnvRef({ secret: "" })).toThrow(InvalidArgumentError);
    expect(() => new SecretEnvRef({ secret: "app/db", key: "" })).toThrow(InvalidArgumentError);
    expect(() => new SecretEnvSource({ secret: "app/config", keys: "DB_HOST" as never })).toThrow(
      InvalidArgumentError,
    );
  });
});

describe("reserved.json", () => {
  test("the reserved names are the limits", () => {
    expect([...AGENT_SECRET_ENV_RESERVED_NAMES]).toEqual(reserved.names);
    expect([...AGENT_SECRET_ENV_RESERVED_PREFIXES]).toEqual(reserved.prefixes);
  });
  for (const testCase of reserved.cases) {
    test(testCase.name, () => {
      expect(isReservedName(testCase.name)).toBe(testCase.reserved);
    });
  }
});

function failureCode(action: () => unknown): string | undefined {
  try {
    action();
  } catch (error) {
    if (error instanceof AgentError) {
      expect(error.reason).toBe("runtime_error");
      return error.detailCode;
    }
    throw error;
  }
  return undefined;
}

describe("expand.json", () => {
  for (const testCase of expand.refCases) {
    test(`reference: ${testCase.name}`, () => {
      const run = () => expandReference(testCase.ref ?? {}, testCase.secretString);
      if (testCase.error !== undefined) {
        expect(failureCode(run)).toBe(testCase.error);
        return;
      }
      expect(run()).toBe(testCase.expected);
    });
  }
  for (const testCase of expand.sourceCases) {
    test(`source: ${testCase.name}`, async () => {
      expect(failureCode(() => expandSource(testCase.source ?? {}, testCase.secretString))).toBe(
        testCase.error,
      );
      const specDocument = {
        env: {},
        from: [testCase.source ?? {}],
        v: AGENT_SECRET_ENV_SPEC_VERSION,
      };
      const reads: (readonly [string, string | undefined])[] = [];
      const result = resolveSecretEnv(specDocument, async (secret, stage) => {
        reads.push([secret, stage]);
        return testCase.secretString;
      });
      if (testCase.error !== undefined) {
        const error = await result.then(
          () => undefined,
          (thrown: unknown) => thrown as AgentError,
        );
        expect(error?.detailCode).toBe(testCase.error);
        expect(error?.message).not.toContain(String(testCase.source?.secret));
        return;
      }
      expect(await result).toEqual(testCase.expected);
      expect(reads).toEqual([[testCase.source?.secret, undefined]]);
    });
  }

  test("each secret is read once and a repeated name is refused", async () => {
    const reads: string[] = [];
    const document = {
      env: { A_URL: { secret: "app/one" }, B_URL: { secret: "app/one" } },
      from: [{ secret: "app/two", keys: ["A_URL"] }],
      v: AGENT_SECRET_ENV_SPEC_VERSION,
    };
    const error = await resolveSecretEnv(document, async (secret) => {
      reads.push(secret);
      return secret === "app/two" ? '{"A_URL": "x"}' : "value";
    }).then(
      () => undefined,
      (thrown: unknown) => thrown as AgentError,
    );
    expect(error?.detailCode).toBe(SECRET_ENV_INVALID_KEY);
    expect(reads).toEqual(["app/one", "app/two"]);
  });

  test("the total size of the values is capped", async () => {
    const document = {
      env: { BIG: { secret: "app/big" } },
      from: [],
      v: AGENT_SECRET_ENV_SPEC_VERSION,
    };
    const error = await resolveSecretEnv(document, async () =>
      "x".repeat(AGENT_SECRET_ENV_MAX_TOTAL_BYTES + 1),
    ).then(
      () => undefined,
      (thrown: unknown) => thrown as AgentError,
    );
    expect(error?.detailCode).toBe(SECRET_ENV_INVALID);
  });
});

describe("spec.json", () => {
  const prefix = `arn:aws:secretsmanager:${spec.region}:${spec.account}:secret:`;
  const nameOf = (arn: string): string =>
    arn.endsWith("-??????") ? arn.slice(prefix.length, -"-??????".length) : arn;
  for (const testCase of spec.cases) {
    test(testCase.name, () => {
      const { secretEnv, sources } = projectValues(testCase.project);
      expect(specVariable(secretEnv, sources) ?? null).toBe(testCase.expected.variable);
      const readable = [
        ...new Set([...testCase.project.readable_secrets, ...secretIds(secretEnv, sources)]),
      ];
      expect(readable).toEqual(testCase.expected.readableSecretArns.map(nameOf));
    });
  }

  test("the known variable names", () => {
    const first = spec.cases[0];
    if (first === undefined) {
      throw new Error("spec.json sin casos");
    }
    const { secretEnv, sources } = projectValues(first.project);
    expect(secretEnvNames(secretEnv, sources)).toEqual([
      "APP_DB_HOST",
      "APP_DB_USER",
      "DATABASE_URL",
      "OPENAI_API_KEY",
    ]);
  });
});

describe("dotenv.json", () => {
  for (const testCase of dotenv.cases) {
    test(testCase.name, () => {
      expect(parseDotenv(testCase.text)).toEqual(testCase.expected);
      expect(dotenvNames(testCase.text)).toEqual(testCase.names);
    });
  }
});

describe("env-names.json", () => {
  for (const testCase of envNames.cases) {
    test(testCase.name, () => {
      const tree = {
        listFiles: (_depth: number, maxFiles: number) =>
          Object.keys(testCase.files).sort().slice(0, maxFiles),
        readText: (path: string) => testCase.files[path],
      };
      const required = requiredEnvNames(tree);
      expect(required).toEqual(testCase.required);
      const missing = missingEnvNames(required, new Set(testCase.covered), testCase.openPrefixes);
      expect(missing).toEqual(testCase.missing);
      const warnings = envMissingWarnings(missing);
      expect(warnings).toHaveLength(missing.length);
      missing.forEach((name, index) => {
        expect(warnings[index]).toContain(name);
      });
    });
  }
});
