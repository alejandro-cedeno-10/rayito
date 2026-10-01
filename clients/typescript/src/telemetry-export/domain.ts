/**
 * Pure domain of m15-rayd-otlp (ADR-021). Mirrors `rayito._telemetry_export._domain`:
 * `OtlpAuth`/`TelemetryExport` validate at construction, no AWS or transport
 * import; `plan()` adds the one pre-launch check that can be done on the
 * client (the caps variant, via `role-policy.ts`). Everything else
 * (resolving an `OtlpAuth.bearer`, filling in image facts, sending
 * `ConfigureSandbox`) lives in `section.ts`, which does touch AWS, and runs
 * only after the sandbox is `RUNNING`.
 */

import { InvalidArgumentError } from "../errors.js";
import { requireCapsFor } from "../role-policy.js";

/** 15..=300 s (research `docs/research/2026-10-e2b-out-of-scope.md` §6.3/§6.8,
 * OT2 will measure billed bytes at this cadence against the real account
 * quota); provisional until the acceptance stage confirms it. */
export const MIN_INTERVAL_S = 15;
export const MAX_INTERVAL_S = 300;
export const DEFAULT_INTERVAL_S = 60;
export const DEFAULT_SERVICE_NAME = "rayito";

export type NameStyleOption = "rayito" | "e2b";
const NAME_STYLES: readonly NameStyleOption[] = ["rayito", "e2b"];

/**
 * How `rayd` signs each export (research §6.3: options B1/B1').
 *
 * `executionRole()` is SigV4 over the execution role's IMDS credentials:
 * needs `rayito-base-caps` (or a size-derived variant). `bearer(secretName)`
 * is experimental (B1'): the secret's value is resolved once, when the
 * section is sent (`section.resolveBearerToken`), and travels to `rayd`
 * through `ConfigureSandbox`, never through an environment variable (ADR-014
 * rule 4) nor in plain text anywhere else; works on `rayito-base`, no caps
 * needed.
 */
export class OtlpAuth {
  private constructor(
    readonly kind: "executionRole" | "bearer",
    readonly secretName?: string,
  ) {}

  static executionRole(): OtlpAuth {
    return new OtlpAuth("executionRole");
  }

  static bearer(secretName: string): OtlpAuth {
    if (secretName.trim() === "") {
      throw new InvalidArgumentError("OtlpAuth.bearer(secretName) no puede estar vacío");
    }
    return new OtlpAuth("bearer", secretName);
  }
}

export interface TelemetryExportOptions {
  readonly intervalS?: number;
  readonly serviceName?: string;
  readonly names?: NameStyleOption;
  readonly auth?: OtlpAuth;
}

/**
 * `telemetry` kwarg de `Sandbox.create()` (m15-rayd-otlp, ADR-021).
 * Validado al construirse: un valor fuera de rango es
 * `InvalidArgumentError` antes de `run-microvm`, nunca una sección
 * rechazada por `rayd` tras lanzar la `MicroVM`.
 *
 * Coste y activación
 * -------------------
 * Activa: pasar una instancia como `telemetry` en `Sandbox.create()`.
 * Recursos y llamadas AWS: ninguno propio más allá de lo que adjuntes: con
 *   `OtlpAuth.executionRole()` necesitas la política `RayitoOtlpExport`
 *   (`infra/otlp-export.yaml`, `rayito stack deploy otlp-export`) en el
 *   execution role; `rayd` hace un `PutMetricData` por lote exportado.
 * Coste aproximado: $0 por la opción en sí; CloudWatch factura las
 *   métricas personalizadas que de verdad se exporten.
 * IAM: `cloudwatch:PutMetricData` sobre el dataset OTLP por defecto de la
 *   cuenta (no se puede acotar por namespace, research OT9); con
 *   `OtlpAuth.bearer(...)`, el permiso de lectura del secreto.
 * Cómo apagarla: no pases `telemetry` (por defecto `undefined`); borra la
 *   pila `otlp-export` si ya no la usa ningún sandbox.
 * Ejemplo: ver el TSDoc de `SandboxCreateOptions.telemetry`.
 */
export class TelemetryExport {
  readonly intervalS: number;
  readonly serviceName: string;
  readonly names: NameStyleOption;
  readonly auth: OtlpAuth;

  constructor(options: TelemetryExportOptions = {}) {
    this.intervalS = options.intervalS ?? DEFAULT_INTERVAL_S;
    this.serviceName = options.serviceName ?? DEFAULT_SERVICE_NAME;
    this.names = options.names ?? "rayito";
    this.auth = options.auth ?? OtlpAuth.executionRole();
    if (this.intervalS < MIN_INTERVAL_S || this.intervalS > MAX_INTERVAL_S) {
      throw new InvalidArgumentError(
        `telemetry: TelemetryExport({ intervalS: ${this.intervalS} }) debe estar entre ` +
          `${MIN_INTERVAL_S} y ${MAX_INTERVAL_S}`,
      );
    }
    if (this.serviceName.trim() === "") {
      throw new InvalidArgumentError(
        "telemetry: TelemetryExport({ serviceName }) no puede estar vacío",
      );
    }
    if (!NAME_STYLES.includes(this.names)) {
      throw new InvalidArgumentError(
        `telemetry: TelemetryExport({ names: ${JSON.stringify(this.names)} }) no reconocido: ` +
          `usa ${NAME_STYLES.join(" o ")}`,
      );
    }
  }
}

/**
 * `sbx.getTelemetryStatus()`: mirrors `TelemetryExportStatus`
 * (`ConfigureService.ConfigureStatus`). All zero/`undefined` when `rayd`
 * never applied a section (no `telemetry`, or the image doesn't support the
 * feature).
 */
export interface TelemetryHealth {
  readonly exported: bigint;
  readonly dropped: bigint;
  readonly lastErrorClass: string | undefined;
}

export const EMPTY_TELEMETRY_HEALTH: TelemetryHealth = Object.freeze({
  exported: 0n,
  dropped: 0n,
  lastErrorClass: undefined,
});

/**
 * Único chequeo previo a `run-microvm` además del tipo de `telemetry`: la
 * variante caps, cuando el nombre de imagen ya lo permite saber
 * (`requireCapsFor`, diferido a `Health.features` en otro caso).
 */
export function planTelemetry(
  telemetry: unknown,
  imageVariant: string | undefined,
): TelemetryExport {
  if (!(telemetry instanceof TelemetryExport)) {
    throw new InvalidArgumentError(`telemetry debe ser un TelemetryExport, no ${typeof telemetry}`);
  }
  if (telemetry.auth.kind === "executionRole") {
    requireCapsFor("telemetry", imageVariant);
  }
  return telemetry;
}
