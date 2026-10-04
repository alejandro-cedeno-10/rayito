from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class EfsVolumeState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EFS_VOLUME_STATE_UNSPECIFIED: _ClassVar[EfsVolumeState]
    EFS_VOLUME_STATE_REQUESTED: _ClassVar[EfsVolumeState]
    EFS_VOLUME_STATE_MOUNTING: _ClassVar[EfsVolumeState]
    EFS_VOLUME_STATE_MOUNTED: _ClassVar[EfsVolumeState]
    EFS_VOLUME_STATE_DEGRADED: _ClassVar[EfsVolumeState]
    EFS_VOLUME_STATE_REMOUNTING: _ClassVar[EfsVolumeState]
    EFS_VOLUME_STATE_UNMOUNTED: _ClassVar[EfsVolumeState]
    EFS_VOLUME_STATE_FAILED: _ClassVar[EfsVolumeState]
EFS_VOLUME_STATE_UNSPECIFIED: EfsVolumeState
EFS_VOLUME_STATE_REQUESTED: EfsVolumeState
EFS_VOLUME_STATE_MOUNTING: EfsVolumeState
EFS_VOLUME_STATE_MOUNTED: EfsVolumeState
EFS_VOLUME_STATE_DEGRADED: EfsVolumeState
EFS_VOLUME_STATE_REMOUNTING: EfsVolumeState
EFS_VOLUME_STATE_UNMOUNTED: EfsVolumeState
EFS_VOLUME_STATE_FAILED: EfsVolumeState

class EfsVolumesConfig(_message.Message):
    __slots__ = ("mounts",)
    MOUNTS_FIELD_NUMBER: _ClassVar[int]
    mounts: _containers.RepeatedCompositeFieldContainer[EfsVolumeMount]
    def __init__(self, mounts: _Optional[_Iterable[_Union[EfsVolumeMount, _Mapping]]] = ...) -> None: ...

class EfsVolumeMount(_message.Message):
    __slots__ = ("mount_path", "file_system_id", "access_point_id", "read_only", "mount_target_ip")
    MOUNT_PATH_FIELD_NUMBER: _ClassVar[int]
    FILE_SYSTEM_ID_FIELD_NUMBER: _ClassVar[int]
    ACCESS_POINT_ID_FIELD_NUMBER: _ClassVar[int]
    READ_ONLY_FIELD_NUMBER: _ClassVar[int]
    MOUNT_TARGET_IP_FIELD_NUMBER: _ClassVar[int]
    mount_path: str
    file_system_id: str
    access_point_id: str
    read_only: bool
    mount_target_ip: str
    def __init__(self, mount_path: _Optional[str] = ..., file_system_id: _Optional[str] = ..., access_point_id: _Optional[str] = ..., read_only: _Optional[bool] = ..., mount_target_ip: _Optional[str] = ...) -> None: ...

class EfsVolumesStatus(_message.Message):
    __slots__ = ("volumes",)
    VOLUMES_FIELD_NUMBER: _ClassVar[int]
    volumes: _containers.RepeatedCompositeFieldContainer[EfsVolumeStatus]
    def __init__(self, volumes: _Optional[_Iterable[_Union[EfsVolumeStatus, _Mapping]]] = ...) -> None: ...

class EfsVolumeStatus(_message.Message):
    __slots__ = ("mount_path", "state", "last_error_class")
    MOUNT_PATH_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    LAST_ERROR_CLASS_FIELD_NUMBER: _ClassVar[int]
    mount_path: str
    state: EfsVolumeState
    last_error_class: str
    def __init__(self, mount_path: _Optional[str] = ..., state: _Optional[_Union[EfsVolumeState, str]] = ..., last_error_class: _Optional[str] = ...) -> None: ...
