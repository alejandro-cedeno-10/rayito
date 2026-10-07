"""`tests/e2e/image_cleanup.py` sin AWS: el teardown del e2e de templates
borra cada imagen que construyó, tolera las que nunca llegaron a crearse,
reintenta mientras la imagen está ocupada, borra también su grupo de logs
`/rayito/<nombre>` y devuelve como fallo lo que no pudo borrar, sin cortar
el resto."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from rayito._images import LOG_GROUP_PREFIX
from tests.e2e.image_cleanup import (
    DEFAULT_TIMEOUT_SECONDS,
    POLL_INTERVAL_SECONDS,
    RETRY_BACKOFF_SECONDS,
    BuiltImages,
)


def _error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


class FakeMicrovms:
    """`delete_microvm_image`/`get_microvm_image` guiados por guion: cada
    llamada consume el siguiente resultado de su lista (una excepción se
    lanza; un estado se devuelve); sin guion, la imagen ya no existe."""

    def __init__(
        self,
        deletes: dict[str, list[Exception | None]] | None = None,
        states: dict[str, list[str | Exception]] | None = None,
    ) -> None:
        self.deletes = deletes or {}
        self.states = states or {}
        self.calls: list[tuple[str, str]] = []

    def delete_microvm_image(self, *, imageIdentifier: str) -> dict[str, Any]:
        self.calls.append(("delete", imageIdentifier))
        script = self.deletes.get(imageIdentifier, [None])
        outcome = script.pop(0) if len(script) > 1 else script[0]
        if outcome is not None:
            raise outcome
        return {"state": "DELETING"}

    def get_microvm_image(self, *, imageIdentifier: str) -> dict[str, Any]:
        self.calls.append(("get", imageIdentifier))
        script = self.states.get(imageIdentifier, [_error("ResourceNotFoundException")])
        outcome = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(outcome, Exception):
            raise outcome
        return {"state": outcome}


class FakeLogs:
    """`delete_log_group` guiado por guion, como `FakeMicrovms`; sin guion
    el grupo existe y se borra."""

    def __init__(self, deletes: dict[str, list[Exception | None]] | None = None) -> None:
        self.deletes = deletes or {}
        self.calls: list[str] = []

    def delete_log_group(self, *, logGroupName: str) -> dict[str, Any]:
        self.calls.append(logGroupName)
        script = self.deletes.get(logGroupName, [None])
        outcome = script.pop(0) if len(script) > 1 else script[0]
        if outcome is not None:
            raise outcome
        return {}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def _images(
    microvms: FakeMicrovms, clock: FakeClock | None = None, logs: FakeLogs | None = None
) -> BuiltImages:
    clock = clock or FakeClock()
    return BuiltImages(
        microvms,
        resolve_arn=lambda name: f"arn:{name}",
        logs=logs or FakeLogs(),
        sleep=clock.sleep,
        clock=clock,
    )


def test_every_tracked_image_is_deleted_and_waited_for() -> None:
    microvms = FakeMicrovms(states={"arn:a": ["DELETING", "DELETING", "DELETED"]})
    clock = FakeClock()
    images = _images(microvms, clock)
    assert images.track("a") == "a"
    images.track("b")

    assert images.delete_all() == []
    assert ("delete", "arn:a") in microvms.calls
    assert ("delete", "arn:b") in microvms.calls
    assert clock.sleeps == [POLL_INTERVAL_SECONDS, POLL_INTERVAL_SECONDS]
    assert images.names == []


def test_an_image_the_build_never_created_is_fine() -> None:
    microvms = FakeMicrovms(deletes={"arn:x": [_error("ResourceNotFoundException")]})
    images = _images(microvms)
    images.track("x")
    assert images.delete_all() == []
    assert microvms.calls == [("delete", "arn:x")]


def test_a_busy_image_is_retried_with_backoff() -> None:
    conflict = _error("ConflictException")
    microvms = FakeMicrovms(deletes={"arn:a": [conflict, _error("ThrottlingException"), None]})
    clock = FakeClock()
    images = _images(microvms, clock)
    images.track("a")
    assert images.delete_all() == []
    assert clock.sleeps == list(RETRY_BACKOFF_SECONDS[:2])


def test_a_failure_is_reported_and_the_rest_still_deleted() -> None:
    microvms = FakeMicrovms(
        deletes={
            "arn:denied": [_error("AccessDeniedException")],
            "arn:busy": [_error("ConflictException")],
        },
        states={"arn:stuck": ["DELETE_FAILED"]},
    )
    images = _images(microvms)
    for name in ("denied", "busy", "stuck", "ok"):
        images.track(name)

    assert images.delete_all() == [
        "denied: AccessDeniedException",
        "busy: ConflictException",
        "stuck: DELETE_FAILED",
    ]
    assert ("delete", "arn:ok") in microvms.calls
    busy_deletes = [call for call in microvms.calls if call == ("delete", "arn:busy")]
    assert len(busy_deletes) == len(RETRY_BACKOFF_SECONDS) + 1


def test_waiting_gives_up_after_the_timeout() -> None:
    microvms = FakeMicrovms(states={"arn:slow": ["DELETING"]})
    clock = FakeClock()
    images = _images(microvms, clock)
    images.track("slow")
    (failure,) = images.delete_all()
    assert failure.startswith("slow: sigue DELETING")
    assert clock.now >= DEFAULT_TIMEOUT_SECONDS


def test_each_image_log_group_is_deleted_too() -> None:
    logs = FakeLogs()
    images = _images(FakeMicrovms(), logs=logs)
    images.track("a")
    images.track("b")
    assert images.delete_all() == []
    assert logs.calls == [f"{LOG_GROUP_PREFIX}/a", f"{LOG_GROUP_PREFIX}/b"]


def test_a_log_group_that_never_existed_is_fine() -> None:
    group = f"{LOG_GROUP_PREFIX}/x"
    logs = FakeLogs({group: [_error("ResourceNotFoundException")]})
    images = _images(FakeMicrovms(), logs=logs)
    images.track("x")
    assert images.delete_all() == []
    assert logs.calls == [group]


def test_a_throttled_log_group_delete_is_retried() -> None:
    group = f"{LOG_GROUP_PREFIX}/a"
    logs = FakeLogs(
        {group: [_error("ThrottlingException"), _error("OperationAbortedException"), None]}
    )
    clock = FakeClock()
    images = _images(FakeMicrovms(), clock, logs)
    images.track("a")
    assert images.delete_all() == []
    assert logs.calls == [group, group, group]
    assert clock.sleeps == list(RETRY_BACKOFF_SECONDS[:2])


def test_a_log_group_failure_is_reported_and_the_image_still_deleted() -> None:
    logs = FakeLogs({f"{LOG_GROUP_PREFIX}/denied": [_error("AccessDeniedException")]})
    microvms = FakeMicrovms()
    images = _images(microvms, logs=logs)
    images.track("denied")
    images.track("ok")
    assert images.delete_all() == ["denied (logs): AccessDeniedException"]
    assert ("delete", "arn:denied") in microvms.calls
    assert logs.calls[-1] == f"{LOG_GROUP_PREFIX}/ok"


def test_the_log_group_is_deleted_even_when_the_image_is_not() -> None:
    microvms = FakeMicrovms(deletes={"arn:denied": [_error("AccessDeniedException")]})
    logs = FakeLogs()
    images = _images(microvms, logs=logs)
    images.track("denied")
    assert images.delete_all() == ["denied: AccessDeniedException"]
    assert logs.calls == [f"{LOG_GROUP_PREFIX}/denied"]
