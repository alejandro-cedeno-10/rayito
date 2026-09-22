from rayito.v1 import common_pb2 as _common_pb2
from rayito.v1 import process_pb2 as _process_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PtySize(_message.Message):
    __slots__ = ("cols", "rows")
    COLS_FIELD_NUMBER: _ClassVar[int]
    ROWS_FIELD_NUMBER: _ClassVar[int]
    cols: int
    rows: int
    def __init__(self, cols: _Optional[int] = ..., rows: _Optional[int] = ...) -> None: ...

class PtyStart(_message.Message):
    __slots__ = ("size", "envs", "cwd", "user", "shell", "timeout_ms")
    class EnvsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    SIZE_FIELD_NUMBER: _ClassVar[int]
    ENVS_FIELD_NUMBER: _ClassVar[int]
    CWD_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    SHELL_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    size: PtySize
    envs: _containers.ScalarMap[str, str]
    cwd: str
    user: _common_pb2.User
    shell: str
    timeout_ms: int
    def __init__(self, size: _Optional[_Union[PtySize, _Mapping]] = ..., envs: _Optional[_Mapping[str, str]] = ..., cwd: _Optional[str] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ..., shell: _Optional[str] = ..., timeout_ms: _Optional[int] = ...) -> None: ...

class PtyServerMessage(_message.Message):
    __slots__ = ("started", "data", "exited", "keepalive", "seq")
    STARTED_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    EXITED_FIELD_NUMBER: _ClassVar[int]
    KEEPALIVE_FIELD_NUMBER: _ClassVar[int]
    SEQ_FIELD_NUMBER: _ClassVar[int]
    started: PtyStarted
    data: bytes
    exited: PtyExited
    keepalive: _common_pb2.KeepAlive
    seq: int
    def __init__(self, started: _Optional[_Union[PtyStarted, _Mapping]] = ..., data: _Optional[bytes] = ..., exited: _Optional[_Union[PtyExited, _Mapping]] = ..., keepalive: _Optional[_Union[_common_pb2.KeepAlive, _Mapping]] = ..., seq: _Optional[int] = ...) -> None: ...

class PtyStarted(_message.Message):
    __slots__ = ("pid",)
    PID_FIELD_NUMBER: _ClassVar[int]
    pid: int
    def __init__(self, pid: _Optional[int] = ...) -> None: ...

class PtyExited(_message.Message):
    __slots__ = ("exit_code", "exited", "status", "error", "signal")
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    EXITED_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    exit_code: int
    exited: bool
    status: str
    error: _common_pb2.StreamError
    signal: int
    def __init__(self, exit_code: _Optional[int] = ..., exited: _Optional[bool] = ..., status: _Optional[str] = ..., error: _Optional[_Union[_common_pb2.StreamError, _Mapping]] = ..., signal: _Optional[int] = ...) -> None: ...

class ResizeRequest(_message.Message):
    __slots__ = ("pid", "size")
    PID_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    pid: int
    size: PtySize
    def __init__(self, pid: _Optional[int] = ..., size: _Optional[_Union[PtySize, _Mapping]] = ...) -> None: ...

class ResizeResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class KillPtyRequest(_message.Message):
    __slots__ = ("pid",)
    PID_FIELD_NUMBER: _ClassVar[int]
    pid: int
    def __init__(self, pid: _Optional[int] = ...) -> None: ...

class KillPtyResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
