/**
 * La vista multicuenta del panel local (opt-in): las especificaciones
 * `--account`, el reparto de `AccountsApi` por `?account=`, que las cuentas
 * adicionales son de sólo lectura, y las credenciales que asumen el rol.
 * Espejo de `test_dashboard_accounts.py`.
 */

import { inspect } from "node:util";
import type { AssumeRoleCommand, AssumeRoleCommandOutput } from "@aws-sdk/client-sts";
import { describe, expect, it } from "vitest";
import { AccountsApi, parseAccountSpecs } from "../../src/dashboard/accounts.js";
import type { ApiBackend } from "../../src/dashboard/api/contract.js";
import {
  AssumedRole,
  assumedCredentials,
  CREDENTIALS_REFRESH_MARGIN_MS,
  type StsSender,
} from "../../src/dashboard/assume.js";
import { AuthenticationError, InvalidArgumentError } from "../../src/errors.js";
import {
  DASHBOARD_ACCOUNT_LABEL_MAX_CHARS,
  DASHBOARD_ACCOUNTS_MAX,
  DASHBOARD_ASSUME_ROLE_SESSION_NAME,
  DASHBOARD_ASSUME_ROLE_SESSION_SECONDS,
} from "../../src/limits.js";
import { awsSdkError } from "./fake/dashboard.js";
import { apiRequest, Rig } from "./fake/dashboard-world.js";

const ROLE_ARN = "arn:aws:iam::123456789012:role/rayito-dashboard-read";
const OTHER_ROLE_ARN = "arn:aws:iam::123456789012:role/rayito-dashboard-read-other";
const WRITES = ["terminateMicrovm", "suspendMicrovm", "resumeMicrovm"];

function twoAccounts(): { api: AccountsApi; primary: Rig; staging: Rig } {
  const primary = new Rig({ allowActions: true, sandboxes: [{ n: 1 }] });
  const staging = new Rig({ sandboxes: [{ n: 2 }, { n: 3 }] });
  const others = new Map<string, ApiBackend>([["staging", staging.api]]);
  return { api: new AccountsApi(primary.api, others), primary, staging };
}

describe("account specs", () => {
  it("pair a label with a role ARN", () => {
    const specs = parseAccountSpecs([`staging=${ROLE_ARN}`, `Prod 2=${OTHER_ROLE_ARN}`]);
    expect(specs.map((spec) => [spec.label, spec.roleArn])).toEqual([
      ["staging", ROLE_ARN],
      ["Prod 2", OTHER_ROLE_ARN],
    ]);
  });

  it.each([
    "staging",
    `=${ROLE_ARN}`,
    `-bad=${ROLE_ARN}`,
    `${"a".repeat(DASHBOARD_ACCOUNT_LABEL_MAX_CHARS + 1)}=${ROLE_ARN}`,
    "staging=arn:aws:iam::123456789012:root",
    "staging=arn:aws:iam::123456789012:user/someone",
    "staging=123456789012",
  ])("refuses the malformed spec %s without echoing the value", (value) => {
    let message = "";
    try {
      parseAccountSpecs([value]);
    } catch (error) {
      expect(error).toBeInstanceOf(InvalidArgumentError);
      message = (error as Error).message;
    }
    expect(message).not.toBe("");
    expect(message).not.toContain(value);
  });

  it("labels are unique ignoring case and capped", () => {
    expect(() => parseAccountSpecs([`staging=${ROLE_ARN}`, `STAGING=${OTHER_ROLE_ARN}`])).toThrow(
      InvalidArgumentError,
    );
    const many = Array.from({ length: DASHBOARD_ACCOUNTS_MAX + 1 }, (_, n) => `a${n}=${ROLE_ARN}`);
    expect(() => parseAccountSpecs(many)).toThrow(InvalidArgumentError);
    expect(parseAccountSpecs(many.slice(0, DASHBOARD_ACCOUNTS_MAX))).toHaveLength(
      DASHBOARD_ACCOUNTS_MAX,
    );
  });

  it("the label limit is the named one", () => {
    const longest = "a".repeat(DASHBOARD_ACCOUNT_LABEL_MAX_CHARS);
    expect(parseAccountSpecs([`${longest}=${ROLE_ARN}`])[0]?.label).toBe(longest);
    expect(() => parseAccountSpecs([`${longest}a=${ROLE_ARN}`])).toThrow(InvalidArgumentError);
  });

  it("the spec hides the role ARN from inspection and JSON", () => {
    const [spec] = parseAccountSpecs([`staging=${ROLE_ARN}`]);
    expect(inspect(spec)).not.toContain("123456789012");
    expect(JSON.stringify(spec)).not.toContain("123456789012");
  });
});

describe("AccountsApi", () => {
  it("lists labels only", async () => {
    const { api } = twoAccounts();
    const response = await api.handle(apiRequest("GET", "/accounts"));
    expect(response.status).toBe(200);
    expect(response.body).toEqual({ accounts: [{ name: "staging" }] });
  });

  it("answers with the primary account without the parameter", async () => {
    const { api, staging } = twoAccounts();
    const response = await api.handle(apiRequest("GET", "/sandboxes"));
    expect((response.body.sandboxes as unknown[]).length).toBe(1);
    expect(staging.plane.operations()).toEqual([]);
  });

  it("the parameter selects another account and is not forwarded", async () => {
    const { api, primary } = twoAccounts();
    const response = await api.handle(
      apiRequest("GET", "/sandboxes", { query: { account: ["staging"] } }),
    );
    expect((response.body.sandboxes as unknown[]).length).toBe(2);
    expect(primary.plane.operations()).toEqual([]);
    const meta = await api.handle(apiRequest("GET", "/meta", { query: { account: ["staging"] } }));
    expect(meta.body.allowActions).toBe(false);
  });

  it("refuses an unknown account before any AWS call", async () => {
    const { api, primary, staging } = twoAccounts();
    const response = await api.handle(
      apiRequest("GET", "/sandboxes", { query: { account: ["nope"] } }),
    );
    expect(response.status).toBe(400);
    expect(response.body).toMatchObject({ error: { code: "invalid_argument" } });
    expect([...primary.plane.operations(), ...staging.plane.operations()]).toEqual([]);
  });

  it("additional accounts never run actions", async () => {
    const { api, primary, staging } = twoAccounts();
    const response = await api.handle(
      apiRequest("POST", "/sandboxes/x/kill", {
        query: { account: ["staging"] },
        body: { confirm: true },
      }),
    );
    expect(response.status).toBe(403);
    const error = (response.body as { error: { code: string; message: string } }).error;
    expect(error.code).toBe("actions_disabled");
    expect(error.message).toContain("sólo lectura");
    expect(error.message).not.toContain("--allow-actions");
    const operations = [...staging.plane.operations(), ...primary.plane.operations()];
    expect(operations.filter((name) => WRITES.includes(name))).toEqual([]);
  });

  it("csrf rules are the primary ones and close closes everything", async () => {
    const { api } = twoAccounts();
    expect(api.requiresCsrf(apiRequest("POST", "/actions"))).toBe(true);
    expect(api.requiresCsrf(apiRequest("GET", "/sandboxes"))).toBe(false);
    await api.close();
  });
});

const SDK_METADATA = "$metadata";

class FakeSts implements StsSender {
  readonly calls: Array<Record<string, unknown>> = [];

  constructor(private readonly options: { fail?: string; expiration?: unknown } = {}) {}

  async send(command: AssumeRoleCommand): Promise<AssumeRoleCommandOutput> {
    this.calls.push({ ...command.input });
    if (this.options.fail !== undefined) {
      throw awsSdkError(`no puedes asumir ${ROLE_ARN}`, this.options.fail);
    }
    return {
      Credentials: {
        AccessKeyId: "AKIAEXAMPLE",
        SecretAccessKey: "secret",
        SessionToken: "token",
        Expiration: ("expiration" in this.options
          ? this.options.expiration
          : new Date("2099-01-01T00:00:00Z")) as Date,
      },
      [SDK_METADATA]: {},
    };
  }
}

describe("assumed role", () => {
  it("asks STS with the named session and the external id", async () => {
    const sts = new FakeSts();
    const fetched = await new AssumedRole(sts, { roleArn: ROLE_ARN, externalId: "ext-1" }).fetch();
    expect(sts.calls).toEqual([
      {
        RoleArn: ROLE_ARN,
        RoleSessionName: DASHBOARD_ASSUME_ROLE_SESSION_NAME,
        DurationSeconds: DASHBOARD_ASSUME_ROLE_SESSION_SECONDS,
        ExternalId: "ext-1",
      },
    ]);
    expect(fetched.accessKeyId).toBe("AKIAEXAMPLE");
  });

  it("sends no ExternalId without one", async () => {
    const sts = new FakeSts();
    await new AssumedRole(sts, { roleArn: ROLE_ARN }).fetch();
    expect(sts.calls[0]).not.toHaveProperty("ExternalId");
  });

  it.each(["2099-01-01T00:00:00Z", 4070908800, {}])(
    "credentials without a real expiry date (%s) are an authentication error",
    async (expiration) => {
      const role = new AssumedRole(new FakeSts({ expiration }), { roleArn: ROLE_ARN });
      await expect(role.fetch()).rejects.toThrow(AuthenticationError);
      await expect(role.fetch()).rejects.toThrow("caducidad");
    },
  );

  it("a rejected AssumeRole keeps the AWS code and drops the message", async () => {
    const role = new AssumedRole(new FakeSts({ fail: "AccessDenied" }), { roleArn: ROLE_ARN });
    const error = await role.fetch().then(
      () => undefined,
      (caught: unknown) => caught as AuthenticationError,
    );
    expect(error).toBeInstanceOf(AuthenticationError);
    expect(error?.awsCode).toBe("AccessDenied");
    expect(error?.message).not.toContain("123456789012");
    expect(error?.cause).toBeUndefined();
  });
});

describe("assumedCredentials", () => {
  const Ms = 1000;

  it("makes no STS call until a client signs", async () => {
    const sts = new FakeSts();
    const provider = assumedCredentials(new AssumedRole(sts, { roleArn: ROLE_ARN }));
    expect(sts.calls).toEqual([]);
    expect((await provider()).accessKeyId).toBe("AKIAEXAMPLE");
    expect(sts.calls).toHaveLength(1);
  });

  it("reuses the credentials until the refresh margin and then asks again", async () => {
    let now = 0;
    const expiration = new Date(now + 3600 * Ms);
    const sts = new FakeSts({ expiration });
    const provider = assumedCredentials(new AssumedRole(sts, { roleArn: ROLE_ARN }), () => now);
    await provider();
    now = expiration.getTime() - CREDENTIALS_REFRESH_MARGIN_MS - 1;
    await provider();
    expect(sts.calls).toHaveLength(1);
    now = expiration.getTime() - CREDENTIALS_REFRESH_MARGIN_MS;
    await provider();
    expect(sts.calls).toHaveLength(2);
  });

  it("shares one request between simultaneous callers", async () => {
    const sts = new FakeSts();
    const provider = assumedCredentials(new AssumedRole(sts, { roleArn: ROLE_ARN }));
    await Promise.all([provider(), provider(), provider()]);
    expect(sts.calls).toHaveLength(1);
  });

  it("a failure is not cached", async () => {
    const failing = new FakeSts({ fail: "AccessDenied" });
    const provider = assumedCredentials(new AssumedRole(failing, { roleArn: ROLE_ARN }));
    await expect(provider()).rejects.toThrow(AuthenticationError);
    await expect(provider()).rejects.toThrow(AuthenticationError);
    expect(failing.calls).toHaveLength(2);
  });
});
