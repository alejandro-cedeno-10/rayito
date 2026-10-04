"""Tope de la salida que el cliente guarda de un comando o una PTY
(`max_output_bytes`, `COMMAND_OUTPUT_MAX_BYTES` por defecto): código del
sandbox que escribe sin parar no agota la memoria del proceso del operador.
Se guarda la cola, lo descartado se cuenta y `truncated` lo dice; los
callbacks reciben siempre todo."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest

from rayito import AsyncSandbox, CommandResult, Sandbox
from rayito._limits import COMMAND_OUTPUT_MAX_BYTES
from rayito._process_base import DecodedStream, OutputAccumulator, validate_max_output_bytes
from rayito.exceptions import CommandExitException, InvalidArgumentException

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    SANDBOX_ID,
    RaydEndpoint,
    StubbedControlPlane,
)
from .fake_process import CHUNK_SIZE, CannedReply
from .test_commands_sync import stub_launch


def test_decoded_stream_keeps_only_the_tail_and_counts_the_rest() -> None:
    seen: list[str] = []
    stream = DecodedStream(seen.append, max_bytes=10)
    for piece in (b"0123456", b"789ab", b"cdef"):
        stream.feed(piece)
    stream.flush()
    assert stream.text == "6789abcdef"
    assert stream.dropped_bytes == 6
    assert stream.truncated
    assert "".join(seen) == "0123456789abcdef"


def test_decoded_stream_under_the_cap_is_not_truncated() -> None:
    stream = DecodedStream(None, max_bytes=10)
    stream.feed(b"hola")
    assert stream.text == "hola" and not stream.truncated


def test_a_zero_cap_keeps_nothing_but_the_callback_sees_everything() -> None:
    seen: list[str] = []
    stream = DecodedStream(seen.append, max_bytes=0)
    stream.feed("señal".encode())
    stream.flush()
    assert stream.text == ""
    assert "".join(seen) == "señal"


def test_a_multibyte_character_split_across_chunks_still_decodes_under_the_cap() -> None:
    stream = DecodedStream(None, max_bytes=8)
    encoded = "aé".encode()
    stream.feed(encoded[:2])
    stream.feed(encoded[2:])
    stream.flush()
    assert stream.text == "aé"


def test_the_default_cap_is_the_shared_limit() -> None:
    accumulator = OutputAccumulator()
    accumulator.feed_terminal(1, b"x" * 10)
    assert accumulator.stdout == "x" * 10
    assert COMMAND_OUTPUT_MAX_BYTES == 64 * 1024 * 1024


def test_pty_bytes_are_bounded_too() -> None:
    """Una sesión de terminal larga (`rayito sandbox connect`) no guarda cada
    byte que ha pasado por ella."""
    accumulator = OutputAccumulator(max_bytes=4)
    for seq in range(1, 6):
        accumulator.feed_terminal(seq, b"ab")
    assert accumulator.stdout == "abab"
    assert accumulator.truncated
    assert accumulator.last_seq == 5


@pytest.mark.parametrize("value", [-1, 1.5, True, "10"])
def test_invalid_caps_are_rejected(value: object) -> None:
    with pytest.raises(InvalidArgumentException, match="max_output_bytes"):
        validate_max_output_bytes(value)  # type: ignore[arg-type]


@pytest.fixture
def sandbox(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> Iterator[Sandbox]:
    stub_launch(control_plane, fake_rayd)
    created = Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        yield created
    finally:
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        created.kill()


def test_run_keeps_the_tail_and_flags_truncation(sandbox: Sandbox) -> None:
    cap = CHUNK_SIZE
    seen: list[str] = []
    result = sandbox.commands.run(
        f"big {3 * CHUNK_SIZE}", on_stdout=seen.append, max_output_bytes=cap
    )
    assert isinstance(result, CommandResult)
    assert len(result.stdout) == cap
    assert result.truncated
    assert sum(len(chunk) for chunk in seen) == 3 * CHUNK_SIZE


def test_run_under_the_cap_is_not_truncated(sandbox: Sandbox) -> None:
    result = sandbox.commands.run("echo hola")
    assert result.stdout == "hola\n" and not result.truncated


def test_a_failing_command_carries_the_flag(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    fake_rayd.process.reply_when("boom", CannedReply(stdout="x" * 100, exit_code=3))
    with pytest.raises(CommandExitException) as excinfo:
        sandbox.commands.run("boom", max_output_bytes=10)
    assert excinfo.value.stdout == "x" * 10
    assert excinfo.value.truncated


def test_a_negative_cap_fails_before_any_rpc(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    with pytest.raises(InvalidArgumentException):
        sandbox.commands.run("echo x", max_output_bytes=-1)
    assert fake_rayd.process.start_requests == []


@pytest.fixture
async def async_sandbox(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> AsyncIterator[AsyncSandbox]:
    stub_launch(control_plane, fake_rayd)
    created = await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        yield created
    finally:
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        await created.kill()


async def test_async_run_keeps_the_tail_and_flags_truncation(
    async_sandbox: AsyncSandbox,
) -> None:
    result = await async_sandbox.commands.run(f"big {2 * CHUNK_SIZE}", max_output_bytes=CHUNK_SIZE)
    assert len(result.stdout) == CHUNK_SIZE and result.truncated
