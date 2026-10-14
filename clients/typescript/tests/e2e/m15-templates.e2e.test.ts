/**
 * m15-templates contra AWS real (`RAYITO_E2E=1`, `RAYITO_TEMPLATE` y
 * `RAYITO_E2E_TEMPLATE_BUCKET` con un bucket S3 ya existente para el
 * artefacto de build). Cubre los pasos 1-3 del plan de aceptación de
 * `openspec/changes/archive/2026-10-03-m15-templates/proposal.md`:
 *
 * 1. un build correcto (`pipInstall`), esperando `BuildInfo` y que la
 *    imagen quede lanzable.
 * 2. un build que falla en un paso del Dockerfile (`pipInstall` de un
 *    paquete inexistente), esperando `BuildError` con `step`/`command`/
 *    `exitCode` extraídos del log de BuildKit.
 * 3. un `readyCmd` fijo que siempre falla, esperando
 *    `BuildError({ reason: "ready_client_error" | "ready_server_error" })`.
 *
 * El paso 4 (el `startCmd` horneado por `setStartCmd()` sobrevive a un
 * ciclo de suspend/resume dentro de un sandbox lanzado) lo cubre la etapa
 * de aceptación a mano (necesita una `rayito-base` publicada con el `rayd`
 * de esta versión, que lee `/etc/rayito/template.json`).
 *
 * Nombres de imagen con un sufijo aleatorio por corrida. El `afterEach`
 * borra la imagen que construyó cada test (pase o falle), con sus
 * versiones y su grupo de logs `/rayito/<nombre>` (`image-cleanup.ts`); lo
 * que no pueda borrar hace fallar el test con el nombre, para borrarlo a
 * mano. Necesita `lambda:DeleteMicrovmImage` y `logs:DeleteLogGroup` sobre
 * `/rayito/*` en la identidad de la aceptación.
 */

import { randomBytes } from "node:crypto";
import { CloudWatchLogsClient } from "@aws-sdk/client-cloudwatch-logs";
import { afterEach, describe, expect, test } from "vitest";
import {
  BuildError,
  type BuildOptions,
  InvalidArgumentError,
  Sandbox,
  Template,
} from "../../src/index.js";
import { NODE_PACKAGES, NPM_MISSING_EXIT_CODE } from "../../src/templates/dsl.js";
import { e2eEnabled, useE2E } from "./helpers.js";
import { BuiltImages } from "./image-cleanup.js";

const BUCKET_VAR = "RAYITO_E2E_TEMPLATE_BUCKET";
/** Build (~115 s, Q85) + el plazo por defecto de un `readyCmd` crudo (60 s)
 * tras el cual `rayd` responde 500, con holgura para la cola de builds. */
const READY_FAIL_BUDGET_MS = 600_000;
/** Paquete pequeño de los repos de AL2023 para comprobar `dnfInstall`. */
const DNF_PROBE_PACKAGE = "tree";
const NPM_PROBE_PACKAGE = "left-pad@1.3.0";
/** WORKDIR explícito: `npm ls --prefix` no depende del cwd del sandbox. */
const INSTALL_WORKDIR = "/opt/install-probe";
/** Usuario no root previo a `dnfInstall`: comprueba que el paso sube a root. */
const NON_ROOT_USER = "1000";
const INSTALL_SANDBOX_TIMEOUT_MS = 300_000;

/** `exactOptionalPropertyTypes` no deja pasar `region: undefined`
 * explícito: esto omite la clave del todo cuando no hay región. */
function regionOption(region: string | undefined): Pick<BuildOptions, "region"> {
  return region === undefined ? {} : { region };
}

function bucket(): string {
  const value = process.env[BUCKET_VAR];
  if (!value) {
    throw new Error(`este e2e necesita ${BUCKET_VAR}=<bucket-s3-del-artefacto-de-build>`);
  }
  return value;
}

function runName(label: string): string {
  return `rayito-m15-templates-e2e-${label}-${randomBytes(4).toString("hex")}`;
}

describe.runIf(e2eEnabled())("m15-templates (AWS real)", () => {
  const e2e = useE2E();
  let images: BuiltImages | undefined;

  /** Las imágenes del test en curso; se crean al primer uso porque el
   * plano de control de `useE2E()` sólo existe tras su `beforeAll`. */
  function builtImages(): BuiltImages {
    images ??= new BuiltImages({
      client: e2e.controlPlane.client,
      resolveArn: (name) => e2e.controlPlane.resolveTemplateArn(name),
      logs: new CloudWatchLogsClient(regionOption(e2e.settings.region)),
    });
    return images;
  }

  afterEach(async () => {
    const failures = (await images?.deleteAll()) ?? [];
    if (failures.length > 0) {
      throw new Error(`imágenes de test sin borrar (bórralas a mano): ${failures.join("; ")}`);
    }
  });

  test("a successful build produces a launchable image", async () => {
    const t = new Template()
      .fromBaseImage(e2e.settings.template)
      .pipInstall(["pandas"])
      .setEnvs({ RAYITO_M15_TEMPLATES_E2E: "1" });
    const name = builtImages().track(runName("ok"));
    const info = await Template.build(t, name, {
      bucket: bucket(),
      ...regionOption(e2e.settings.region),
    });
    expect(info.templateId.endsWith(`:microvm-image:${name}`)).toBe(true);
    expect(info.alias).toBe(name);
    expect(await Template.exists(name, regionOption(e2e.settings.region))).toBe(true);
  });

  test("a failing RUN step surfaces its step, command and exit code", async () => {
    const t = new Template()
      .fromBaseImage(e2e.settings.template)
      .pipInstall(["this-package-does-not-exist-rayito-m15-e2e"]);
    const name = builtImages().track(runName("fail-run"));
    await expect(
      Template.build(t, name, { bucket: bucket(), ...regionOption(e2e.settings.region) }),
    ).rejects.toMatchObject({
      step: expect.any(Number),
      command: expect.any(String),
      exitCode: expect.any(Number),
    });
  });

  test("a ready cmd that never succeeds fails the build", async () => {
    const t = new Template()
      .fromBaseImage(e2e.settings.template)
      .setStartCmd("sleep 3600", "exit 1");
    const name = builtImages().track(runName("fail-ready"));
    const error = await Template.build(t, name, {
      bucket: bucket(),
      timeoutMs: READY_FAIL_BUDGET_MS,
      ...regionOption(e2e.settings.region),
    }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(BuildError);
    expect((error as BuildError).reason).toBe("ready_server_error");
  });

  test("dnfInstall and npmInstall shortcuts are usable in a sandbox", async () => {
    const t = new Template()
      .fromBaseImage(e2e.settings.template)
      .setUser(NON_ROOT_USER)
      .dnfInstall([DNF_PROBE_PACKAGE, ...NODE_PACKAGES])
      .setUser("root")
      .runCmd(`mkdir -p ${INSTALL_WORKDIR}`)
      .workdir(INSTALL_WORKDIR)
      .npmInstall(NPM_PROBE_PACKAGE);
    const name = builtImages().track(runName("install"));
    await Template.build(t, name, { bucket: bucket(), ...regionOption(e2e.settings.region) });
    const sandbox = await Sandbox.create({
      template: await e2e.controlPlane.resolveTemplateArn(name),
      timeoutMs: INSTALL_SANDBOX_TIMEOUT_MS,
      idle: null,
      executionRoleArn: e2e.settings.executionRoleArn,
      logging: e2e.settings.logging,
      controlPlane: e2e.controlPlane,
    });
    try {
      expect((await sandbox.commands.run(`${DNF_PROBE_PACKAGE} --version`)).exitCode).toBe(0);
      expect((await sandbox.commands.run("node --version")).exitCode).toBe(0);
      expect(
        (await sandbox.commands.run(`npm ls --prefix ${INSTALL_WORKDIR} left-pad`)).exitCode,
      ).toBe(0);
    } finally {
      await sandbox.kill().catch(() => undefined);
    }
  });

  test("npmInstall without node fails with a clear step", async () => {
    const t = new Template().fromBaseImage(e2e.settings.template).npmInstall(NPM_PROBE_PACKAGE);
    const name = builtImages().track(runName("no-node"));
    const error = await Template.build(t, name, {
      bucket: bucket(),
      ...regionOption(e2e.settings.region),
    }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(BuildError);
    expect((error as BuildError).exitCode).toBe(NPM_MISSING_EXIT_CODE);
  });
});

describe("m15-templates (sin AWS real)", () => {
  test("building without fromBaseImage never reaches AWS", async () => {
    await expect(
      Template.build(new Template().pipInstall(["pandas"]), "unused", { bucket: "unused" }),
    ).rejects.toBeInstanceOf(InvalidArgumentError);
  });
});
