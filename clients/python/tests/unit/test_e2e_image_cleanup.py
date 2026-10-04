"""`tests/e2e/image_cleanup.py` sin AWS: el teardown del e2e de templates
borra cada imagen que construyó, tolera las que nunca llegaron a crearse,
reintenta mientras la imagen está ocupada y devuelve como fallo lo que no
pudo borrar, sin cortar el resto."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

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


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def _images(microvms: FakeMicrovms, clock: FakeClock | None = None) -> BuiltImages:
    clock = clock or FakeClock()
    return BuiltImages(
        microvms, resolve_arn=lambda name: f"arn:{name}", sleep=clock.sleep, clock=clock
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
