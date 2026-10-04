/**
 * `Sandbox.create({ volumes })` y el `volumeMounts` del shim de E2B contra AWS
 * real (`RAYITO_E2E=1`, `m15-efs-volumes`), espejo de
 * `clients/python/tests/e2e/test_efs_volumes_mount.py`: monta un volumen de
 * lectura y escritura y otro de sólo lectura sobre una pila `efs-volumes` ya
 * desplegada, escribe y lee como el usuario del sandbox (uid 1000), comprueba
 * que el de sólo lectura da `EROFS` y que el shim monta por el mismo camino.
 *
 * Necesita `RAYITO_EXECUTION_ROLE_ARN` (con la política `CallerPolicyArn` de
 * la pila), `RAYITO_E2E_EFS_STACK` (el nombre de la pila) y
 * `RAYITO_E2E_EFS_TEMPLATE` (una imagen con `amazon-efs-utils`). Nunca
 * imprime un id; crea dos access points efímeros y los borra al final.
 */

import { randomUUID } from "node:crypto";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { E2B } from "../../src/e2b/index.js";
import {
  CommandExitError,
  EfsVolume,
  EfsVolumes,
  Sandbox,
  type VolumeStore,
} from "../../src/index.js";
import { EXECUTION_ROLE_VAR, e2eEnabled } from "./helpers.js";

const STACK_VAR = "RAYITO_E2E_EFS_STACK";
const TEMPLATE_VAR = "RAYITO_E2E_EFS_TEMPLATE";
const CONNECTOR_OUTPUT = "ConnectorArn";
const READ_ONLY_ERROR = "Read-only file system";
const SANDBOX_TIMEOUT_MS = 600_000;
const E2E_TIMEOUT_MS = 300_000;

const suiteEnabled =
  e2eEnabled() &&
  Boolean(process.env[STACK_VAR]) &&
  Boolean(process.env[TEMPLATE_VAR]) &&
  Boolean(process.env[EXECUTION_ROLE_VAR]);

describe.runIf(suiteEnabled)(
  `volumes en un sandbox (requiere RAYITO_E2E=1, ${STACK_VAR}, ${TEMPLATE_VAR} y ${EXECUTION_ROLE_VAR})`,
  () => {
    const region = process.env.AWS_REGION;
    const executionRoleArn = process.env[EXECUTION_ROLE_VAR] as string;
    const template = process.env[TEMPLATE_VAR] as string;
    const suffix = randomUUID().slice(0, 8);
    const names = [`e2e-rw-${suffix}`, `e2e-ro-${suffix}`] as const;
    let store: VolumeStore;
    let connectorArn: string;
    let readWrite: EfsVolume;
    let readOnly: EfsVolume;

    beforeAll(async () => {
      const efs = new EfsVolumes({ stackName: process.env[STACK_VAR] as string, region });
      const status = await efs.status();
      const connector = status?.outputs[CONNECTOR_OUTPUT];
      if (connector === undefined) {
        throw new Error(`la pila de ${STACK_VAR} no existe`);
      }
      connectorArn = connector;
      store = await efs.volumeStore();
      readWrite = await store.create(names[0]);
      const created = await store.create(names[1]);
      readOnly = new EfsVolume({
        fileSystemId: created.fileSystemId,
        accessPointId: created.accessPointId,
        readOnly: true,
      });
    }, E2E_TIMEOUT_MS);

    afterAll(async () => {
      for (const name of names) {
        await store?.destroy(name).catch(() => false);
      }
    }, E2E_TIMEOUT_MS);

    test(
      "create mounts read-write and read-only volumes",
      async () => {
        const sandbox = await Sandbox.create({
          template,
          timeoutMs: SANDBOX_TIMEOUT_MS,
          executionRoleArn,
          egress: [connectorArn],
          region,
          volumes: { "/mnt/rw": readWrite, "/mnt/ro": readOnly },
        });
        try {
          const states = Object.fromEntries(
            [...(await sandbox.volumes())].map(([path, status]) => [path, status.state]),
          );
          console.log(`[efs-volumes mount e2e] states: ${JSON.stringify(states)}`);
          expect(states).toEqual({ "/mnt/rw": "mounted", "/mnt/ro": "mounted" });
          const written = await sandbox.commands.run(
            "echo hola > /mnt/rw/e2e-ts.txt && cat /mnt/rw/e2e-ts.txt",
          );
          expect(written.stdout.trim()).toBe("hola");
          const refused = await sandbox.commands.run("touch /mnt/ro/e2e-ts.txt").catch((e) => e);
          expect(refused).toBeInstanceOf(CommandExitError);
          expect((refused as CommandExitError).stderr).toContain(READ_ONLY_ERROR);
        } finally {
          await sandbox.kill().catch(() => undefined);
        }
      },
      E2E_TIMEOUT_MS,
    );

    test(
      "the e2b shim Volume mounts through the connector",
      async () => {
        const client = new E2B({ region, volumeStore: store, volumeConnectorArn: connectorArn });
        const volume = await client.Volume.create(`e2e-shim-${randomUUID().slice(0, 8)}`);
        try {
          const sandbox = await client.Sandbox.create(template, {
            timeoutMs: SANDBOX_TIMEOUT_MS,
            executionRoleArn,
            volumeMounts: { "/mnt/shared": volume, "/mnt/again": volume.volumeId },
          });
          try {
            const written = await sandbox.commands.run(
              "echo shim > /mnt/shared/shim-ts.txt && cat /mnt/again/shim-ts.txt",
            );
            expect(written.stdout.trim()).toBe("shim");
          } finally {
            await sandbox.kill().catch(() => undefined);
          }
        } finally {
          await client.Volume.destroy(volume.volumeId).catch(() => false);
        }
      },
      E2E_TIMEOUT_MS,
    );
  },
);
