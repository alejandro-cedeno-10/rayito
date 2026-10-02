from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TelemetryExportNameStyle(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TELEMETRY_EXPORT_NAME_STYLE_UNSPECIFIED: _ClassVar[TelemetryExportNameStyle]
    TELEMETRY_EXPORT_NAME_STYLE_RAYITO: _ClassVar[TelemetryExportNameStyle]
    TELEMETRY_EXPORT_NAME_STYLE_E2B: _ClassVar[TelemetryExportNameStyle]
TELEMETRY_EXPORT_NAME_STYLE_UNSPECIFIED: TelemetryExportNameStyle
TELEMETRY_EXPORT_NAME_STYLE_RAYITO: TelemetryExportNameStyle
TELEMETRY_EXPORT_NAME_STYLE_E2B: TelemetryExportNameStyle

class TelemetryExportConfig(_message.Message):
    __slots__ = ("interval_s", "service_name", "names", "image_arn", "image_version", "image_memory_mib", "execution_role", "bearer")
    INTERVAL_S_FIELD_NUMBER: _ClassVar[int]
    SERVICE_NAME_FIELD_NUMBER: _ClassVar[int]
    NAMES_FIELD_NUMBER: _ClassVar[int]
    IMAGE_ARN_FIELD_NUMBER: _ClassVar[int]
    IMAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    IMAGE_MEMORY_MIB_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_ROLE_FIELD_NUMBER: _ClassVar[int]
    BEARER_FIELD_NUMBER: _ClassVar[int]
    interval_s: int
    service_name: str
    names: TelemetryExportNameStyle
    image_arn: str
    image_version: str
    image_memory_mib: int
    execution_role: TelemetryExportExecutionRoleAuth
    bearer: TelemetryExportBearerAuth
    def __init__(self, interval_s: _Optional[int] = ..., service_name: _Optional[str] = ..., names: _Optional[_Union[TelemetryExportNameStyle, str]] = ..., image_arn: _Optional[str] = ..., image_version: _Optional[str] = ..., image_memory_mib: _Optional[int] = ..., execution_role: _Optional[_Union[TelemetryExportExecutionRoleAuth, _Mapping]] = ..., bearer: _Optional[_Union[TelemetryExportBearerAuth, _Mapping]] = ...) -> None: ...

class TelemetryExportExecutionRoleAuth(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class TelemetryExportBearerAuth(_message.Message):
    __slots__ = ("token",)
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    token: str
    def __init__(self, token: _Optional[str] = ...) -> None: ...

class TelemetryExportStatus(_message.Message):
    __slots__ = ("exported", "dropped", "last_error_class")
    EXPORTED_FIELD_NUMBER: _ClassVar[int]
    DROPPED_FIELD_NUMBER: _ClassVar[int]
    LAST_ERROR_CLASS_FIELD_NUMBER: _ClassVar[int]
    exported: int
    dropped: int
    last_error_class: str
    def __init__(self, exported: _Optional[int] = ..., dropped: _Optional[int] = ..., last_error_class: _Optional[str] = ...) -> None: ...
