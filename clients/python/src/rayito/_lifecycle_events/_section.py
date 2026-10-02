"""`ConfigureSection` de `m15-events-webhooks`: ya con `k_sbx` derivado y la
metadata del sandbox resueltas (`LifecycleEvents._build_section`, llamado
después de `run-microvm`, cuando se conocen `sandbox_id`/`image_arn`/
`image_version` — nunca antes, por eso no sale de `_feature_options.plan_features`
tal cual).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rayito.v1 import configure_pb2

SECTION_NAME = "lifecycle_events"


@dataclass(frozen=True)
class LifecycleEventsSection:
    sandbox_key: bytes
    sandbox_id: str
    image_arn: str
    image_version: str

    @property
    def section(self) -> str:
        return SECTION_NAME

    @property
    def required_flag(self) -> str:
        return SECTION_NAME

    def fill(self, request: configure_pb2.ConfigureRequest) -> None:
        from rayito.v1 import lifecycle_events_pb2

        request.lifecycle_events.CopyFrom(
            lifecycle_events_pb2.LifecycleEventsConfig(
                sandbox_key=self.sandbox_key,
                sandbox_id=self.sandbox_id,
                image_arn=self.image_arn,
                image_version=self.image_version,
            )
        )
