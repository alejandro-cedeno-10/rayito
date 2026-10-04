"""`AsyncSandbox.git` contra el `ProcessService` falso: la misma superficie
que `Git` como corrutinas."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator

import pytest

from rayito import AsyncGit, AsyncSandbox, Git
from rayito._limits import DEFAULT_PORT
from rayito.exceptions import (
    CommandExitException,
    GitAuthException,
    GitUpstreamException,
    InvalidArgumentException,
    TimeoutException,
)

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    SANDBOX_ID,
    RaydEndpoint,
    StubbedControlPlane,
    auth_token_response,
    microvm_response,
)
from .fake_process import CannedReply
from .log_capture import capture_logs

REMOTE_URL = "https://github.com/o/r.git"
PASSWORD = "s3cr3t:p@ss"
ENCODED_PASSWORD = "s3cr3t%3Ap%40ss"
ISOLATION = "'-c' 'core.hooksPath=/dev/null' '-c' 'credential.helper='"
REWRITE_CHECK = "'config' '--get-regexp' '^url\\..*\\.(push)?insteadof$'"


@pytest.fixture
async def sandbox(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> AsyncIterator[AsyncSandbox]:
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )
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
        control_plane.microvms.add_response("terminate_microvm", {})
        await created.kill()


def public_methods(cls: type) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def test_async_surface_matches_sync() -> None:
    sync_methods = public_methods(Git)
    async_methods = public_methods(AsyncGit)
    assert sync_methods.keys() == async_methods.keys()
    for name, signature in sync_methods.items():
        assert list(signature.parameters) == list(async_methods[name].parameters), name
        assert inspect.iscoroutinefunction(getattr(AsyncGit, name)), name


async def test_async_exact_commands_on_the_wire(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when("'status'", CannedReply(stdout="## main\n?? new.txt\n"))
    fake_rayd.process.reply_when("'git'", CannedReply())
    assert isinstance(sandbox.git, AsyncGit)
    status = await sandbox.git.status("/repo")
    await sandbox.git.add("/repo")
    await sandbox.git.commit("/repo", "msg", allow_empty=True)
    await sandbox.git.reset("/repo", mode="soft", target="HEAD~1")
    assert fake_rayd.process.commands() == [
        "'git' '-C' '/repo' 'status' '--porcelain=1' '-b'",
        "'git' '-C' '/repo' 'add' '-A'",
        "'git' '-C' '/repo' 'commit' '-m' 'msg' '--allow-empty'",
        "'git' '-C' '/repo' 'reset' '--soft' 'HEAD~1'",
    ]
    assert all(
        request.process.envs["GIT_TERMINAL_PROMPT"] == "0"
        for request in fake_rayd.process.start_requests
    )
    assert status.has_untracked


async def test_async_invalid_arguments_fail_before_any_rpc(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    with pytest.raises(InvalidArgumentException):
        await sandbox.git.reset("/repo", mode="bogus")  # type: ignore[arg-type]
    with pytest.raises(InvalidArgumentException):
        await sandbox.git.restore("/repo", [])
    assert fake_rayd.process.start_requests == []


async def test_async_push_with_credentials_restores_the_url(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when("'--get-regexp'", CannedReply(exit_code=1))
    fake_rayd.process.reply_when("'get-url'", CannedReply(stdout=f"{REMOTE_URL}\n"))
    fake_rayd.process.reply_when("'set-url'", CannedReply())
    fake_rayd.process.reply_when(
        "'push'", CannedReply(stderr=f"error: rejected {PASSWORD}\n", exit_code=128)
    )
    fake_rayd.process.reply_when("'remote'", CannedReply(stdout="origin\n"))
    with capture_logs("rayito") as logs, pytest.raises(CommandExitException) as excinfo:
        await sandbox.git.push("/repo", username="u", password=PASSWORD)
    assert fake_rayd.process.commands() == [
        "'git' '-C' '/repo' 'remote'",
        f"'git' '-C' '/repo' {REWRITE_CHECK}",
        "'git' '-C' '/repo' 'remote' 'get-url' 'origin'",
        "'git' '-C' '/repo' 'remote' 'set-url' 'origin' "
        f"'https://u:{ENCODED_PASSWORD}@github.com/o/r.git'",
        f"'git' '-C' '/repo' {ISOLATION} 'push' '--set-upstream' 'origin'",
        f"'git' '-C' '/repo' 'remote' 'set-url' 'origin' '{REMOTE_URL}'",
    ]
    assert PASSWORD not in f"{excinfo.value} {excinfo.value.stderr}"
    assert excinfo.value.__cause__ is None and excinfo.value.__context__ is None
    assert logs.records == []


async def test_async_failures_are_classified(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when(
        "'push'",
        CannedReply(stderr="fatal: Authentication failed for 'https://h/'\n", exit_code=128),
    )
    fake_rayd.process.reply_when(
        "'pull'", CannedReply(stderr="fatal: has no upstream branch\n", exit_code=1)
    )
    with pytest.raises(GitAuthException):
        await sandbox.git.push("/repo")
    with pytest.raises(GitUpstreamException):
        await sandbox.git.pull("/repo", remote="origin")


async def test_async_dangerously_authenticate_runs_the_two_commands(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when("git", CannedReply())
    await sandbox.git.dangerously_authenticate("ana", "tok")
    commands = fake_rayd.process.commands()
    assert commands[0] == "'git' 'config' '--global' 'credential.helper' 'store'"
    assert commands[1].endswith("| 'git' 'credential' 'approve'")
    assert "host=github.com" in commands[1]
    assert len(commands) == 2


async def test_async_a_restore_that_times_out_keeps_the_push_error_and_warns(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when("'--get-regexp'", CannedReply(exit_code=1))
    fake_rayd.process.reply_when(f"'set-url' 'origin' '{REMOTE_URL}'", CannedReply(times_out=True))
    fake_rayd.process.reply_when("'get-url'", CannedReply(stdout=f"{REMOTE_URL}\n"))
    fake_rayd.process.reply_when("'set-url'", CannedReply())
    fake_rayd.process.reply_when("'push'", CannedReply(stderr="error: rejected\n", exit_code=128))
    with capture_logs("rayito") as logs, pytest.raises(CommandExitException) as excinfo:
        await sandbox.git.push("/repo", remote="origin", username="u", password=PASSWORD)
    assert excinfo.value.exit_code == 128
    assert "git push" in logs.text()
    assert PASSWORD not in logs.text() and ENCODED_PASSWORD not in logs.text()


async def test_async_a_clone_that_times_out_still_tries_to_strip_the_origin(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when("'--get-regexp'", CannedReply(exit_code=1))
    fake_rayd.process.reply_when("'clone'", CannedReply(times_out=True))
    fake_rayd.process.reply_when("'set-url'", CannedReply())
    with pytest.raises(TimeoutException):
        await sandbox.git.clone(REMOTE_URL, "/home/user/r", username="u", password=PASSWORD)
    assert fake_rayd.process.commands()[-1] == (
        f"'git' '-C' '/home/user/r' 'remote' 'set-url' 'origin' '{REMOTE_URL}'"
    )


async def test_async_a_url_rewrite_refuses_the_credentials(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when(
        "'--get-regexp'", CannedReply(stdout="url.http://127.0.0.1:9999/.insteadof https://\n")
    )
    with pytest.raises(GitAuthException, match="insteadOf"):
        await sandbox.git.pull("/repo", remote="origin", username="u", password=PASSWORD)
    assert ENCODED_PASSWORD not in " ".join(fake_rayd.process.commands())
