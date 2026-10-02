/**
 * m15-templates contra AWS real (`RAYITO_E2E=1`, `RAYITO_TEMPLATE` y
 * `RAYITO_E2E_TEMPLATE_BUCKET` con un bucket S3 ya existente para el
 * artefacto de build). Cubre los pasos 1-3 del plan de aceptación de
 * `openspec/changes/m15-templates/proposal.md`:
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
 * Nombres de imagen con un sufijo aleatorio por corrida; nada se borra aquí
 * (fuera de alcance de un agente de función): la limpieza de versiones de
 * imagen es responsabilidad de la etapa de aceptación serializada.
 */

import { randomBytes } from "node:crypto";
import { describe, expect, test } from "vitest";
import { BuildError, type BuildOptions, InvalidArgumentError, Template } from "../../src/index.js";
import { e2eEnabled, useE2E } from "./helpers.js";

const BUCKET_VAR = "RAYITO_E2E_TEMPLATE_BUCKET";
/** Build (~115 s, Q85) + el plazo por defecto de un `readyCmd` crudo (60 s)
 * tras el cual `rayd` responde 500, con holgura para la cola de builds. */
const READY_FAIL_BUDGET_MS = 600_000;

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

  test("a successful build produces a launchable image", async () => {
    const t = new Template()
      .fromBaseImage(e2e.settings.template)
      .pipInstall(["pandas"])
      .setEnvs({ RAYITO_M15_TEMPLATES_E2E: "1" });
    const name = runName("ok");
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
    const name = runName("fail-run");
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
    const name = runName("fail-ready");
    const error = await Template.build(t, name, {
      bucket: bucket(),
      timeoutMs: READY_FAIL_BUDGET_MS,
      ...regionOption(e2e.settings.region),
    }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(BuildError);
    expect((error as BuildError).reason).toBe("ready_server_error");
  });
});

describe("m15-templates (sin AWS real)", () => {
  test("building without fromBaseImage never reaches AWS", async () => {
    await expect(
      Template.build(new Template().pipInstall(["pandas"]), "unused", { bucket: "unused" }),
    ).rejects.toBeInstanceOf(InvalidArgumentError);
  });
});
