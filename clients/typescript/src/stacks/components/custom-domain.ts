/**
 * Stub de componente para `m15-custom-domain` (M15 foundations). Necesita
 * un dominio del mantenedor y un certificado ACM en us-east-1 (D3). La
 * feature sustituye `COMPONENT` por la definición real en su propio cambio.
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "custom-domain",
  description:
    "Pendiente de m15-custom-domain: distribución CloudFront, CloudFront Function de " +
    "enrutado y KeyValueStore, para domain.",
  supported: false,
  cost: { creates: [], idleMonthly: "pendiente de m15-custom-domain" },
};
