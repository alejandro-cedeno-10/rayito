/**
 * `LifecycleEvents` contra AWS real, sólo lectura, espejo de
 * `clients/python/tests/e2e/test_lifecycle_events_readonly.py`: sobre una pila
 * `events-webhooks` YA desplegada, `status()` la describe, `listWebhooks()` y
 * `getEvents()` leen su tabla. Nunca despliega, registra ni borra nada, y no
 * lanza ningún sandbox.
 *
 * Necesita `RAYITO_E2E=1`, `RAYITO_TEMPLATE` y `RAYITO_E2E_EVENTS_STACK` (el
 * nombre de la pila desplegada); sin la última se salta. Nunca imprime ids,
 * URLs ni nombres de recursos: sólo estados y cuentas. Coste: una
 * `DescribeStacks` y dos consultas DynamoDB (< $0,00001).
 */

import { describe, expect, test } from "vitest";
import { LifecycleEvents } from "../../src/index.js";
import { e2eEnabled } from "./helpers.js";

const STACK_VAR = "RAYITO_E2E_EVENTS_STACK";
const READY_STATES = ["CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"];
const TABLE_OUTPUT = "EventsTableName";
const KEY_OUTPUT = "StackKeySecretArn";
const SAMPLE_EVENTS = 1;

const suiteEnabled = e2eEnabled() && Boolean(process.env[STACK_VAR]);

describe.runIf(suiteEnabled)(
  `LifecycleEvents de sólo lectura (requiere RAYITO_E2E=1, RAYITO_TEMPLATE y ${STACK_VAR})`,
  () => {
    const events = new LifecycleEvents({
      stackName: process.env[STACK_VAR] as string,
      region: process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || undefined,
    });

    test("status describes the deployed stack and exposes its outputs", async () => {
      const status = await events.status();
      expect(status).toBeDefined();
      expect(READY_STATES).toContain(status?.state);
      expect(Object.keys(status?.outputs ?? {})).toEqual(
        expect.arrayContaining([TABLE_OUTPUT, KEY_OUTPUT]),
      );
    });

    test("listWebhooks and getEvents read the table without changing it", async () => {
      const webhooks = await events.listWebhooks();
      const records = await events.getEvents({ limit: SAMPLE_EVENTS });
      console.log(
        `[lifecycle-events e2e] webhooks: ${webhooks.length}, eventos leídos: ${records.length}`,
      );
      expect(Array.isArray(webhooks)).toBe(true);
      expect(records.length).toBeLessThanOrEqual(SAMPLE_EVENTS);
    });
  },
);
