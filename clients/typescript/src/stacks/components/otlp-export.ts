/**
 * Stub de componente para `m15-rayd-otlp` (M15 foundations). La feature
 * sustituye `COMPONENT` por la definición real en su propio cambio.
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "otlp-export",
  description: "Pendiente de m15-rayd-otlp: política RayitoOtlpExport para telemetry.",
  supported: false,
  cost: { creates: [], idleMonthly: "$0 (sólo IAM, cuando exista)" },
};
