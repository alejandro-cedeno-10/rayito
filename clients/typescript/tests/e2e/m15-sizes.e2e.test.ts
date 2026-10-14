/**
 * `size` contra AWS real, sólo lectura, espejo de
 * `clients/python/tests/e2e/test_m15_sizes_readonly.py`: sin lanzar ningún
 * MicroVM, resuelve `size` contra el catálogo cerrado, localiza la imagen de
 * ese tamaño por la convención de nombres (`<imagen>-<tamaño>`) y comprueba,
 * con la `GetMicrovmImageVersion` gratuita que usa `getInfo()`, que su
 * `minimumMemoryInMiB` es el que el SDK resolvió en cliente.
 *
 * Necesita `RAYITO_E2E=1`, `RAYITO_TEMPLATE`, `RAYITO_E2E_SIZE_IMAGE` (el
 * nombre de la imagen sin sufijo de tamaño, nunca su ARN),
 * `RAYITO_E2E_SIZE` (por ejemplo `4gb`, publicado con `--sizes`) y
 * `RAYITO_E2E_SIZE_IMAGE_VERSION` (una versión publicada de esa imagen de
 * tamaño); sin ellas se salta. Nunca imprime un ARN.
 */

import { describe, expect, test } from "vitest";
import { LambdaMicrovmsControlPlane } from "../../src/index.js";
import { applySizeSuffix, resolveSize, type SizeName } from "../../src/sizing/sizing.js";
import { e2eEnabled } from "./helpers.js";

const IMAGE_VAR = "RAYITO_E2E_SIZE_IMAGE";
const SIZE_VAR = "RAYITO_E2E_SIZE";
const VERSION_VAR = "RAYITO_E2E_SIZE_IMAGE_VERSION";

const suiteEnabled =
  e2eEnabled() &&
  Boolean(process.env[IMAGE_VAR]) &&
  Boolean(process.env[SIZE_VAR]) &&
  Boolean(process.env[VERSION_VAR]);

describe.runIf(suiteEnabled)(
  `size de sólo lectura (requiere RAYITO_E2E=1, ${IMAGE_VAR}, ${SIZE_VAR} y ${VERSION_VAR})`,
  () => {
    test("the published image of that size reports the memory the SDK resolved", async () => {
      const size = process.env[SIZE_VAR] as SizeName;
      const resolved = resolveSize(size);
      const plane = LambdaMicrovmsControlPlane.fromRegion(
        process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || undefined,
      );
      const sizedName = applySizeSuffix(process.env[IMAGE_VAR] as string, resolved);
      const arn = await plane.resolveTemplateArn(sizedName);
      const minimumMemoryMib = await plane.getMicrovmImageVersion(
        arn,
        process.env[VERSION_VAR] as string,
      );
      console.log(`[m15-sizes e2e] ${size}: ${minimumMemoryMib} MiB`);
      expect(minimumMemoryMib).toBe(resolved.memoryMib);
    });
  },
);
