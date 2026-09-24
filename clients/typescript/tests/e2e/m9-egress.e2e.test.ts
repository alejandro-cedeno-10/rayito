/**
 * M9 `m9-egress-policy` a través del SDK TypeScript contra AWS real, espejo
 * de `clients/python/tests/e2e/test_m9_egress.py` (design D19).
 *
 * Con `RAYITO_TEMPLATE_CAPS` (imagen M9 de `rayito-base-caps`):
 * `allowInternetAccess: false` bloquea a uid 1000 (urllib en menos de 5 s y
 * toda dirección resuelta; el nombre puede resolverse por los resolvedores de
 * la plataforma dentro del guest, adenda de ADR-012) mientras un servidor en
 * loopback sigue accesible por `getHost`; la
 * lista de nombres de host responde 200 por las variables del proxy local y
 * 403 para otro host, y lo que ignora el proxy falla; el ciclo
 * `updateNetwork` (instancia y estático) sigue cada política en menos de 1 s.
 *
 * Con `RAYITO_TEMPLATE` (`rayito-base`, sin `CAP_NET_ADMIN`):
 * `create({ allowInternetAccess: false })` rechaza con `UnimplementedError`
 * tras terminar el MicroVM, también con `keepOnFailure`, y `updateNetwork`
 * con restricciones es `UnimplementedError`.
 *
 * Las dos suites se omiten sin `RAYITO_TEMPLATE_CAPS`: su presencia es la
 * señal de que las dos imágenes son de M9 (con un `rayito-base` anterior a M9,
 * `UpdateNetwork` respondería `Unimplemented`). Nada de aquí imprime listas
 * de la política, direcciones de proxy ni credenciales.
 */

import { lookup } from "node:dns/promises";
import { afterAll, describe, expect, test } from "vitest";
import {
  ALL_TRAFFIC,
  CommandExitError,
  type CommandResult,
  type ControlPlane,
  EgressEnforcement,
  type NetworkPolicyInput,
  Sandbox,
  type SandboxInfo,
  SandboxNotFoundError,
  UnimplementedError,
} from "../../src/index.js";
import {
  createTestSandbox,
  type E2EContext,
  e2eEnabled,
  seconds,
  TEST_SANDBOX_TIMEOUT_MS,
  useE2E,
  waitUntil,
} from "./helpers.js";

const CAPS_TEMPLATE_VAR = "RAYITO_TEMPLATE_CAPS";
const CAPS_IMAGE = "rayito-base-caps";
const ALLOWED_HOST = "aws.amazon.com";
const OTHER_HOST = "example.com";
const RAW_TARGET = "1.1.1.1";
const HTTPS_PORT = 443;
const LOOPBACK_PORT = 8000;
const COMMAND_TIMEOUT_MS = 60_000;
const REFUSAL_BUDGET_S = 6;
const POLICY_SETTLE_BUDGET_S = 1;
const LOOPBACK_READY_BUDGET_MS = 30_000;
const TERMINATED_BUDGET_MS = 60_000;
const PROBE_PATH = "/home/user/egress_probe.py";

/**
 * Sondas como uid 1000, cada una con su tiempo medido dentro del VM (sin el
 * ida y vuelta del proxy de AWS): `tcp HOST PORT open|closed` repite el
 * connect hasta que coincide y escribe los segundos; `url URL` usa urllib (que
 * respeta `HTTPS_PROXY`) y escribe `status <código>` o `refused`;
 * `unreachable NAME` resuelve el nombre (puede resolverse o no: adenda de
 * ADR-012) e intenta conectar a cada dirección en el 443, y escribe
 * `unresolved`, `blocked` o `connected`. Sale con 1 cuando no se cumple.
 */
const PROBE_SCRIPT = `import socket
import sys
import time
import urllib.request


def tcp(host, port, want):
    start = time.monotonic()
    while True:
        try:
            socket.create_connection((host, int(port)), timeout=0.3).close()
            reachable = True
        except OSError:
            reachable = False
        elapsed = time.monotonic() - start
        if reachable == (want == "open"):
            print(f"{elapsed:.3f}")
            return 0
        if elapsed > 10:
            print(f"timeout {elapsed:.3f}")
            return 1
        time.sleep(0.05)


def url(target):
    start = time.monotonic()
    try:
        with urllib.request.urlopen(target, timeout=5) as response:
            print(f"status {response.status} {time.monotonic() - start:.3f}")
            return 0
    except Exception as exc:
        print(f"refused {time.monotonic() - start:.3f} {type(exc).__name__}")
        return 1


def unreachable(name):
    try:
        infos = socket.getaddrinfo(name, 443, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        print("unresolved")
        return 0
    for info in infos:
        try:
            socket.create_connection(info[4][:2], timeout=5).close()
        except OSError:
            continue
        print("connected")
        return 1
    print("blocked")
    return 0


COMMANDS = {"tcp": tcp, "url": url, "unreachable": unreachable}
sys.exit(COMMANDS[sys.argv[1]](*sys.argv[2:]))
`;

const m9Enabled = e2eEnabled() && Boolean(process.env[CAPS_TEMPLATE_VAR]);

function note(label: string, value: string): void {
  console.log(`\n[m9-egress] ${label}: ${value}`);
}

interface RecordingControlPlane {
  readonly plane: ControlPlane;
  readonly launched: string[];
  readonly terminated: string[];
}

/**
 * El plano real tras un `Proxy` que apunta qué MicroVMs se lanzaron y cuáles
 * se terminaron. El resto de métodos se enlaza al plano real (sus campos
 * privados no admiten otro `this`), así que un método nuevo de `ControlPlane`
 * no rompe el test.
 */
function recordingControlPlane(inner: ControlPlane): RecordingControlPlane {
  const launched: string[] = [];
  const terminated: string[] = [];
  const runMicrovm: ControlPlane["runMicrovm"] = async (request) => {
    const info: SandboxInfo = await inner.runMicrovm(request);
    launched.push(info.sandboxId);
    return info;
  };
  const terminateMicrovm: ControlPlane["terminateMicrovm"] = (sandboxId) => {
    terminated.push(sandboxId);
    return inner.terminateMicrovm(sandboxId);
  };
  const overrides: ReadonlyMap<PropertyKey, unknown> = new Map<PropertyKey, unknown>([
    ["runMicrovm", runMicrovm],
    ["terminateMicrovm", terminateMicrovm],
  ]);
  const plane = new Proxy(inner, {
    get(target, property) {
      if (overrides.has(property)) {
        return overrides.get(property);
      }
      const value: unknown = Reflect.get(target, property, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  return { plane, launched, terminated };
}

interface EgressCreateOptions {
  readonly network?: NetworkPolicyInput;
  readonly allowInternetAccess?: boolean;
  readonly allowedPorts?: readonly number[];
  readonly keepOnFailure?: boolean;
  readonly controlPlane?: ControlPlane;
}

/** `create()` con los guardarraíles de `helpers.ts` más la política de egress; el sandbox entra en el barrido. */
async function createEgressSandbox(
  context: E2EContext,
  options: EgressCreateOptions = {},
): Promise<Sandbox> {
  const started = performance.now();
  const sandbox = await Sandbox.create({
    template: context.templateArn,
    timeoutMs: TEST_SANDBOX_TIMEOUT_MS,
    idle: null,
    executionRoleArn: context.settings.executionRoleArn,
    ingress: ["ALL_INGRESS"],
    logging: context.settings.logging,
    controlPlane: options.controlPlane ?? context.controlPlane,
    network: options.network,
    allowInternetAccess: options.allowInternetAccess,
    allowedPorts: options.allowedPorts,
    keepOnFailure: options.keepOnFailure,
  });
  context.created.push(sandbox);
  note(`${sandbox.sandboxId}: create() con política de egress`, `${seconds(started).toFixed(2)} s`);
  return sandbox;
}

/** El comando como uid 1000; una salida distinta de cero se devuelve en vez de lanzarse. */
async function attempt(sandbox: Sandbox, cmd: string): Promise<CommandResult | CommandExitError> {
  try {
    return await sandbox.commands.run(cmd, { timeoutMs: COMMAND_TIMEOUT_MS });
  } catch (error) {
    if (error instanceof CommandExitError) {
      return error;
    }
    throw error;
  }
}

async function installProbe(sandbox: Sandbox): Promise<void> {
  await sandbox.files.write(PROBE_PATH, PROBE_SCRIPT);
}

function probe(sandbox: Sandbox, args: string): Promise<CommandResult | CommandExitError> {
  return attempt(sandbox, `python3 ${PROBE_PATH} ${args}`);
}

/** Segundos que tardó la sonda `tcp` en ver `want` (falla si no lo vio). */
async function secondsUntilTcp(
  sandbox: Sandbox,
  address: string,
  want: "open" | "closed",
): Promise<number> {
  const outcome = await probe(sandbox, `tcp ${address} ${HTTPS_PORT} ${want}`);
  expect(outcome, `tcp ${want}: ${outcome.stdout}`).not.toBeInstanceOf(CommandExitError);
  return Number(outcome.stdout.trim());
}

async function firstIpv4(hostname: string): Promise<string> {
  const { address } = await lookup(hostname, { family: 4 });
  return address;
}

/** `TERMINATED` (o ya desaparecido) dentro del plazo de D19. */
async function waitTerminated(context: E2EContext, sandboxId: string): Promise<number> {
  return waitUntil(
    async () => {
      try {
        return (await context.controlPlane.getMicrovm(sandboxId)).state === "TERMINATED";
      } catch (error) {
        if (error instanceof SandboxNotFoundError) {
          return true;
        }
        throw error;
      }
    },
    TERMINATED_BUDGET_MS,
    `${sandboxId} TERMINATED`,
    1_000,
  );
}

async function terminateAll(context: E2EContext, sandboxIds: readonly string[]): Promise<void> {
  for (const sandboxId of sandboxIds) {
    try {
      await context.controlPlane.terminateMicrovm(sandboxId);
    } catch (error) {
      if (!(error instanceof SandboxNotFoundError)) {
        throw error;
      }
    }
  }
}

async function fetchLoopback(sandbox: Sandbox): Promise<number> {
  const host = await sandbox.getHost(LOOPBACK_PORT);
  const response = await fetch(`${host.url}/`, { headers: host.headers });
  await response.arrayBuffer();
  return response.status;
}

describe.skipIf(!m9Enabled)(
  `política de egress en ${CAPS_IMAGE} (requiere RAYITO_E2E=1 y ${CAPS_TEMPLATE_VAR})`,
  () => {
    const e2e = useE2E(CAPS_TEMPLATE_VAR);

    test("allowInternetAccess false blocks uid 1000 while loopback stays reachable", async () => {
      const sandbox = await createEgressSandbox(e2e, {
        allowInternetAccess: false,
        allowedPorts: [LOOPBACK_PORT],
      });
      await installProbe(sandbox);

      const refused = await probe(sandbox, `url https://${ALLOWED_HOST}`);
      expect(refused).toBeInstanceOf(CommandExitError);
      const [verdict, elapsed] = refused.stdout.trim().split(/\s+/);
      expect(verdict).toBe("refused");
      expect(Number(elapsed)).toBeLessThan(REFUSAL_BUDGET_S);
      note("urllib bloqueado (uid 1000)", `${elapsed} s`);

      // Adenda de ADR-012: el nombre puede resolverse; ninguna dirección conecta.
      const unreached = await probe(sandbox, `unreachable ${ALLOWED_HOST}`);
      expect(unreached, `unreachable: ${unreached.stdout}`).not.toBeInstanceOf(CommandExitError);
      expect(["unresolved", "blocked"]).toContain(unreached.stdout.trim());
      note("resolver + conectar (uid 1000)", unreached.stdout.trim());

      await sandbox.commands.run(
        `python3 -m http.server ${LOOPBACK_PORT} --bind 127.0.0.1 >/dev/null 2>&1`,
        { background: true, timeoutMs: 0 },
      );
      await waitUntil(
        async () => (await fetchLoopback(sandbox).catch(() => 0)) === 200,
        LOOPBACK_READY_BUDGET_MS,
        `getHost(${LOOPBACK_PORT}) -> 200`,
        500,
      );

      expect((await sandbox.getHealth()).egressEnforcement).toBe(EgressEnforcement.GUEST_ROUTES);
      const state = await sandbox.getNetwork();
      expect(state.denyOut).toEqual([ALL_TRAFFIC]);
      expect(state.enforcement).toBe(EgressEnforcement.GUEST_ROUTES);
    });

    test("hostname allowlist: 200 through the proxy variables, 403 for another host", async () => {
      const sandbox = await createEgressSandbox(e2e, {
        network: { allowOut: [ALLOWED_HOST], denyOut: [ALL_TRAFFIC] },
      });
      await installProbe(sandbox);
      expect((await sandbox.getHealth()).egressEnforcement).toBe(
        EgressEnforcement.GUEST_ROUTES_AND_PROXY,
      );
      expect((await sandbox.getNetwork()).localProxyPort).toBeGreaterThan(0);

      const allowed = await attempt(
        sandbox,
        `curl -sS -o /dev/null -w '%{http_code}' --max-time 20 https://${ALLOWED_HOST}`,
      );
      expect(allowed).not.toBeInstanceOf(CommandExitError);
      expect(allowed.stdout.trim()).toBe("200");

      const other = await attempt(
        sandbox,
        `curl -sS -o /dev/null --max-time 20 https://${OTHER_HOST}`,
      );
      expect(other).toBeInstanceOf(CommandExitError);
      expect(other.stderr).toContain("403");

      const bypass = await attempt(
        sandbox,
        `curl --noproxy '*' -sS -o /dev/null --max-time 10 https://${ALLOWED_HOST}`,
      );
      expect(bypass).toBeInstanceOf(CommandExitError);

      const raw = await attempt(
        sandbox,
        `python3 -c "import socket; socket.create_connection(('${RAW_TARGET}', ${HTTPS_PORT}), timeout=5)"`,
      );
      expect(raw).toBeInstanceOf(CommandExitError);

      const viaUrllib = await probe(sandbox, `url https://${ALLOWED_HOST}`);
      expect(viaUrllib.stdout).toMatch(/^status 200 /);
    });

    test("updateNetwork cycle (instance and static) follows each policy within 1 s", async () => {
      const target = await firstIpv4(ALLOWED_HOST);
      const sandbox = await createEgressSandbox(e2e);
      await installProbe(sandbox);
      expect(await secondsUntilTcp(sandbox, target, "open")).toBeLessThanOrEqual(
        POLICY_SETTLE_BUDGET_S,
      );

      const denied = await sandbox.updateNetwork(undefined, { allowInternetAccess: false });
      expect(denied.enforcement).toBe(EgressEnforcement.GUEST_ROUTES);
      expect(denied.denyOut).toEqual([ALL_TRAFFIC]);
      const closing = await secondsUntilTcp(sandbox, target, "closed");
      note("updateNetwork(allowInternetAccess: false) -> connect bloqueado", `${closing} s`);
      expect(closing).toBeLessThanOrEqual(POLICY_SETTLE_BUDGET_S);

      const opened = await Sandbox.updateNetwork(sandbox.sandboxId, undefined, {
        accessToken: sandbox.accessToken,
        controlPlane: e2e.controlPlane,
      });
      expect(opened.enforcement).toBe(EgressEnforcement.NONE);
      expect(opened.denyOut).toEqual([]);
      const opening = await secondsUntilTcp(sandbox, target, "open");
      note("Sandbox.updateNetwork(id, undefined) -> connect permitido", `${opening} s`);
      expect(opening).toBeLessThanOrEqual(POLICY_SETTLE_BUDGET_S);

      const reclosed = await Sandbox.updateNetwork(
        sandbox.sandboxId,
        { denyOut: ({ allTraffic }) => [allTraffic] },
        { accessToken: sandbox.accessToken, controlPlane: e2e.controlPlane },
      );
      expect(reclosed.enforcement).toBe(EgressEnforcement.GUEST_ROUTES);
      expect(await secondsUntilTcp(sandbox, target, "closed")).toBeLessThanOrEqual(
        POLICY_SETTLE_BUDGET_S,
      );
      expect((await sandbox.getNetwork()).denyOut).toEqual([ALL_TRAFFIC]);
      expect((await e2e.controlPlane.getMicrovm(sandbox.sandboxId)).state).toBe("RUNNING");
    });
  },
);

describe.skipIf(!m9Enabled)(
  `rayito-base sin CAP_NET_ADMIN: la política de egress falla cerrado (requiere RAYITO_E2E=1, RAYITO_TEMPLATE y ${CAPS_TEMPLATE_VAR})`,
  () => {
    const e2e = useE2E();
    const launched: string[] = [];

    afterAll(async () => {
      await terminateAll(e2e, launched);
    });

    test.each([false, true])(
      "allowInternetAccess false rejects after terminating the MicroVM (keepOnFailure %s)",
      async (keepOnFailure) => {
        const recording = recordingControlPlane(e2e.controlPlane);
        const error = await createEgressSandbox(e2e, {
          allowInternetAccess: false,
          keepOnFailure,
          controlPlane: recording.plane,
        }).catch((caught: unknown) => caught);
        launched.push(...recording.launched);

        expect(error).toBeInstanceOf(UnimplementedError);
        expect((error as UnimplementedError).feature).toBe("allowInternetAccess: false");
        expect((error as UnimplementedError).reason).toContain(CAPS_IMAGE);
        expect(recording.launched).toHaveLength(1);
        const sandboxId = recording.launched[0] as string;
        expect(recording.terminated).toContain(sandboxId);
        note(
          `${sandboxId} -> TERMINATED`,
          `${(await waitTerminated(e2e, sandboxId)).toFixed(2)} s`,
        );
      },
    );

    test("updateNetwork with restrictions is UnimplementedError; an open policy is accepted", async () => {
      const sandbox = await createTestSandbox(e2e);
      const error = await sandbox
        .updateNetwork({ denyOut: [ALL_TRAFFIC] })
        .catch((caught: unknown) => caught);
      expect(error).toBeInstanceOf(UnimplementedError);
      expect((error as UnimplementedError).reason).toContain(CAPS_IMAGE);
      const open = await sandbox.updateNetwork({});
      expect(open.enforcement).toBe(EgressEnforcement.NONE);
      expect((await sandbox.getHealth()).egressEnforcement).toBe(EgressEnforcement.NONE);
    });
  },
);
