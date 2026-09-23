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

class TransferDirection(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TRANSFER_DIRECTION_UNSPECIFIED: _ClassVar[TransferDirection]
    TRANSFER_DIRECTION_IMPORT: _ClassVar[TransferDirection]
    TRANSFER_DIRECTION_EXPORT: _ClassVar[TransferDirection]

class TransferPhase(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TRANSFER_PHASE_UNSPECIFIED: _ClassVar[TransferPhase]
    TRANSFER_PHASE_WAITING: _ClassVar[TransferPhase]
    TRANSFER_PHASE_RUNNING: _ClassVar[TransferPhase]
    TRANSFER_PHASE_DONE: _ClassVar[TransferPhase]
    TRANSFER_PHASE_FAILED: _ClassVar[TransferPhase]
    TRANSFER_PHASE_CANCELLED: _ClassVar[TransferPhase]
FILESYSTEM_EVENT_TYPE_UNSPECIFIED: FilesystemEventType
FILESYSTEM_EVENT_TYPE_CREATE: FilesystemEventType
FILESYSTEM_EVENT_TYPE_WRITE: FilesystemEventType
FILESYSTEM_EVENT_TYPE_REMOVE: FilesystemEventType
FILESYSTEM_EVENT_TYPE_RENAME: FilesystemEventType
FILESYSTEM_EVENT_TYPE_CHMOD: FilesystemEventType
TRANSFER_DIRECTION_UNSPECIFIED: TransferDirection
TRANSFER_DIRECTION_IMPORT: TransferDirection
TRANSFER_DIRECTION_EXPORT: TransferDirection
TRANSFER_PHASE_UNSPECIFIED: TransferPhase
TRANSFER_PHASE_WAITING: TransferPhase
TRANSFER_PHASE_RUNNING: TransferPhase
TRANSFER_PHASE_DONE: TransferPhase
TRANSFER_PHASE_FAILED: TransferPhase
TRANSFER_PHASE_CANCELLED: TransferPhase

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
    __slots__ = ("path", "user", "mode", "chunk", "metadata")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    PATH_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    CHUNK_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    path: str
    user: _common_pb2.User
    mode: int
    chunk: bytes
    metadata: _containers.ScalarMap[str, str]
    def __init__(self, path: _Optional[str] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ..., mode: _Optional[int] = ..., chunk: _Optional[bytes] = ..., metadata: _Optional[_Mapping[str, str]] = ...) -> None: ...

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

class S3Object(_message.Message):
    __slots__ = ("bucket", "key", "region")
    BUCKET_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    REGION_FIELD_NUMBER: _ClassVar[int]
    bucket: str
    key: str
    region: str
    def __init__(self, bucket: _Optional[str] = ..., key: _Optional[str] = ..., region: _Optional[str] = ...) -> None: ...

class PresignedRequest(_message.Message):
    __slots__ = ("url", "headers")
    class HeadersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    URL_FIELD_NUMBER: _ClassVar[int]
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    url: str
    headers: _containers.ScalarMap[str, str]
    def __init__(self, url: _Optional[str] = ..., headers: _Optional[_Mapping[str, str]] = ...) -> None: ...

class PresignedMultipart(_message.Message):
    __slots__ = ("part_size", "parts")
    PART_SIZE_FIELD_NUMBER: _ClassVar[int]
    PARTS_FIELD_NUMBER: _ClassVar[int]
    part_size: int
    parts: _containers.RepeatedCompositeFieldContainer[PresignedRequest]
    def __init__(self, part_size: _Optional[int] = ..., parts: _Optional[_Iterable[_Union[PresignedRequest, _Mapping]]] = ...) -> None: ...

class StartImportRequest(_message.Message):
    __slots__ = ("path", "user", "mode", "object", "get", "delete", "wait_for_object", "expires_at_unix_ms", "max_bytes", "expected_sha256", "metadata")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    PATH_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    OBJECT_FIELD_NUMBER: _ClassVar[int]
    GET_FIELD_NUMBER: _ClassVar[int]
    DELETE_FIELD_NUMBER: _ClassVar[int]
    WAIT_FOR_OBJECT_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    MAX_BYTES_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_SHA256_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    path: str
    user: _common_pb2.User
    mode: int
    object: S3Object
    get: PresignedRequest
    delete: PresignedRequest
    wait_for_object: bool
    expires_at_unix_ms: int
    max_bytes: int
    expected_sha256: str
    metadata: _containers.ScalarMap[str, str]
    def __init__(self, path: _Optional[str] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ..., mode: _Optional[int] = ..., object: _Optional[_Union[S3Object, _Mapping]] = ..., get: _Optional[_Union[PresignedRequest, _Mapping]] = ..., delete: _Optional[_Union[PresignedRequest, _Mapping]] = ..., wait_for_object: _Optional[bool] = ..., expires_at_unix_ms: _Optional[int] = ..., max_bytes: _Optional[int] = ..., expected_sha256: _Optional[str] = ..., metadata: _Optional[_Mapping[str, str]] = ...) -> None: ...

class StartExportRequest(_message.Message):
    __slots__ = ("path", "user", "object", "put", "multipart", "expires_at_unix_ms")
    PATH_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    OBJECT_FIELD_NUMBER: _ClassVar[int]
    PUT_FIELD_NUMBER: _ClassVar[int]
    MULTIPART_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    path: str
    user: _common_pb2.User
    object: S3Object
    put: PresignedRequest
    multipart: PresignedMultipart
    expires_at_unix_ms: int
    def __init__(self, path: _Optional[str] = ..., user: _Optional[_Union[_common_pb2.User, _Mapping]] = ..., object: _Optional[_Union[S3Object, _Mapping]] = ..., put: _Optional[_Union[PresignedRequest, _Mapping]] = ..., multipart: _Optional[_Union[PresignedMultipart, _Mapping]] = ..., expires_at_unix_ms: _Optional[int] = ...) -> None: ...

class StartTransferResponse(_message.Message):
    __slots__ = ("transfer_id",)
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    transfer_id: str
    def __init__(self, transfer_id: _Optional[str] = ...) -> None: ...

class GetTransferRequest(_message.Message):
    __slots__ = ("transfer_id",)
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    transfer_id: str
    def __init__(self, transfer_id: _Optional[str] = ...) -> None: ...

class WatchTransferRequest(_message.Message):
    __slots__ = ("transfer_id",)
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    transfer_id: str
    def __init__(self, transfer_id: _Optional[str] = ...) -> None: ...

class CancelTransferRequest(_message.Message):
    __slots__ = ("transfer_id",)
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    transfer_id: str
    def __init__(self, transfer_id: _Optional[str] = ...) -> None: ...

class CancelTransferResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class TransferState(_message.Message):
    __slots__ = ("transfer_id", "direction", "phase", "bytes_done", "bytes_total", "probes", "entry", "sha256", "part_etags", "duration_ms", "error")
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    DIRECTION_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    BYTES_DONE_FIELD_NUMBER: _ClassVar[int]
    BYTES_TOTAL_FIELD_NUMBER: _ClassVar[int]
    PROBES_FIELD_NUMBER: _ClassVar[int]
    ENTRY_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    PART_ETAGS_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    transfer_id: str
    direction: TransferDirection
    phase: TransferPhase
    bytes_done: int
    bytes_total: int
    probes: int
    entry: _common_pb2.EntryInfo
    sha256: str
    part_etags: _containers.RepeatedScalarFieldContainer[str]
    duration_ms: int
    error: _common_pb2.StreamError
    def __init__(self, transfer_id: _Optional[str] = ..., direction: _Optional[_Union[TransferDirection, str]] = ..., phase: _Optional[_Union[TransferPhase, str]] = ..., bytes_done: _Optional[int] = ..., bytes_total: _Optional[int] = ..., probes: _Optional[int] = ..., entry: _Optional[_Union[_common_pb2.EntryInfo, _Mapping]] = ..., sha256: _Optional[str] = ..., part_etags: _Optional[_Iterable[str]] = ..., duration_ms: _Optional[int] = ..., error: _Optional[_Union[_common_pb2.StreamError, _Mapping]] = ...) -> None: ...

class TransferEvent(_message.Message):
    __slots__ = ("state", "keepalive")
    STATE_FIELD_NUMBER: _ClassVar[int]
    KEEPALIVE_FIELD_NUMBER: _ClassVar[int]
    state: TransferState
    keepalive: _common_pb2.KeepAlive
    def __init__(self, state: _Optional[_Union[TransferState, _Mapping]] = ..., keepalive: _Optional[_Union[_common_pb2.KeepAlive, _Mapping]] = ...) -> None: ...
