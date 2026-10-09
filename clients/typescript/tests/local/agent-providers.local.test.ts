/**
 * Los presets de pasarela de cada proveedor de modelo con OpenCode y
 * deepagents (`make local-providers-up` y `make local-e2e`), espejo de
 * `clients/python/tests/local/test_local_agent_providers_fake.py`: sin claves
 * reales ni Internet, contra el upstream HTTPS falso de
 * `dev/local/providers/fake_upstream.py`.
 *
 * Para cada preset de `testdata/agent/provider-catalogue.json` y cada
 * runtime que lo admite, con el egress cerrado: el agente completa una vuelta
 * y devuelve el texto del upstream falso; al upstream llega el valor del
 * secreto en la cabecera del preset y nunca `MODEL_CREDENTIAL_PLACEHOLDER`;
 * sólo llegan rutas de la allowlist, y una que no lo está recibe 403 de
 * `rayd` sin llegar al upstream.
 *
 * Además de `RAYITO_LOCAL_GUEST`, necesita
 * `RAYITO_LOCAL_FAKE_UPSTREAM_ADMIN` (la define
 * `dev/local/providers/compose.yaml`); sin ella se saltan.
 */

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { AgentLimits, EgressEnforcement } from "../../src/index.js";
import { MODEL_CREDENTIAL_PLACEHOLDER } from "../../src/limits.js";
import { type LocalHarness, localEnabled, localHarness, releaseGuest } from "./helpers.js";
import {
  AUTHORIZATION_HEADER,
  deepagentsSupported,
  entryOf,
  FAKE_UPSTREAM_ADMIN_VAR,
  PRESETS,
  PROMPT,
  Provider,
  ProviderBox,
  RUNTIMES,
  randomKey,
  secretValueFor,
} from "./providers.js";

/** El texto con el que contesta siempre el upstream falso (`REPLY_TEXT`). */
const FAKE_REPLY = "rayito-fake-ok";
const AGENT_TIMEOUT_MS = 240_000;
const SANDBOX_SETUP_TIMEOUT_MS = 300_000;
const EGRESS_PROBE_TIMEOUT_SECONDS = 5;
/** Una ruta que ningún preset permite. */
const FORBIDDEN_PATH = "/v1/models";
const FORBIDDEN_STATUS = 403;

interface Received {
  readonly host: string;
  readonly path: string;
  readonly headers: Record<string, string>;
  readonly body: string;
}

const adminUrl = process.env[FAKE_UPSTREAM_ADMIN_VAR];

async function clearUpstream(): Promise<void> {
  await fetch(`${adminUrl}/requests`, { method: "DELETE" });
}

async function receivedBy(host: string): Promise<Received[]> {
  const entries = (await (await fetch(`${adminUrl}/requests`)).json()) as Received[];
  return entries.filter((entry) => entry.host.split(":")[0] === host);
}

describe.skipIf(!localEnabled() || !adminUrl)(
  "presets de proveedor contra el upstream falso",
  () => {
    let harness: LocalHarness;

    beforeAll(async () => {
      harness = await localHarness();
    });

    afterAll(async () => {
      if (harness !== undefined) {
        await releaseGuest(harness);
      }
    });

    describe.each(PRESETS)("%s", (name) => {
      const header = entryOf(name).headers[0] ?? AUTHORIZATION_HEADER;
      const secretValue = secretValueFor(header, randomKey());
      const provider = new Provider(name, secretValue);
      let box: ProviderBox;

      beforeAll(async () => {
        box = await ProviderBox.open(harness, provider);
      }, SANDBOX_SETUP_TIMEOUT_MS);

      afterAll(async () => {
        if (box !== undefined) {
          await box.close();
        }
      });

      it("egress cerrado y 403 fuera de la allowlist", async () => {
        expect((await box.sandbox.getHealth()).egressEnforcement).toBe(
          EgressEnforcement.GUEST_ROUTES,
        );
        const direct = await box.sh(
          `curl -sS -o /dev/null -m ${EGRESS_PROBE_TIMEOUT_SECONDS} https://${provider.host}/`,
        );
        expect(direct.exitCode).not.toBe(0);
        const url = box.sandbox.gateways.get(provider.route)?.url ?? "";
        await clearUpstream();
        const refused = await box.sh(
          `curl -sS -o /dev/null -w '%{http_code}' -X POST -d '{}' ${url}${FORBIDDEN_PATH}`,
        );
        expect(refused.exitCode).toBe(0);
        expect(Number(refused.stdout)).toBe(FORBIDDEN_STATUS);
        expect(await receivedBy(provider.host)).toEqual([]);
      });

      it.each(RUNTIMES)(
        "%s: una vuelta con la credencial real y sin el marcador",
        async (runtime) => {
          if (runtime === "deepagents" && !deepagentsSupported(provider.entry)) {
            return;
          }
          await clearUpstream();
          const result = await box.sandbox.agent.run(PROMPT, {
            spec: provider.spec(),
            runtime,
            limits: new AgentLimits({ timeoutMs: AGENT_TIMEOUT_MS }),
          });
          const received = await receivedBy(provider.host);
          expect(received.length).toBeGreaterThan(0);
          expect(result.text).toContain(FAKE_REPLY);
          for (const request of received) {
            expect(
              provider.allowedPaths.has(new URL(request.path, "https://upstream").pathname),
            ).toBe(true);
            const injectedIsReal = request.headers[provider.header] === secretValue;
            expect(injectedIsReal, "la cabecera del preset no llevó el valor del secreto").toBe(
              true,
            );
            const placeholderLeft =
              JSON.stringify(request.headers).includes(MODEL_CREDENTIAL_PLACEHOLDER) ||
              request.body.includes(MODEL_CREDENTIAL_PLACEHOLDER);
            expect(placeholderLeft).toBe(false);
          }
        },
        AGENT_TIMEOUT_MS,
      );
    });
  },
);
