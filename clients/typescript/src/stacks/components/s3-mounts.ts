/**
 * Stub de componente para `m15-s3-mounts` (M15 foundations). La feature
 * sustituye `COMPONENT` por la definición real en su propio cambio.
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "s3-mounts",
  description: "Pendiente de m15-s3-mounts: política RayitoS3MountAccess para mounts.",
  supported: false,
  cost: { creates: [], idleMonthly: "$0 (sólo IAM, cuando exista)" },
};
