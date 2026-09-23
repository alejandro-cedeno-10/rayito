from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TimeoutMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TIMEOUT_MODE_UNSPECIFIED: _ClassVar[TimeoutMode]
    TIMEOUT_MODE_EXACT: _ClassVar[TimeoutMode]
    TIMEOUT_MODE_AT_LEAST: _ClassVar[TimeoutMode]

class TimeoutAction(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TIMEOUT_ACTION_UNSPECIFIED: _ClassVar[TimeoutAction]
    TIMEOUT_ACTION_KILL: _ClassVar[TimeoutAction]
    TIMEOUT_ACTION_PAUSE: _ClassVar[TimeoutAction]

class LifecyclePhase(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    LIFECYCLE_PHASE_UNSPECIFIED: _ClassVar[LifecyclePhase]
    LIFECYCLE_PHASE_UNMANAGED: _ClassVar[LifecyclePhase]
    LIFECYCLE_PHASE_ACTIVE: _ClassVar[LifecyclePhase]
    LIFECYCLE_PHASE_RESUME_GRACE: _ClassVar[LifecyclePhase]
    LIFECYCLE_PHASE_EXPIRED: _ClassVar[LifecyclePhase]
TIMEOUT_MODE_UNSPECIFIED: TimeoutMode
TIMEOUT_MODE_EXACT: TimeoutMode
TIMEOUT_MODE_AT_LEAST: TimeoutMode
TIMEOUT_ACTION_UNSPECIFIED: TimeoutAction
TIMEOUT_ACTION_KILL: TimeoutAction
TIMEOUT_ACTION_PAUSE: TimeoutAction
LIFECYCLE_PHASE_UNSPECIFIED: LifecyclePhase
LIFECYCLE_PHASE_UNMANAGED: LifecyclePhase
LIFECYCLE_PHASE_ACTIVE: LifecyclePhase
LIFECYCLE_PHASE_RESUME_GRACE: LifecyclePhase
LIFECYCLE_PHASE_EXPIRED: LifecyclePhase

class SetTimeoutRequest(_message.Message):
    __slots__ = ("timeout_ms", "mode")
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    timeout_ms: int
    mode: TimeoutMode
    def __init__(self, timeout_ms: _Optional[int] = ..., mode: _Optional[_Union[TimeoutMode, str]] = ...) -> None: ...

class LifecycleState(_message.Message):
    __slots__ = ("phase", "deadline_unix_ms", "cap_unix_ms", "timeout_ms", "on_timeout", "auto_resume", "extensions")
    PHASE_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    CAP_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    ON_TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    AUTO_RESUME_FIELD_NUMBER: _ClassVar[int]
    EXTENSIONS_FIELD_NUMBER: _ClassVar[int]
    phase: LifecyclePhase
    deadline_unix_ms: int
    cap_unix_ms: int
    timeout_ms: int
    on_timeout: TimeoutAction
    auto_resume: bool
    extensions: int
    def __init__(self, phase: _Optional[_Union[LifecyclePhase, str]] = ..., deadline_unix_ms: _Optional[int] = ..., cap_unix_ms: _Optional[int] = ..., timeout_ms: _Optional[int] = ..., on_timeout: _Optional[_Union[TimeoutAction, str]] = ..., auto_resume: _Optional[bool] = ..., extensions: _Optional[int] = ...) -> None: ...
