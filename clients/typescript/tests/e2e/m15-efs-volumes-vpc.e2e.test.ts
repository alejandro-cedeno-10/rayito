/**
 * `EfsVolumes.check()` contra AWS real (`RAYITO_E2E=1`, `m15-efs-volumes`):
 * la comprobación previa de sólo lectura sobre una VPC existente, espejo
 * de `test_check_is_read_only_and_accepts_the_network` (el despliegue
 * completo lo cubre el e2e de Python). Necesita `RAYITO_E2E_VPC_ID` y
 * `RAYITO_E2E_SUBNET_IDS` (de 1 a 3 subredes separadas por comas); nunca
 * crea nada ni imprime un id.
 */

import { describe, expect, test } from "vitest";
import { EfsVolumes } from "../../src/index.js";
import { e2eEnabled } from "./helpers.js";

const VPC_ID_VAR = "RAYITO_E2E_VPC_ID";
const SUBNET_IDS_VAR = "RAYITO_E2E_SUBNET_IDS";

const suiteEnabled =
  e2eEnabled() && Boolean(process.env[VPC_ID_VAR]) && Boolean(process.env[SUBNET_IDS_VAR]);

describe.runIf(suiteEnabled)(
  `EfsVolumes.check en una VPC existente (requiere RAYITO_E2E=1, ${VPC_ID_VAR} y ${SUBNET_IDS_VAR})`,
  () => {
    test("is read-only and accepts the network", async () => {
      const volumes = new EfsVolumes({ region: process.env.AWS_REGION });
      const report = await volumes.check({
        vpcId: process.env[VPC_ID_VAR] as string,
        subnetIds: process.env[SUBNET_IDS_VAR] as string,
      });
      console.log(
        `[efs-volumes vpc e2e] check: ${report.status} ${JSON.stringify(
          report.findings.map((finding) => [finding.code, finding.level]),
        )}`,
      );
      expect(report.ok).toBe(true);
    });
  },
);
