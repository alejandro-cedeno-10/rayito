/**
 * Stub de componente para `m15-events-webhooks` (M15 foundations). La
 * feature sustituye `COMPONENT` por la definición real en su propio cambio.
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "events-webhooks",
  description:
    "Pendiente de m15-events-webhooks: secreto HMAC, tabla de eventos/webhooks, " +
    "forwarder/deliverer/reconciler y el scheduler, para events.",
  supported: false,
  cost: { creates: [], idleMonthly: "pendiente de m15-events-webhooks" },
};
