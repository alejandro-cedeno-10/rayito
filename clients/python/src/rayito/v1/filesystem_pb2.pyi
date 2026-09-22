from rayito.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FilesystemEventType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FILESYSTEM_EVENT_TYPE_UNSPECIFIED: _ClassVar[FilesystemEventType]
    FILESYSTEM_EVENT_TYPE_CREATE: _ClassVar[FilesystemEventType]
    FILESYSTEM_EVENT_TYPE_WRITE: _ClassVar[FilesystemEventType]
    FILESYSTEM_EVENT_TYPE_REMOVE: _ClassVar[FilesystemEventType]
    FILESYSTEM_EVENT_TYPE_RENAME: _ClassVar[FilesystemEventType]
    FILESYSTEM_EVENT_TYPE_CHMOD: _ClassVar[FilesystemEventType]
FILESYSTEM_EVENT_TYPE_UNSPECIFIED: FilesystemEventType
FILESYSTEM_EVENT_TYPE_CREATE: FilesystemEventType
FILESYSTEM_EVENT_TYPE_WRITE: FilesystemEventType
FILESYSTEM_EVENT_TYPE_REMOVE: FilesystemEventType
FILESYSTEM_EVENT_TYPE_RENAME: FilesystemEventType
FILESYSTEM_EVENT_TYPE_CHMOD: FilesystemEventType

class ReadRequest(_message.Message):
    __slots__ = ("path", "user")
    PATH_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    path: str
    user: _common_pb2.User
    def __init__(self, path: _Optional[str] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ...) -> None: ...

class ReadResponse(_message.Message):
    __slots__ = ("chunk",)
    CHUNK_FIELD_NUMBER: _ClassVar[int]
    chunk: bytes
    def __init__(self, chunk: _Optional[bytes] = ...) -> None: ...

class WriteRequest(_message.Message):
    __slots__ = ("path", "user", "mode", "chunk")
    PATH_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    CHUNK_FIELD_NUMBER: _ClassVar[int]
    path: str
    user: _common_pb2.User
    mode: int
    chunk: bytes
    def __init__(self, path: _Optional[str] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ..., mode: _Optional[int] = ..., chunk: _Optional[bytes] = ...) -> None: ...

class WriteResponse(_message.Message):
    __slots__ = ("entries",)
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[_common_pb2.EntryInfo]
    def __init__(self, entries: _Optional[_Iterable[_Union[_common_pb2.EntryInfo, _Mapping]]] = ...) -> None: ...

class StatRequest(_message.Message):
    __slots__ = ("path", "user")
    PATH_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    path: str
    user: _common_pb2.User
    def __init__(self, path: _Optional[str] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ...) -> None: ...

class StatResponse(_message.Message):
    __slots__ = ("entry",)
    ENTRY_FIELD_NUMBER: _ClassVar[int]
    entry: _common_pb2.EntryInfo
    def __init__(self, entry: _Optional[_Union[_common_pb2.EntryInfo, _Mapping]] = ...) -> None: ...

class ListDirRequest(_message.Message):
    __slots__ = ("path", "depth", "user")
    PATH_FIELD_NUMBER: _ClassVar[int]
    DEPTH_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    path: str
    depth: int
    user: _common_pb2.User
    def __init__(self, path: _Optional[str] = ..., depth: _Optional[int] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ...) -> None: ...

class ListDirResponse(_message.Message):
    __slots__ = ("entries",)
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[_common_pb2.EntryInfo]
    def __init__(self, entries: _Optional[_Iterable[_Union[_common_pb2.EntryInfo, _Mapping]]] = ...) -> None: ...

class MakeDirRequest(_message.Message):
    __slots__ = ("path", "user")
    PATH_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    path: str
    user: _common_pb2.User
    def __init__(self, path: _Optional[str] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ...) -> None: ...

class MakeDirResponse(_message.Message):
    __slots__ = ("entry",)
    ENTRY_FIELD_NUMBER: _ClassVar[int]
    entry: _common_pb2.EntryInfo
    def __init__(self, entry: _Optional[_Union[_common_pb2.EntryInfo, _Mapping]] = ...) -> None: ...

class MoveRequest(_message.Message):
    __slots__ = ("source", "destination", "user")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    DESTINATION_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    source: str
    destination: str
    user: _common_pb2.User
    def __init__(self, source: _Optional[str] = ..., destination: _Optional[str] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ...) -> None: ...

class MoveResponse(_message.Message):
    __slots__ = ("entry",)
    ENTRY_FIELD_NUMBER: _ClassVar[int]
    entry: _common_pb2.EntryInfo
    def __init__(self, entry: _Optional[_Union[_common_pb2.EntryInfo, _Mapping]] = ...) -> None: ...

class RemoveRequest(_message.Message):
    __slots__ = ("path", "recursive", "user")
    PATH_FIELD_NUMBER: _ClassVar[int]
    RECURSIVE_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    path: str
    recursive: bool
    user: _common_pb2.User
    def __init__(self, path: _Optional[str] = ..., recursive: _Optional[bool] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ...) -> None: ...

class RemoveResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class WatchDirRequest(_message.Message):
    __slots__ = ("path", "recursive", "user", "include_entry")
    PATH_FIELD_NUMBER: _ClassVar[int]
    RECURSIVE_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_ENTRY_FIELD_NUMBER: _ClassVar[int]
    path: str
    recursive: bool
    user: _common_pb2.User
    include_entry: bool
    def __init__(self, path: _Optional[str] = ..., recursive: _Optional[bool] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ..., include_entry: _Optional[bool] = ...) -> None: ...

class WatchDirResponse(_message.Message):
    __slots__ = ("started", "filesystem", "keepalive")
    STARTED_FIELD_NUMBER: _ClassVar[int]
    FILESYSTEM_FIELD_NUMBER: _ClassVar[int]
    KEEPALIVE_FIELD_NUMBER: _ClassVar[int]
    started: WatchStarted
    filesystem: FilesystemEvent
    keepalive: _common_pb2.KeepAlive
    def __init__(self, started: _Optional[_Union[WatchStarted, _Mapping]] = ..., filesystem: _Optional[_Union[FilesystemEvent, _Mapping]] = ..., keepalive: _Optional[_Union[_common_pb2.KeepAlive, _Mapping]] = ...) -> None: ...

class WatchStarted(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class FilesystemEvent(_message.Message):
    __slots__ = ("name", "type", "entry")
    NAME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    ENTRY_FIELD_NUMBER: _ClassVar[int]
    name: str
    type: FilesystemEventType
    entry: _common_pb2.EntryInfo
    def __init__(self, name: _Optional[str] = ..., type: _Optional[_Union[FilesystemEventType, str]] = ..., entry: _Optional[_Union[_common_pb2.EntryInfo, _Mapping]] = ...) -> None: ...

class S3Location(_message.Message):
    __slots__ = ("bucket", "key_prefix", "region")
    BUCKET_FIELD_NUMBER: _ClassVar[int]
    KEY_PREFIX_FIELD_NUMBER: _ClassVar[int]
    REGION_FIELD_NUMBER: _ClassVar[int]
    bucket: str
    key_prefix: str
    region: str
    def __init__(self, bucket: _Optional[str] = ..., key_prefix: _Optional[str] = ..., region: _Optional[str] = ...) -> None: ...

class CheckpointRequest(_message.Message):
    __slots__ = ("target", "user", "exclude")
    TARGET_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    EXCLUDE_FIELD_NUMBER: _ClassVar[int]
    target: S3Location
    user: _common_pb2.User
    exclude: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, target: _Optional[_Union[S3Location, _Mapping]] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ..., exclude: _Optional[_Iterable[str]] = ...) -> None: ...

class CheckpointEvent(_message.Message):
    __slots__ = ("started", "progress", "done", "error", "keepalive")
    STARTED_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    DONE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    KEEPALIVE_FIELD_NUMBER: _ClassVar[int]
    started: CheckpointStarted
    progress: CheckpointProgress
    done: CheckpointDone
    error: _common_pb2.StreamError
    keepalive: _common_pb2.KeepAlive
    def __init__(self, started: _Optional[_Union[CheckpointStarted, _Mapping]] = ..., progress: _Optional[_Union[CheckpointProgress, _Mapping]] = ..., done: _Optional[_Union[CheckpointDone, _Mapping]] = ..., error: _Optional[_Union[_common_pb2.StreamError, _Mapping]] = ..., keepalive: _Optional[_Union[_common_pb2.KeepAlive, _Mapping]] = ...) -> None: ...

class CheckpointStarted(_message.Message):
    __slots__ = ("files", "bytes")
    FILES_FIELD_NUMBER: _ClassVar[int]
    BYTES_FIELD_NUMBER: _ClassVar[int]
    files: int
    bytes: int
    def __init__(self, files: _Optional[int] = ..., bytes: _Optional[int] = ...) -> None: ...

class CheckpointProgress(_message.Message):
    __slots__ = ("files_done", "bytes_read", "bytes_uploaded")
    FILES_DONE_FIELD_NUMBER: _ClassVar[int]
    BYTES_READ_FIELD_NUMBER: _ClassVar[int]
    BYTES_UPLOADED_FIELD_NUMBER: _ClassVar[int]
    files_done: int
    bytes_read: int
    bytes_uploaded: int
    def __init__(self, files_done: _Optional[int] = ..., bytes_read: _Optional[int] = ..., bytes_uploaded: _Optional[int] = ...) -> None: ...

class CheckpointDone(_message.Message):
    __slots__ = ("files", "bytes_read", "archive_bytes", "sha256", "skipped", "duration_ms")
    FILES_FIELD_NUMBER: _ClassVar[int]
    BYTES_READ_FIELD_NUMBER: _ClassVar[int]
    ARCHIVE_BYTES_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    SKIPPED_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    files: int
    bytes_read: int
    archive_bytes: int
    sha256: str
    skipped: int
    duration_ms: int
    def __init__(self, files: _Optional[int] = ..., bytes_read: _Optional[int] = ..., archive_bytes: _Optional[int] = ..., sha256: _Optional[str] = ..., skipped: _Optional[int] = ..., duration_ms: _Optional[int] = ...) -> None: ...

class RestoreRequest(_message.Message):
    __slots__ = ("source", "user")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    source: S3Location
    user: _common_pb2.User
    def __init__(self, source: _Optional[_Union[S3Location, _Mapping]] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ...) -> None: ...

class RestoreEvent(_message.Message):
    __slots__ = ("started", "progress", "done", "error", "keepalive")
    STARTED_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    DONE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    KEEPALIVE_FIELD_NUMBER: _ClassVar[int]
    started: RestoreStarted
    progress: RestoreProgress
    done: RestoreDone
    error: _common_pb2.StreamError
    keepalive: _common_pb2.KeepAlive
    def __init__(self, started: _Optional[_Union[RestoreStarted, _Mapping]] = ..., progress: _Optional[_Union[RestoreProgress, _Mapping]] = ..., done: _Optional[_Union[RestoreDone, _Mapping]] = ..., error: _Optional[_Union[_common_pb2.StreamError, _Mapping]] = ..., keepalive: _Optional[_Union[_common_pb2.KeepAlive, _Mapping]] = ...) -> None: ...

class RestoreStarted(_message.Message):
    __slots__ = ("archive_bytes", "files")
    ARCHIVE_BYTES_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    archive_bytes: int
    files: int
    def __init__(self, archive_bytes: _Optional[int] = ..., files: _Optional[int] = ...) -> None: ...

class RestoreProgress(_message.Message):
    __slots__ = ("files_done", "bytes_downloaded")
    FILES_DONE_FIELD_NUMBER: _ClassVar[int]
    BYTES_DOWNLOADED_FIELD_NUMBER: _ClassVar[int]
    files_done: int
    bytes_downloaded: int
    def __init__(self, files_done: _Optional[int] = ..., bytes_downloaded: _Optional[int] = ...) -> None: ...

class RestoreDone(_message.Message):
    __slots__ = ("files", "bytes_written", "archive_bytes", "sha256", "skipped", "duration_ms")
    FILES_FIELD_NUMBER: _ClassVar[int]
    BYTES_WRITTEN_FIELD_NUMBER: _ClassVar[int]
    ARCHIVE_BYTES_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    SKIPPED_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    files: int
    bytes_written: int
    archive_bytes: int
    sha256: str
    skipped: int
    duration_ms: int
    def __init__(self, files: _Optional[int] = ..., bytes_written: _Optional[int] = ..., archive_bytes: _Optional[int] = ..., sha256: _Optional[str] = ..., skipped: _Optional[int] = ..., duration_ms: _Optional[int] = ...) -> None: ...
