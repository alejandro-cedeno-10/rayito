"""`mounts=` (`m15-s3-mounts`, ADR-017): montajes de buckets S3 en el guest
vía `mount-s3`/FUSE, sólo sobre `rayito-base-caps`. Paquete interno (no
reexportado todavía desde `rayito/__init__.py`, seam de M15 foundations
reservada a esa función): el wiring público de `Sandbox.create(mounts=)` lo
completa `_feature_options.plan_features` en un cambio posterior; hasta
entonces, `mounts=` sigue lanzando `UnimplementedError` como en 0.5.x/M15
foundations y este módulo se usa directamente
(`from rayito._s3_mounts import S3Mount`).
"""

from __future__ import annotations

from ._domain import MOUNT_ERROR_CLASSES, MountPhase, MountStatus, S3Mount
from ._section import (
    S3MountsSection,
    check_section_result,
    from_proto_status,
    plan_s3_mounts,
    to_proto,
)

__all__ = [
    "MOUNT_ERROR_CLASSES",
    "MountPhase",
    "MountStatus",
    "S3Mount",
    "S3MountsSection",
    "check_section_result",
    "from_proto_status",
    "plan_s3_mounts",
    "to_proto",
]
