from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class EgressEnforcement(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EGRESS_ENFORCEMENT_UNSPECIFIED: _ClassVar[EgressEnforcement]
    EGRESS_ENFORCEMENT_NONE: _ClassVar[EgressEnforcement]
    EGRESS_ENFORCEMENT_GUEST_ROUTES: _ClassVar[EgressEnforcement]
    EGRESS_ENFORCEMENT_GUEST_ROUTES_AND_PROXY: _ClassVar[EgressEnforcement]
EGRESS_ENFORCEMENT_UNSPECIFIED: EgressEnforcement
EGRESS_ENFORCEMENT_NONE: EgressEnforcement
EGRESS_ENFORCEMENT_GUEST_ROUTES: EgressEnforcement
EGRESS_ENFORCEMENT_GUEST_ROUTES_AND_PROXY: EgressEnforcement

class EgressProxy(_message.Message):
    __slots__ = ("address", "username", "password")
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    USERNAME_FIELD_NUMBER: _ClassVar[int]
    PASSWORD_FIELD_NUMBER: _ClassVar[int]
    address: str
    username: str
    password: str
    def __init__(self, address: _Optional[str] = ..., username: _Optional[str] = ..., password: _Optional[str] = ...) -> None: ...

class NetworkPolicy(_message.Message):
    __slots__ = ("allow_out", "deny_out", "egress_proxy")
    ALLOW_OUT_FIELD_NUMBER: _ClassVar[int]
    DENY_OUT_FIELD_NUMBER: _ClassVar[int]
    EGRESS_PROXY_FIELD_NUMBER: _ClassVar[int]
    allow_out: _containers.RepeatedScalarFieldContainer[str]
    deny_out: _containers.RepeatedScalarFieldContainer[str]
    egress_proxy: EgressProxy
    def __init__(self, allow_out: _Optional[_Iterable[str]] = ..., deny_out: _Optional[_Iterable[str]] = ..., egress_proxy: _Optional[_Union[EgressProxy, _Mapping]] = ...) -> None: ...

class UpdateNetworkRequest(_message.Message):
    __slots__ = ("policy",)
    POLICY_FIELD_NUMBER: _ClassVar[int]
    policy: NetworkPolicy
    def __init__(self, policy: _Optional[_Union[NetworkPolicy, _Mapping]] = ...) -> None: ...

class GetNetworkRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class NetworkState(_message.Message):
    __slots__ = ("allow_out", "deny_out", "egress_proxy_configured", "enforcement", "local_proxy_port")
    ALLOW_OUT_FIELD_NUMBER: _ClassVar[int]
    DENY_OUT_FIELD_NUMBER: _ClassVar[int]
    EGRESS_PROXY_CONFIGURED_FIELD_NUMBER: _ClassVar[int]
    ENFORCEMENT_FIELD_NUMBER: _ClassVar[int]
    LOCAL_PROXY_PORT_FIELD_NUMBER: _ClassVar[int]
    allow_out: _containers.RepeatedScalarFieldContainer[str]
    deny_out: _containers.RepeatedScalarFieldContainer[str]
    egress_proxy_configured: bool
    enforcement: EgressEnforcement
    local_proxy_port: int
    def __init__(self, allow_out: _Optional[_Iterable[str]] = ..., deny_out: _Optional[_Iterable[str]] = ..., egress_proxy_configured: _Optional[bool] = ..., enforcement: _Optional[_Union[EgressEnforcement, str]] = ..., local_proxy_port: _Optional[int] = ...) -> None: ...
