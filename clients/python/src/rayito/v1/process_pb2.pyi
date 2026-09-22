from rayito.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ProcessKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PROCESS_KIND_UNSPECIFIED: _ClassVar[ProcessKind]
    PROCESS_KIND_PROCESS: _ClassVar[ProcessKind]
    PROCESS_KIND_PTY: _ClassVar[ProcessKind]
PROCESS_KIND_UNSPECIFIED: ProcessKind
PROCESS_KIND_PROCESS: ProcessKind
PROCESS_KIND_PTY: ProcessKind

class ProcessConfig(_message.Message):
    __slots__ = ("cmd", "args", "envs", "cwd")
    class EnvsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    CMD_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    ENVS_FIELD_NUMBER: _ClassVar[int]
    CWD_FIELD_NUMBER: _ClassVar[int]
    cmd: str
    args: _containers.RepeatedScalarFieldContainer[str]
    envs: _containers.ScalarMap[str, str]
    cwd: str
    def __init__(self, cmd: _Optional[str] = ..., args: _Optional[_Iterable[str]] = ..., envs: _Optional[_Mapping[str, str]] = ..., cwd: _Optional[str] = ...) -> None: ...

class StartRequest(_message.Message):
    __slots__ = ("process", "user", "timeout_ms", "stdin", "tag")
    PROCESS_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    STDIN_FIELD_NUMBER: _ClassVar[int]
    TAG_FIELD_NUMBER: _ClassVar[int]
    process: ProcessConfig
    user: _common_pb2.User
    timeout_ms: int
    stdin: bool
    tag: str
    def __init__(self, process: _Optional[_Union[ProcessConfig, _Mapping]] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ..., timeout_ms: _Optional[int] = ..., stdin: _Optional[bool] = ..., tag: _Optional[str] = ...) -> None: ...

class ConnectRequest(_message.Message):
    __slots__ = ("pid", "from_seq")
    PID_FIELD_NUMBER: _ClassVar[int]
    FROM_SEQ_FIELD_NUMBER: _ClassVar[int]
    pid: int
    from_seq: int
    def __init__(self, pid: _Optional[int] = ..., from_seq: _Optional[int] = ...) -> None: ...

class ProcessEvent(_message.Message):
    __slots__ = ("start", "data", "end", "keepalive")
    START_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    KEEPALIVE_FIELD_NUMBER: _ClassVar[int]
    start: StartEvent
    data: DataEvent
    end: EndEvent
    keepalive: _common_pb2.KeepAlive
    def __init__(self, start: _Optional[_Union[StartEvent, _Mapping]] = ..., data: _Optional[_Union[DataEvent, _Mapping]] = ..., end: _Optional[_Union[EndEvent, _Mapping]] = ..., keepalive: _Optional[_Union[_common_pb2.KeepAlive, _Mapping]] = ...) -> None: ...

class StartEvent(_message.Message):
    __slots__ = ("pid",)
    PID_FIELD_NUMBER: _ClassVar[int]
    pid: int
    def __init__(self, pid: _Optional[int] = ...) -> None: ...

class DataEvent(_message.Message):
    __slots__ = ("stdout", "stderr", "seq")
    STDOUT_FIELD_NUMBER: _ClassVar[int]
    STDERR_FIELD_NUMBER: _ClassVar[int]
    SEQ_FIELD_NUMBER: _ClassVar[int]
    stdout: bytes
    stderr: bytes
    seq: int
    def __init__(self, stdout: _Optional[bytes] = ..., stderr: _Optional[bytes] = ..., seq: _Optional[int] = ...) -> None: ...

class EndEvent(_message.Message):
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

class SendInputRequest(_message.Message):
    __slots__ = ("pid", "data")
    PID_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    pid: int
    data: bytes
    def __init__(self, pid: _Optional[int] = ..., data: _Optional[bytes] = ...) -> None: ...

class SendInputResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class CloseStdinRequest(_message.Message):
    __slots__ = ("pid",)
    PID_FIELD_NUMBER: _ClassVar[int]
    pid: int
    def __init__(self, pid: _Optional[int] = ...) -> None: ...

class CloseStdinResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class SendSignalRequest(_message.Message):
    __slots__ = ("pid", "signal")
    PID_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    pid: int
    signal: int
    def __init__(self, pid: _Optional[int] = ..., signal: _Optional[int] = ...) -> None: ...

class SendSignalResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ProcessInfo(_message.Message):
    __slots__ = ("pid", "config", "tag", "kind")
    PID_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    TAG_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    pid: int
    config: ProcessConfig
    tag: str
    kind: ProcessKind
    def __init__(self, pid: _Optional[int] = ..., config: _Optional[_Union[ProcessConfig, _Mapping]] = ..., tag: _Optional[str] = ..., kind: _Optional[_Union[ProcessKind, str]] = ...) -> None: ...

class ListResponse(_message.Message):
    __slots__ = ("processes",)
    PROCESSES_FIELD_NUMBER: _ClassVar[int]
    processes: _containers.RepeatedCompositeFieldContainer[ProcessInfo]
    def __init__(self, processes: _Optional[_Iterable[_Union[ProcessInfo, _Mapping]]] = ...) -> None: ...
