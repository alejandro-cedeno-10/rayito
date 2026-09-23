from rayito.v1 import lifecycle_pb2 as _lifecycle_pb2
from rayito.v1 import network_pb2 as _network_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class HealthRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class HealthResponse(_message.Message):
    __slots__ = ("agent_ready", "kernel_ready", "agent_version", "uptime_ms", "sandbox_id", "resume_generation", "clock_offset_ms", "kernel_state_lost", "imds_blocked", "hook_anomalies", "metadata", "lifecycle", "egress_enforcement", "cpu_count", "memory_total_bytes")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    AGENT_READY_FIELD_NUMBER: _ClassVar[int]
    KERNEL_READY_FIELD_NUMBER: _ClassVar[int]
    AGENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    UPTIME_MS_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    RESUME_GENERATION_FIELD_NUMBER: _ClassVar[int]
    CLOCK_OFFSET_MS_FIELD_NUMBER: _ClassVar[int]
    KERNEL_STATE_LOST_FIELD_NUMBER: _ClassVar[int]
    IMDS_BLOCKED_FIELD_NUMBER: _ClassVar[int]
    HOOK_ANOMALIES_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    LIFECYCLE_FIELD_NUMBER: _ClassVar[int]
    EGRESS_ENFORCEMENT_FIELD_NUMBER: _ClassVar[int]
    CPU_COUNT_FIELD_NUMBER: _ClassVar[int]
    MEMORY_TOTAL_BYTES_FIELD_NUMBER: _ClassVar[int]
    agent_ready: bool
    kernel_ready: bool
    agent_version: str
    uptime_ms: int
    sandbox_id: str
    resume_generation: int
    clock_offset_ms: int
    kernel_state_lost: bool
    imds_blocked: bool
    hook_anomalies: int
    metadata: _containers.ScalarMap[str, str]
    lifecycle: _lifecycle_pb2.LifecycleState
    egress_enforcement: _network_pb2.EgressEnforcement
    cpu_count: int
    memory_total_bytes: int
    def __init__(self, agent_ready: _Optional[bool] = ..., kernel_ready: _Optional[bool] = ..., agent_version: _Optional[str] = ..., uptime_ms: _Optional[int] = ..., sandbox_id: _Optional[str] = ..., resume_generation: _Optional[int] = ..., clock_offset_ms: _Optional[int] = ..., kernel_state_lost: _Optional[bool] = ..., imds_blocked: _Optional[bool] = ..., hook_anomalies: _Optional[int] = ..., metadata: _Optional[_Mapping[str, str]] = ..., lifecycle: _Optional[_Union[_lifecycle_pb2.LifecycleState, _Mapping]] = ..., egress_enforcement: _Optional[_Union[_network_pb2.EgressEnforcement, str]] = ..., cpu_count: _Optional[int] = ..., memory_total_bytes: _Optional[int] = ...) -> None: ...

class MetricsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class MetricsResponse(_message.Message):
    __slots__ = ("cpu_used_pct", "mem_used_bytes", "mem_total_bytes", "disk_used_bytes", "disk_total_bytes", "cpu_count", "timestamp_unix_ms", "mem_cache_bytes")
    CPU_USED_PCT_FIELD_NUMBER: _ClassVar[int]
    MEM_USED_BYTES_FIELD_NUMBER: _ClassVar[int]
    MEM_TOTAL_BYTES_FIELD_NUMBER: _ClassVar[int]
    DISK_USED_BYTES_FIELD_NUMBER: _ClassVar[int]
    DISK_TOTAL_BYTES_FIELD_NUMBER: _ClassVar[int]
    CPU_COUNT_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    MEM_CACHE_BYTES_FIELD_NUMBER: _ClassVar[int]
    cpu_used_pct: float
    mem_used_bytes: int
    mem_total_bytes: int
    disk_used_bytes: int
    disk_total_bytes: int
    cpu_count: int
    timestamp_unix_ms: int
    mem_cache_bytes: int
    def __init__(self, cpu_used_pct: _Optional[float] = ..., mem_used_bytes: _Optional[int] = ..., mem_total_bytes: _Optional[int] = ..., disk_used_bytes: _Optional[int] = ..., disk_total_bytes: _Optional[int] = ..., cpu_count: _Optional[int] = ..., timestamp_unix_ms: _Optional[int] = ..., mem_cache_bytes: _Optional[int] = ...) -> None: ...

class MetricsHistoryRequest(_message.Message):
    __slots__ = ("start_unix_ms", "end_unix_ms", "max_points")
    START_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    END_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    MAX_POINTS_FIELD_NUMBER: _ClassVar[int]
    start_unix_ms: int
    end_unix_ms: int
    max_points: int
    def __init__(self, start_unix_ms: _Optional[int] = ..., end_unix_ms: _Optional[int] = ..., max_points: _Optional[int] = ...) -> None: ...

class MetricsHistoryResponse(_message.Message):
    __slots__ = ("samples", "oldest_unix_ms")
    SAMPLES_FIELD_NUMBER: _ClassVar[int]
    OLDEST_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    samples: _containers.RepeatedCompositeFieldContainer[MetricsResponse]
    oldest_unix_ms: int
    def __init__(self, samples: _Optional[_Iterable[_Union[MetricsResponse, _Mapping]]] = ..., oldest_unix_ms: _Optional[int] = ...) -> None: ...
