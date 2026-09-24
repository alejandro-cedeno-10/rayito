"""`Sandbox.git` contra el `ProcessService` falso: comandos exactos en el
cable, flujo de credenciales, clasificación de errores y redacción."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from rayito import Git, GitStatus, Sandbox
from rayito._limits import DEFAULT_PORT
from rayito.exceptions import (
    CommandExitException,
    GitAuthException,
    GitUpstreamException,
    InvalidArgumentException,
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


@pytest.fixture
def sandbox(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> Iterator[Sandbox]:
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
        control_plane.microvms.add_response("terminate_microvm", {})
        created.kill()


def reply_with_a_remote(fake_rayd: RaydEndpoint, push: CannedReply) -> None:
    fake_rayd.process.reply_when("'get-url'", CannedReply(stdout=f"{REMOTE_URL}\n"))
    fake_rayd.process.reply_when("'set-url'", CannedReply())
    fake_rayd.process.reply_when("'push'", push)
    fake_rayd.process.reply_when("'pull'", push)
    fake_rayd.process.reply_when("'remote'", CannedReply(stdout="origin\n"))


def test_git_is_a_lazy_property(sandbox: Sandbox) -> None:
    assert isinstance(sandbox.git, Git)
    assert sandbox.git is sandbox.git


def test_exact_commands_on_the_wire(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    fake_rayd.process.reply_when("'status'", CannedReply(stdout="## main...origin/main\n"))
    fake_rayd.process.reply_when("'git'", CannedReply())
    status = sandbox.git.status("/repo")
    sandbox.git.add("/repo")
    sandbox.git.commit("/repo", "msg", allow_empty=True)
    sandbox.git.reset("/repo", mode="soft", target="HEAD~1")
    assert fake_rayd.process.commands() == [
        "'git' '-C' '/repo' 'status' '--porcelain=1' '-b'",
        "'git' '-C' '/repo' 'add' '-A'",
        "'git' '-C' '/repo' 'commit' '-m' 'msg' '--allow-empty'",
        "'git' '-C' '/repo' 'reset' '--soft' 'HEAD~1'",
    ]
    for request in fake_rayd.process.start_requests:
        assert request.process.envs["GIT_TERMINAL_PROMPT"] == "0"
        assert request.timeout_ms == 0
    assert isinstance(status, GitStatus)
    assert (status.current_branch, status.upstream, status.is_clean) == (
        "main",
        "origin/main",
        True,
    )


def test_caller_envs_and_options_reach_the_command(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when("'branch'", CannedReply(stdout="dev\t \nmain\t*\n"))
    branches = sandbox.git.branches("/repo", envs={"A": "1"}, user="user", timeout=30)
    request = fake_rayd.process.start_requests[-1]
    assert dict(request.process.envs) == {"GIT_TERMINAL_PROMPT": "0", "A": "1"}
    assert request.user.username == "user"
    assert request.timeout_ms == 30_000
    assert (branches.branches, branches.current_branch) == (["dev", "main"], "main")


def test_invalid_arguments_fail_before_any_rpc(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    with pytest.raises(InvalidArgumentException):
        sandbox.git.reset("/repo", mode="bogus")  # type: ignore[arg-type]
    with pytest.raises(InvalidArgumentException):
        sandbox.git.restore("/repo", [])
    with pytest.raises(InvalidArgumentException):
        sandbox.git.push("/repo", password="x")
    with pytest.raises(InvalidArgumentException):
        sandbox.git.clone("git@github.com:o/r.git", "/dst", username="u", password="x")
    with pytest.raises(InvalidArgumentException):
        sandbox.git.clone("https://github.com", username="u", password="x")
    assert fake_rayd.process.start_requests == []


def test_push_with_credentials_restores_the_url_even_when_it_fails(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    reply_with_a_remote(
        fake_rayd, CannedReply(stderr="error: failed to push some refs\n", exit_code=128)
    )
    with pytest.raises(CommandExitException) as excinfo:
        sandbox.git.push("/repo", username="u", password=PASSWORD)
    assert fake_rayd.process.commands() == [
        "'git' '-C' '/repo' 'remote'",
        "'git' '-C' '/repo' 'remote' 'get-url' 'origin'",
        f"'git' '-C' '/repo' 'remote' 'set-url' 'origin' 'https://u:{ENCODED_PASSWORD}@github.com/o/r.git'",
        "'git' '-C' '/repo' 'push' '--set-upstream' 'origin'",
        f"'git' '-C' '/repo' 'remote' 'set-url' 'origin' '{REMOTE_URL}'",
    ]
    assert excinfo.value.exit_code == 128


def test_pull_with_credentials_uses_the_resolved_remote(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    reply_with_a_remote(fake_rayd, CannedReply(stdout="Already up to date.\n"))
    result = sandbox.git.pull("/repo", branch="main", username="u", password=PASSWORD)
    assert result.stdout == "Already up to date.\n"
    commands = fake_rayd.process.commands()
    assert commands[3] == "'git' '-C' '/repo' 'pull' 'origin' 'main'"
    assert commands[-1] == f"'git' '-C' '/repo' 'remote' 'set-url' 'origin' '{REMOTE_URL}'"


def test_auth_and_upstream_failures_are_classified(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when(
        "'push'",
        CannedReply(
            stderr="fatal: could not read Username for 'https://github.com': "
            "terminal prompts disabled\n",
            exit_code=128,
        ),
    )
    fake_rayd.process.reply_when(
        "'pull'",
        CannedReply(
            stderr="There is no tracking information for the current branch.\n", exit_code=1
        ),
    )
    with pytest.raises(GitAuthException) as auth:
        sandbox.git.push("/repo")
    with pytest.raises(GitUpstreamException):
        sandbox.git.pull("/repo", remote="origin")
    assert "github.com" not in str(auth.value)
    assert str(auth.value) == "git push necesita credenciales para repositorios privados"


def test_pull_without_upstream_fails_before_pulling(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when(
        "'rev-parse'", CannedReply(stderr="fatal: no upstream configured\n", exit_code=128)
    )
    with pytest.raises(GitUpstreamException):
        sandbox.git.pull("/repo")
    assert fake_rayd.process.commands() == [
        "'git' '-C' '/repo' 'rev-parse' '--abbrev-ref' '--symbolic-full-name' '@{u}'"
    ]


def test_failed_credentialed_clone_never_leaks_the_password(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when(
        "'clone'",
        CannedReply(
            stderr=f"fatal: unable to access 'https://u:{ENCODED_PASSWORD}@github.com/o/r.git/': "
            f"error 500 ({PASSWORD})\n",
            stdout=PASSWORD,
            exit_code=128,
        ),
    )
    with capture_logs("rayito") as logs, pytest.raises(CommandExitException) as excinfo:
        sandbox.git.clone(REMOTE_URL, "/home/user/r", username="u", password=PASSWORD)
    failure = excinfo.value
    visible = f"{failure} {failure.stderr} {failure.stdout} {failure.error}"
    assert PASSWORD not in visible and ENCODED_PASSWORD not in visible
    assert "***" in failure.stderr
    assert failure.__cause__ is None and failure.__context__ is None
    assert PASSWORD not in logs.text() and ENCODED_PASSWORD not in logs.text()


def test_credentialed_clone_auth_failure_hides_the_chain(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when(
        "'clone'", CannedReply(stderr=f"fatal: Authentication failed {PASSWORD}\n", exit_code=128)
    )
    with pytest.raises(GitAuthException) as excinfo:
        sandbox.git.clone(REMOTE_URL, "/home/user/r", username="u", password=PASSWORD)
    assert excinfo.value.__cause__ is None and excinfo.value.__context__ is None


def test_successful_credentialed_clone_strips_the_origin(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when("'git'", CannedReply())
    sandbox.git.clone(REMOTE_URL, username="u", password=PASSWORD, depth=1)
    assert fake_rayd.process.commands() == [
        f"'git' 'clone' 'https://u:{ENCODED_PASSWORD}@github.com/o/r.git' '--depth' '1'",
        f"'git' '-C' 'r' 'remote' 'set-url' 'origin' '{REMOTE_URL}'",
    ]


def test_dangerously_authenticate_runs_the_two_commands(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when("git", CannedReply())
    sandbox.git.dangerously_authenticate("ana", "tok", host="example.invalid")
    commands = fake_rayd.process.commands()
    assert commands[0] == "'git' 'config' '--global' 'credential.helper' 'store'"
    assert commands[1] == (
        "printf %s 'protocol=https\nhost=example.invalid\nusername=ana\npassword=tok\n\n' "
        "| 'git' 'credential' 'approve'"
    )
    assert len(commands) == 2
    with pytest.raises(InvalidArgumentException):
        sandbox.git.dangerously_authenticate("ana", "")


def test_config_and_remote_reads(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    fake_rayd.process.reply_when("'--get'", CannedReply(stdout="true\n"))
    fake_rayd.process.reply_when("'get-url'", CannedReply())
    fake_rayd.process.reply_when("git", CannedReply())
    assert sandbox.git.get_config("core.editor", scope="local", path="/repo") == "true"
    assert sandbox.git.remote_get("/repo", "origin") is None
    sandbox.git.configure_user("Ana", "ana@example.com")
    sandbox.git.remote_add("/repo", "origin", REMOTE_URL, overwrite=True)
    assert fake_rayd.process.commands() == [
        "'git' '-C' '/repo' 'config' '--local' '--get' 'core.editor' || true",
        "'git' '-C' '/repo' 'remote' 'get-url' 'origin' || true",
        "'git' 'config' '--global' 'user.name' 'Ana'",
        "'git' 'config' '--global' 'user.email' 'ana@example.com'",
        f"'git' '-C' '/repo' 'remote' 'add' 'origin' '{REMOTE_URL}' || "
        f"'git' '-C' '/repo' 'remote' 'set-url' 'origin' '{REMOTE_URL}'",
    ]
    with pytest.raises(InvalidArgumentException):
        sandbox.git.set_config("core.editor", "vim", scope="local")


def test_git_emits_no_log_records(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    reply_with_a_remote(fake_rayd, CannedReply())
    fake_rayd.process.reply_when("git", CannedReply())
    with capture_logs("rayito") as logs:
        sandbox.git.push("/repo", username="u", password=PASSWORD)
        sandbox.git.dangerously_authenticate("u", PASSWORD)
        sandbox.git.status("/repo")
    assert logs.records == []
