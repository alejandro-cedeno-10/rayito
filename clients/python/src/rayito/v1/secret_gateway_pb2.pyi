from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SecretGatewayRouteState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SECRET_GATEWAY_ROUTE_STATE_UNSPECIFIED: _ClassVar[SecretGatewayRouteState]
    SECRET_GATEWAY_ROUTE_STATE_LISTENING: _ClassVar[SecretGatewayRouteState]
    SECRET_GATEWAY_ROUTE_STATE_FAILED: _ClassVar[SecretGatewayRouteState]
SECRET_GATEWAY_ROUTE_STATE_UNSPECIFIED: SecretGatewayRouteState
SECRET_GATEWAY_ROUTE_STATE_LISTENING: SecretGatewayRouteState
SECRET_GATEWAY_ROUTE_STATE_FAILED: SecretGatewayRouteState

class SecretGatewayConfig(_message.Message):
    __slots__ = ("routes",)
    ROUTES_FIELD_NUMBER: _ClassVar[int]
    routes: _containers.RepeatedCompositeFieldContainer[SecretGatewayRoute]
    def __init__(self, routes: _Optional[_Iterable[_Union[SecretGatewayRoute, _Mapping]]] = ...) -> None: ...

class SecretGatewayRoute(_message.Message):
    __slots__ = ("name", "upstream", "headers", "allow", "rate_per_minute")
    class HeadersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    NAME_FIELD_NUMBER: _ClassVar[int]
    UPSTREAM_FIELD_NUMBER: _ClassVar[int]
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    ALLOW_FIELD_NUMBER: _ClassVar[int]
    RATE_PER_MINUTE_FIELD_NUMBER: _ClassVar[int]
    name: str
    upstream: str
    headers: _containers.ScalarMap[str, str]
    allow: _containers.RepeatedCompositeFieldContainer[SecretGatewayAllowRule]
    rate_per_minute: int
    def __init__(self, name: _Optional[str] = ..., upstream: _Optional[str] = ..., headers: _Optional[_Mapping[str, str]] = ..., allow: _Optional[_Iterable[_Union[SecretGatewayAllowRule, _Mapping]]] = ..., rate_per_minute: _Optional[int] = ...) -> None: ...

class SecretGatewayAllowRule(_message.Message):
    __slots__ = ("method", "path")
    METHOD_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    method: str
    path: str
    def __init__(self, method: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class SecretGatewayStatus(_message.Message):
    __slots__ = ("routes",)
    ROUTES_FIELD_NUMBER: _ClassVar[int]
    routes: _containers.RepeatedCompositeFieldContainer[SecretGatewayRouteStatus]
    def __init__(self, routes: _Optional[_Iterable[_Union[SecretGatewayRouteStatus, _Mapping]]] = ...) -> None: ...

class SecretGatewayRouteStatus(_message.Message):
    __slots__ = ("name", "port", "state", "last_error_class")
    NAME_FIELD_NUMBER: _ClassVar[int]
    PORT_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    LAST_ERROR_CLASS_FIELD_NUMBER: _ClassVar[int]
    name: str
    port: int
    state: SecretGatewayRouteState
    last_error_class: str
    def __init__(self, name: _Optional[str] = ..., port: _Optional[int] = ..., state: _Optional[_Union[SecretGatewayRouteState, str]] = ..., last_error_class: _Optional[str] = ...) -> None: ...
