from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FileType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FILE_TYPE_UNSPECIFIED: _ClassVar[FileType]
    FILE_TYPE_FILE: _ClassVar[FileType]
    FILE_TYPE_DIRECTORY: _ClassVar[FileType]
    FILE_TYPE_SYMLINK: _ClassVar[FileType]
FILE_TYPE_UNSPECIFIED: FileType
FILE_TYPE_FILE: FileType
FILE_TYPE_DIRECTORY: FileType
FILE_TYPE_SYMLINK: FileType

class User(_message.Message):
    __slots__ = ("username",)
    USERNAME_FIELD_NUMBER: _ClassVar[int]
    username: str
    def __init__(self, username: _Optional[str] = ...) -> None: ...

class StreamError(_message.Message):
    __slots__ = ("code", "message")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    def __init__(self, code: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class KeepAlive(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class EntryInfo(_message.Message):
    __slots__ = ("name", "type", "path", "size", "mode", "permissions", "owner", "group", "modified_time_unix_ms", "symlink_target")
    NAME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    PERMISSIONS_FIELD_NUMBER: _ClassVar[int]
    OWNER_FIELD_NUMBER: _ClassVar[int]
    GROUP_FIELD_NUMBER: _ClassVar[int]
    MODIFIED_TIME_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    SYMLINK_TARGET_FIELD_NUMBER: _ClassVar[int]
    name: str
    type: FileType
    path: str
    size: int
    mode: int
    permissions: str
    owner: str
    group: str
    modified_time_unix_ms: int
    symlink_target: str
    def __init__(self, name: _Optional[str] = ..., type: _Optional[_Union[FileType, str]] = ..., path: _Optional[str] = ..., size: _Optional[int] = ..., mode: _Optional[int] = ..., permissions: _Optional[str] = ..., owner: _Optional[str] = ..., group: _Optional[str] = ..., modified_time_unix_ms: _Optional[int] = ..., symlink_target: _Optional[str] = ...) -> None: ...
