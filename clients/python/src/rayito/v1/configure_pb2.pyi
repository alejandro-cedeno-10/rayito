from rayito.v1 import s3_mounts_pb2 as _s3_mounts_pb2
from rayito.v1 import efs_volumes_pb2 as _efs_volumes_pb2
from rayito.v1 import lifecycle_events_pb2 as _lifecycle_events_pb2
from rayito.v1 import telemetry_export_pb2 as _telemetry_export_pb2
from rayito.v1 import secret_gateway_pb2 as _secret_gateway_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ConfigSection(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CONFIG_SECTION_UNSPECIFIED: _ClassVar[ConfigSection]
    CONFIG_SECTION_S3_MOUNTS: _ClassVar[ConfigSection]
    CONFIG_SECTION_EFS_VOLUMES: _ClassVar[ConfigSection]
    CONFIG_SECTION_LIFECYCLE_EVENTS: _ClassVar[ConfigSection]
    CONFIG_SECTION_TELEMETRY_EXPORT: _ClassVar[ConfigSection]
    CONFIG_SECTION_SECRET_GATEWAY: _ClassVar[ConfigSection]

class SectionCode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SECTION_CODE_UNSPECIFIED: _ClassVar[SectionCode]
    SECTION_CODE_APPLIED: _ClassVar[SectionCode]
    SECTION_CODE_PENDING: _ClassVar[SectionCode]
    SECTION_CODE_UNSUPPORTED: _ClassVar[SectionCode]
    SECTION_CODE_INVALID: _ClassVar[SectionCode]
    SECTION_CODE_FAILED: _ClassVar[SectionCode]
CONFIG_SECTION_UNSPECIFIED: ConfigSection
CONFIG_SECTION_S3_MOUNTS: ConfigSection
CONFIG_SECTION_EFS_VOLUMES: ConfigSection
CONFIG_SECTION_LIFECYCLE_EVENTS: ConfigSection
CONFIG_SECTION_TELEMETRY_EXPORT: ConfigSection
CONFIG_SECTION_SECRET_GATEWAY: ConfigSection
SECTION_CODE_UNSPECIFIED: SectionCode
SECTION_CODE_APPLIED: SectionCode
SECTION_CODE_PENDING: SectionCode
SECTION_CODE_UNSUPPORTED: SectionCode
SECTION_CODE_INVALID: SectionCode
SECTION_CODE_FAILED: SectionCode

class ConfigureRequest(_message.Message):
    __slots__ = ("request_id", "s3_mounts", "efs_volumes", "lifecycle_events", "telemetry_export", "secret_gateway")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    S3_MOUNTS_FIELD_NUMBER: _ClassVar[int]
    EFS_VOLUMES_FIELD_NUMBER: _ClassVar[int]
    LIFECYCLE_EVENTS_FIELD_NUMBER: _ClassVar[int]
    TELEMETRY_EXPORT_FIELD_NUMBER: _ClassVar[int]
    SECRET_GATEWAY_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    s3_mounts: _s3_mounts_pb2.S3MountsConfig
    efs_volumes: _efs_volumes_pb2.EfsVolumesConfig
    lifecycle_events: _lifecycle_events_pb2.LifecycleEventsConfig
    telemetry_export: _telemetry_export_pb2.TelemetryExportConfig
    secret_gateway: _secret_gateway_pb2.SecretGatewayConfig
    def __init__(self, request_id: _Optional[str] = ..., s3_mounts: _Optional[_Union[_s3_mounts_pb2.S3MountsConfig, _Mapping]] = ..., efs_volumes: _Optional[_Union[_efs_volumes_pb2.EfsVolumesConfig, _Mapping]] = ..., lifecycle_events: _Optional[_Union[_lifecycle_events_pb2.LifecycleEventsConfig, _Mapping]] = ..., telemetry_export: _Optional[_Union[_telemetry_export_pb2.TelemetryExportConfig, _Mapping]] = ..., secret_gateway: _Optional[_Union[_secret_gateway_pb2.SecretGatewayConfig, _Mapping]] = ...) -> None: ...

class ConfigureResponse(_message.Message):
    __slots__ = ("results", "config_generation")
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    CONFIG_GENERATION_FIELD_NUMBER: _ClassVar[int]
    results: _containers.RepeatedCompositeFieldContainer[SectionResult]
    config_generation: int
    def __init__(self, results: _Optional[_Iterable[_Union[SectionResult, _Mapping]]] = ..., config_generation: _Optional[int] = ...) -> None: ...

class SectionResult(_message.Message):
    __slots__ = ("section", "code", "error_class")
    SECTION_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_CLASS_FIELD_NUMBER: _ClassVar[int]
    section: ConfigSection
    code: SectionCode
    error_class: str
    def __init__(self, section: _Optional[_Union[ConfigSection, str]] = ..., code: _Optional[_Union[SectionCode, str]] = ..., error_class: _Optional[str] = ...) -> None: ...

class ConfigureStatusRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ConfigureStatusResponse(_message.Message):
    __slots__ = ("s3_mounts", "efs_volumes", "lifecycle_events", "telemetry_export", "secret_gateway")
    S3_MOUNTS_FIELD_NUMBER: _ClassVar[int]
    EFS_VOLUMES_FIELD_NUMBER: _ClassVar[int]
    LIFECYCLE_EVENTS_FIELD_NUMBER: _ClassVar[int]
    TELEMETRY_EXPORT_FIELD_NUMBER: _ClassVar[int]
    SECRET_GATEWAY_FIELD_NUMBER: _ClassVar[int]
    s3_mounts: _s3_mounts_pb2.S3MountsStatus
    efs_volumes: _efs_volumes_pb2.EfsVolumesStatus
    lifecycle_events: _lifecycle_events_pb2.LifecycleEventsStatus
    telemetry_export: _telemetry_export_pb2.TelemetryExportStatus
    secret_gateway: _secret_gateway_pb2.SecretGatewayStatus
    def __init__(self, s3_mounts: _Optional[_Union[_s3_mounts_pb2.S3MountsStatus, _Mapping]] = ..., efs_volumes: _Optional[_Union[_efs_volumes_pb2.EfsVolumesStatus, _Mapping]] = ..., lifecycle_events: _Optional[_Union[_lifecycle_events_pb2.LifecycleEventsStatus, _Mapping]] = ..., telemetry_export: _Optional[_Union[_telemetry_export_pb2.TelemetryExportStatus, _Mapping]] = ..., secret_gateway: _Optional[_Union[_secret_gateway_pb2.SecretGatewayStatus, _Mapping]] = ...) -> None: ...
