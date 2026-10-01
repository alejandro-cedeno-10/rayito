from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RootEgressClass(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ROOT_EGRESS_CLASS_UNSPECIFIED: _ClassVar[RootEgressClass]
    ROOT_EGRESS_CLASS_S3: _ClassVar[RootEgressClass]
    ROOT_EGRESS_CLASS_CLOUDWATCH_OTLP: _ClassVar[RootEgressClass]
    ROOT_EGRESS_CLASS_SECRET_GATEWAY_UPSTREAM: _ClassVar[RootEgressClass]
    ROOT_EGRESS_CLASS_EFS: _ClassVar[RootEgressClass]
ROOT_EGRESS_CLASS_UNSPECIFIED: RootEgressClass
ROOT_EGRESS_CLASS_S3: RootEgressClass
ROOT_EGRESS_CLASS_CLOUDWATCH_OTLP: RootEgressClass
ROOT_EGRESS_CLASS_SECRET_GATEWAY_UPSTREAM: RootEgressClass
ROOT_EGRESS_CLASS_EFS: RootEgressClass

class AgentFeatures(_message.Message):
    __slots__ = ("configure", "s3_mounts", "efs_volumes", "lifecycle_events", "telemetry_export", "secret_gateway", "template_start", "root_egress")
    CONFIGURE_FIELD_NUMBER: _ClassVar[int]
    S3_MOUNTS_FIELD_NUMBER: _ClassVar[int]
    EFS_VOLUMES_FIELD_NUMBER: _ClassVar[int]
    LIFECYCLE_EVENTS_FIELD_NUMBER: _ClassVar[int]
    TELEMETRY_EXPORT_FIELD_NUMBER: _ClassVar[int]
    SECRET_GATEWAY_FIELD_NUMBER: _ClassVar[int]
    TEMPLATE_START_FIELD_NUMBER: _ClassVar[int]
    ROOT_EGRESS_FIELD_NUMBER: _ClassVar[int]
    configure: bool
    s3_mounts: bool
    efs_volumes: bool
    lifecycle_events: bool
    telemetry_export: bool
    secret_gateway: bool
    template_start: bool
    root_egress: _containers.RepeatedScalarFieldContainer[RootEgressClass]
    def __init__(self, configure: _Optional[bool] = ..., s3_mounts: _Optional[bool] = ..., efs_volumes: _Optional[bool] = ..., lifecycle_events: _Optional[bool] = ..., telemetry_export: _Optional[bool] = ..., secret_gateway: _Optional[bool] = ..., template_start: _Optional[bool] = ..., root_egress: _Optional[_Iterable[_Union[RootEgressClass, str]]] = ...) -> None: ...
