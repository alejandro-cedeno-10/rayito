from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class S3MountPhase(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    S3_MOUNT_PHASE_UNSPECIFIED: _ClassVar[S3MountPhase]
    S3_MOUNT_PHASE_PENDING: _ClassVar[S3MountPhase]
    S3_MOUNT_PHASE_MOUNTED: _ClassVar[S3MountPhase]
    S3_MOUNT_PHASE_FAILED: _ClassVar[S3MountPhase]
S3_MOUNT_PHASE_UNSPECIFIED: S3MountPhase
S3_MOUNT_PHASE_PENDING: S3MountPhase
S3_MOUNT_PHASE_MOUNTED: S3MountPhase
S3_MOUNT_PHASE_FAILED: S3MountPhase

class S3MountsConfig(_message.Message):
    __slots__ = ("mounts",)
    MOUNTS_FIELD_NUMBER: _ClassVar[int]
    mounts: _containers.RepeatedCompositeFieldContainer[S3Mount]
    def __init__(self, mounts: _Optional[_Iterable[_Union[S3Mount, _Mapping]]] = ...) -> None: ...

class S3Mount(_message.Message):
    __slots__ = ("mount_path", "bucket", "prefix", "read_only", "allow_overwrite", "allow_delete")
    MOUNT_PATH_FIELD_NUMBER: _ClassVar[int]
    BUCKET_FIELD_NUMBER: _ClassVar[int]
    PREFIX_FIELD_NUMBER: _ClassVar[int]
    READ_ONLY_FIELD_NUMBER: _ClassVar[int]
    ALLOW_OVERWRITE_FIELD_NUMBER: _ClassVar[int]
    ALLOW_DELETE_FIELD_NUMBER: _ClassVar[int]
    mount_path: str
    bucket: str
    prefix: str
    read_only: bool
    allow_overwrite: bool
    allow_delete: bool
    def __init__(self, mount_path: _Optional[str] = ..., bucket: _Optional[str] = ..., prefix: _Optional[str] = ..., read_only: _Optional[bool] = ..., allow_overwrite: _Optional[bool] = ..., allow_delete: _Optional[bool] = ...) -> None: ...

class S3MountsStatus(_message.Message):
    __slots__ = ("mounts",)
    MOUNTS_FIELD_NUMBER: _ClassVar[int]
    mounts: _containers.RepeatedCompositeFieldContainer[S3MountState]
    def __init__(self, mounts: _Optional[_Iterable[_Union[S3MountState, _Mapping]]] = ...) -> None: ...

class S3MountState(_message.Message):
    __slots__ = ("mount_path", "phase", "error_class")
    MOUNT_PATH_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    ERROR_CLASS_FIELD_NUMBER: _ClassVar[int]
    mount_path: str
    phase: S3MountPhase
    error_class: str
    def __init__(self, mount_path: _Optional[str] = ..., phase: _Optional[_Union[S3MountPhase, str]] = ..., error_class: _Optional[str] = ...) -> None: ...
