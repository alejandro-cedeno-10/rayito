/**
 * `ConfigureSection` de `m15-events-webhooks`: ya con `k_sbx` derivado y la
 * metadata del sandbox resueltas (`LifecycleEvents.buildSection`, llamado
 * después de `run-microvm`, cuando se conocen `sandboxId`/`imageArn`/
 * `imageVersion`). `planFeatures` sólo guarda el `LifecycleEvents` ya
 * validado; `plannedSections` añade un `LifecycleEventsSectionFactory` en
 * cuanto `create()` conoce esos hechos. Espejo de
 * `rayito._lifecycle_events._section`.
 */

import { create } from "@bufbuild/protobuf";
import { type ConfigureSectionFactory, ImmediateSection } from "../configure/base.js";
import type { ConfigureRequest } from "../gen/rayito/v1/configure_pb.js";
import { LifecycleEventsConfigSchema } from "../gen/rayito/v1/lifecycle_events_pb.js";

export const SECTION_NAME = "lifecycle_events";

/** Lo que sólo `run-microvm` sabe y la sección necesita. */
export interface LifecycleEventsSectionFacts {
  readonly sandboxId: string;
  readonly imageArn: string;
  readonly imageVersion: string;
}

/**
 * `configure/base.ts`'s `ConfigureSection`: `rayd` la aplica en el acto
 * (nunca `PENDING`); un `INVALID` (clave o `sandboxId` vacíos) lanza por
 * `raiseSectionError`, como `gateways`/`telemetry`.
 */
export class LifecycleEventsSection extends ImmediateSection {
  readonly section = SECTION_NAME;
  readonly requiredFlag = "lifecycleEvents";
  /** `k_sbx` en un campo privado de ES: `util.inspect`, `console.log` y
   * `JSON.stringify` no lo ven. */
  readonly #sandboxKey: Uint8Array;

  constructor(
    sandboxKey: Uint8Array,
    private readonly facts: LifecycleEventsSectionFacts,
  ) {
    super();
    this.#sandboxKey = sandboxKey;
  }

  fill(request: ConfigureRequest): void {
    request.lifecycleEvents = create(LifecycleEventsConfigSchema, {
      sandboxKey: this.#sandboxKey,
      sandboxId: this.facts.sandboxId,
      imageArn: this.facts.imageArn,
      imageVersion: this.facts.imageVersion,
    });
  }
}

/** Lo que `LifecycleEventsSectionFactory` necesita de `LifecycleEvents`
 * (sólo `buildSection`): evita un import circular con `service.ts`. */
export interface LifecycleEventsSectionSource {
  buildSection(facts: LifecycleEventsSectionFacts): Promise<LifecycleEventsSection>;
}

/**
 * `ConfigureSectionFactory` de `events`: los hechos de `run-microvm` ya
 * fijados; `resolveSections` la construye justo antes del `Configure`, y es
 * entonces cuando `LifecycleEvents` lee la clave del stack (un
 * `GetSecretValue` por instancia, con sus propias credenciales: la clave
 * del stack no es un secreto de `secrets`, así que la `SecretCache` del
 * handle no se usa).
 */
export class LifecycleEventsSectionFactory implements ConfigureSectionFactory {
  constructor(
    readonly events: LifecycleEventsSectionSource,
    readonly facts: LifecycleEventsSectionFacts,
  ) {}

  build(): Promise<LifecycleEventsSection> {
    return this.events.buildSection(this.facts);
  }
}
