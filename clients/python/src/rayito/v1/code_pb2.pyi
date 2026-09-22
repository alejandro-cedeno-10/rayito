from rayito.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CreateContextRequest(_message.Message):
    __slots__ = ("language", "cwd", "envs")
    class EnvsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    CWD_FIELD_NUMBER: _ClassVar[int]
    ENVS_FIELD_NUMBER: _ClassVar[int]
    language: str
    cwd: str
    envs: _containers.ScalarMap[str, str]
    def __init__(self, language: _Optional[str] = ..., cwd: _Optional[str] = ..., envs: _Optional[_Mapping[str, str]] = ...) -> None: ...

class CreateContextResponse(_message.Message):
    __slots__ = ("context_id",)
    CONTEXT_ID_FIELD_NUMBER: _ClassVar[int]
    context_id: str
    def __init__(self, context_id: _Optional[str] = ...) -> None: ...

class ExecuteRequest(_message.Message):
    __slots__ = ("context_id", "code", "timeout_ms", "envs", "language")
    class EnvsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    CONTEXT_ID_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    ENVS_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    context_id: str
    code: str
    timeout_ms: int
    envs: _containers.ScalarMap[str, str]
    language: str
    def __init__(self, context_id: _Optional[str] = ..., code: _Optional[str] = ..., timeout_ms: _Optional[int] = ..., envs: _Optional[_Mapping[str, str]] = ..., language: _Optional[str] = ...) -> None: ...

class ReattachRequest(_message.Message):
    __slots__ = ("context_id", "execution_id", "from_seq")
    CONTEXT_ID_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    FROM_SEQ_FIELD_NUMBER: _ClassVar[int]
    context_id: str
    execution_id: str
    from_seq: int
    def __init__(self, context_id: _Optional[str] = ..., execution_id: _Optional[str] = ..., from_seq: _Optional[int] = ...) -> None: ...

class ExecuteEvent(_message.Message):
    __slots__ = ("stdout", "stderr", "result", "error", "end", "started", "keepalive", "seq")
    STDOUT_FIELD_NUMBER: _ClassVar[int]
    STDERR_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    STARTED_FIELD_NUMBER: _ClassVar[int]
    KEEPALIVE_FIELD_NUMBER: _ClassVar[int]
    SEQ_FIELD_NUMBER: _ClassVar[int]
    stdout: OutputChunk
    stderr: OutputChunk
    result: ExecutionResult
    error: ExecutionError
    end: ExecutionEnd
    started: ExecutionStarted
    keepalive: _common_pb2.KeepAlive
    seq: int
    def __init__(self, stdout: _Optional[_Union[OutputChunk, _Mapping]] = ..., stderr: _Optional[_Union[OutputChunk, _Mapping]] = ..., result: _Optional[_Union[ExecutionResult, _Mapping]] = ..., error: _Optional[_Union[ExecutionError, _Mapping]] = ..., end: _Optional[_Union[ExecutionEnd, _Mapping]] = ..., started: _Optional[_Union[ExecutionStarted, _Mapping]] = ..., keepalive: _Optional[_Union[_common_pb2.KeepAlive, _Mapping]] = ..., seq: _Optional[int] = ...) -> None: ...

class ExecutionStarted(_message.Message):
    __slots__ = ("execution_id", "execution_count")
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_COUNT_FIELD_NUMBER: _ClassVar[int]
    execution_id: str
    execution_count: int
    def __init__(self, execution_id: _Optional[str] = ..., execution_count: _Optional[int] = ...) -> None: ...

class OutputChunk(_message.Message):
    __slots__ = ("text", "timestamp_unix_ns")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_UNIX_NS_FIELD_NUMBER: _ClassVar[int]
    text: str
    timestamp_unix_ns: int
    def __init__(self, text: _Optional[str] = ..., timestamp_unix_ns: _Optional[int] = ...) -> None: ...

class ExecutionResult(_message.Message):
    __slots__ = ("is_main_result", "text", "html", "markdown", "latex", "json", "javascript", "png", "jpeg", "svg", "pdf", "chart", "data", "extra")
    class ExtraEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    IS_MAIN_RESULT_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    HTML_FIELD_NUMBER: _ClassVar[int]
    MARKDOWN_FIELD_NUMBER: _ClassVar[int]
    LATEX_FIELD_NUMBER: _ClassVar[int]
    JSON_FIELD_NUMBER: _ClassVar[int]
    JAVASCRIPT_FIELD_NUMBER: _ClassVar[int]
    PNG_FIELD_NUMBER: _ClassVar[int]
    JPEG_FIELD_NUMBER: _ClassVar[int]
    SVG_FIELD_NUMBER: _ClassVar[int]
    PDF_FIELD_NUMBER: _ClassVar[int]
    CHART_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    EXTRA_FIELD_NUMBER: _ClassVar[int]
    is_main_result: bool
    text: str
    html: str
    markdown: str
    latex: str
    json: str
    javascript: str
    png: str
    jpeg: str
    svg: str
    pdf: str
    chart: str
    data: str
    extra: _containers.ScalarMap[str, str]
    def __init__(self, is_main_result: _Optional[bool] = ..., text: _Optional[str] = ..., html: _Optional[str] = ..., markdown: _Optional[str] = ..., latex: _Optional[str] = ..., json: _Optional[str] = ..., javascript: _Optional[str] = ..., png: _Optional[str] = ..., jpeg: _Optional[str] = ..., svg: _Optional[str] = ..., pdf: _Optional[str] = ..., chart: _Optional[str] = ..., data: _Optional[str] = ..., extra: _Optional[_Mapping[str, str]] = ...) -> None: ...

class ExecutionError(_message.Message):
    __slots__ = ("name", "value", "traceback")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    TRACEBACK_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: str
    traceback: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, name: _Optional[str] = ..., value: _Optional[str] = ..., traceback: _Optional[_Iterable[str]] = ...) -> None: ...

class ExecutionEnd(_message.Message):
    __slots__ = ("execution_count",)
    EXECUTION_COUNT_FIELD_NUMBER: _ClassVar[int]
    execution_count: int
    def __init__(self, execution_count: _Optional[int] = ...) -> None: ...

class ListContextsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ContextInfo(_message.Message):
    __slots__ = ("context_id", "language", "cwd")
    CONTEXT_ID_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    CWD_FIELD_NUMBER: _ClassVar[int]
    context_id: str
    language: str
    cwd: str
    def __init__(self, context_id: _Optional[str] = ..., language: _Optional[str] = ..., cwd: _Optional[str] = ...) -> None: ...

class ListContextsResponse(_message.Message):
    __slots__ = ("contexts",)
    CONTEXTS_FIELD_NUMBER: _ClassVar[int]
    contexts: _containers.RepeatedCompositeFieldContainer[ContextInfo]
    def __init__(self, contexts: _Optional[_Iterable[_Union[ContextInfo, _Mapping]]] = ...) -> None: ...

class DestroyContextRequest(_message.Message):
    __slots__ = ("context_id",)
    CONTEXT_ID_FIELD_NUMBER: _ClassVar[int]
    context_id: str
    def __init__(self, context_id: _Optional[str] = ...) -> None: ...

class DestroyContextResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class RestartContextRequest(_message.Message):
    __slots__ = ("context_id",)
    CONTEXT_ID_FIELD_NUMBER: _ClassVar[int]
    context_id: str
    def __init__(self, context_id: _Optional[str] = ...) -> None: ...

class RestartContextResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
