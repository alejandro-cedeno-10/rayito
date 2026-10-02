/**
 * `ConfigureSection` de `m15-events-webhooks`: ya con `k_sbx` derivado y la
 * metadata del sandbox resueltas (`LifecycleEvents.buildSection`, llamado
 * después de `run-microvm`, cuando se conocen `sandboxId`/`imageArn`/
 * `imageVersion`).
 */

import { create } from "@bufbuild/protobuf";
import type { ConfigureRequest } from "../gen/rayito/v1/configure_pb.js";
import { LifecycleEventsConfigSchema } from "../gen/rayito/v1/lifecycle_events_pb.js";

export const SECTION_NAME = "lifecycle_events";

export interface LifecycleEventsSection {
  readonly section: typeof SECTION_NAME;
  readonly requiredFlag: typeof SECTION_NAME;
  fill(request: ConfigureRequest): void;
}

export function lifecycleEventsSection(options: {
  readonly sandboxKey: Uint8Array;
  readonly sandboxId: string;
  readonly imageArn: string;
  readonly imageVersion: string;
}): LifecycleEventsSection {
  return {
    section: SECTION_NAME,
    requiredFlag: SECTION_NAME,
    fill(request: ConfigureRequest): void {
      request.lifecycleEvents = create(LifecycleEventsConfigSchema, {
        sandboxKey: options.sandboxKey,
        sandboxId: options.sandboxId,
        imageArn: options.imageArn,
        imageVersion: options.imageVersion,
      });
    },
  };
}
