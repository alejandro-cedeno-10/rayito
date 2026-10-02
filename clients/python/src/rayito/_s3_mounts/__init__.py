"""`mounts=` (`m15-s3-mounts`, ADR-017): montajes de buckets S3 en el guest
vía `mount-s3`/FUSE, sólo sobre `rayito-base-caps`. Paquete interno
(`S3Mount`/`MountStatus` se reexportan desde `rayito/__init__.py`; el resto
—`S3MountsSection`, `plan_s3_mounts`, ...— es el adaptador de
`_feature_options.plan_features`, no API pública).
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
