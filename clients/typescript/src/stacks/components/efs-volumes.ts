/**
 * Stub de componente para `m15-efs-volumes` (M15 foundations, experimental).
 * La feature sustituye `COMPONENT` por la definición real en su propio
 * cambio, pendiente de la campaña de medición EFS-1..EFS-20.
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "efs-volumes",
  description:
    "Pendiente de m15-efs-volumes (experimental): sistema de ficheros EFS, mount " +
    "targets y conector, para volumes.",
  supported: false,
  cost: { creates: [], idleMonthly: "pendiente de la medición EFS" },
};
