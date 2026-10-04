from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class LifecycleEventsConfig(_message.Message):
    __slots__ = ("sandbox_key", "sandbox_id", "image_arn", "image_version")
    SANDBOX_KEY_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    IMAGE_ARN_FIELD_NUMBER: _ClassVar[int]
    IMAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    sandbox_key: bytes
    sandbox_id: str
    image_arn: str
    image_version: str
    def __init__(self, sandbox_key: _Optional[bytes] = ..., sandbox_id: _Optional[str] = ..., image_arn: _Optional[str] = ..., image_version: _Optional[str] = ...) -> None: ...

class LifecycleEventsStatus(_message.Message):
    __slots__ = ("emitted", "dropped", "last_error_class")
    EMITTED_FIELD_NUMBER: _ClassVar[int]
    DROPPED_FIELD_NUMBER: _ClassVar[int]
    LAST_ERROR_CLASS_FIELD_NUMBER: _ClassVar[int]
    emitted: int
    dropped: int
    last_error_class: str
    def __init__(self, emitted: _Optional[int] = ..., dropped: _Optional[int] = ..., last_error_class: _Optional[str] = ...) -> None: ...
