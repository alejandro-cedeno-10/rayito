/**
 * Los escenarios de sólo lectura del panel local `rayito dashboard` de
 * TypeScript (ADR-027, fase 8) que comparten los e2e contra AWS real
 * (`tests/e2e`) y los locales contra Docker + Floci (`tests/local`): arranca
 * el servidor con `runCli`, canjea el token de arranque y valida con
 * `api-v1.schema.json` cada ruta `GET` que no cuesta, las dos guardas que
 * rechazan antes de llamar a AWS (acciones sin `--allow-actions` y coste
 * facturado apagado) y que sin sesión no contesta nada. No crea sandboxes ni
 * ejecuta acciones.
 */

import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { runCli } from "../../src/cli/run.js";
import type { ServicesFactory } from "../../src/cli/services.js";
import { FakeTerminal } from "../unit/cli/terminal.js";
import { validateAgainstSchema } from "../unit/fake/dashboard-world.js";

const URL_LINE = 1;
const POLL_MS = 20;
const START_BUDGET_MS = 30_000;
const PLANNED_COUNT = 3;
const CSRF_HEADER = "X-Rayito-CSRF";

export interface DashboardWorld {
  readonly createServices: ServicesFactory;
  readonly env: Readonly<Record<string, string | undefined>>;
  /** El nombre de una imagen de la cuenta cuyas versiones lee el panel. */
  readonly imageName: string;
}

export function defineDashboardReadScenarios(world: DashboardWorld): void {
  describe("panel local de sólo lectura", () => {
    const interrupt = new AbortController();
    const terminal = new FakeTerminal();
    let finished: Promise<number>;
    let origin = "";
    let cookie = "";
    let csrf = "";

    async function get(path: string): Promise<{ status: number; body: Record<string, unknown> }> {
      const response = await fetch(`${origin}${path}`, { headers: { Cookie: cookie } });
      return { status: response.status, body: (await response.json()) as Record<string, unknown> };
    }

    beforeAll(async () => {
      finished = runCli(["dashboard"], {
        terminal,
        env: { ...world.env },
        now: () => new Date(),
        sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
        signal: interrupt.signal,
        createServices: world.createServices,
      });
      const deadline = Date.now() + START_BUDGET_MS;
      while (terminal.stdoutText.split("\n").length <= URL_LINE + 1) {
        if (Date.now() > deadline) {
          throw new Error(`el panel no arrancó: ${terminal.stderrText}`);
        }
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
      }
      const start = new URL(terminal.stdoutText.split("\n")[URL_LINE] as string);
      origin = start.origin;
      const redeemed = await fetch(`${origin}${start.pathname}${start.search}`, {
        redirect: "manual",
      });
      cookie = (redeemed.headers.getSetCookie()[0] as string).split(";")[0] as string;
      csrf = ((await get("/api/v1/session")).body as { csrfToken: string }).csrfToken;
    });

    afterAll(async () => {
      interrupt.abort();
      await finished;
    });

    test("meta says it is read-only and local", async () => {
      const { status, body } = await get("/api/v1/meta");
      expect(status).toBe(200);
      validateAgainstSchema(body, "meta");
      expect(body).toMatchObject({ deployment: "local", allowActions: false });
      expect((body.costExplorer as { enabled: boolean }).enabled).toBe(false);
    });

    test("sandboxes, images and versions are read from the account", async () => {
      const sandboxes = await get("/api/v1/sandboxes");
      expect(sandboxes.status).toBe(200);
      validateAgainstSchema(sandboxes.body, "sandboxes");
      const images = await get("/api/v1/images");
      expect(images.status).toBe(200);
      validateAgainstSchema(images.body, "images");
      const versions = await get(
        `/api/v1/images/versions?image=${encodeURIComponent(world.imageName)}`,
      );
      expect(versions.status).toBe(200);
      validateAgainstSchema(versions.body, "imageVersions");
      expect((versions.body.versions as unknown[]).length).toBeGreaterThan(0);
    });

    test("quotas and the free cost estimate", async () => {
      const quotas = await get("/api/v1/quotas");
      expect(quotas.status).toBe(200);
      validateAgainstSchema(quotas.body, "quotas");
      const cost = await get("/api/v1/cost");
      expect(cost.status).toBe(200);
      validateAgainstSchema(cost.body, "cost");
      expect(cost.body.billed).toBeNull();
    });

    test("events, accounts, jobs and the bulk plan", async () => {
      const events = await get("/api/v1/events");
      validateAgainstSchema(events.body, "events");
      expect(events.body.configured).toBe(false);
      validateAgainstSchema((await get("/api/v1/accounts")).body, "accounts");
      validateAgainstSchema((await get("/api/v1/actions")).body, "jobsDocument");
      const plan = await get(`/api/v1/actions/plan?action=kill&count=${PLANNED_COUNT}`);
      expect(plan.status).toBe(200);
      validateAgainstSchema(plan.body, "planDocument");
    });

    test("an action is refused before any AWS call and the billed cost stays off", async () => {
      const refused = await fetch(`${origin}/api/v1/actions`, {
        method: "POST",
        headers: { Cookie: cookie, [CSRF_HEADER]: csrf, "Content-Type": "application/json" },
        body: JSON.stringify({ action: "kill", sandboxIds: ["microvm-x"], confirm: true }),
      });
      expect(refused.status).toBe(403);
      expect(await refused.json()).toMatchObject({ error: { code: "actions_disabled" } });
      const billed = await fetch(`${origin}/api/v1/cost?billed=true`, {
        headers: { Cookie: cookie, [CSRF_HEADER]: csrf },
      });
      expect(billed.status).toBe(403);
      expect(await billed.json()).toMatchObject({ error: { code: "cost_explorer_disabled" } });
    });

    test("without the session nothing answers", async () => {
      const response = await fetch(`${origin}/api/v1/meta`);
      expect(response.status).toBe(401);
    });
  });
}
