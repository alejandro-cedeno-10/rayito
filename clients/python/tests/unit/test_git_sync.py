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


ISOLATION = "'-c' 'core.hooksPath=/dev/null' '-c' 'credential.helper='"
REWRITE_CHECK = "'config' '--get-regexp' '^url\\..*\\.(push)?insteadof$'"
RESTORE = f"'git' '-C' '/repo' 'remote' 'set-url' 'origin' '{REMOTE_URL}'"


def no_url_rewrites(fake_rayd: RaydEndpoint) -> None:
    fake_rayd.process.reply_when("'--get-regexp'", CannedReply(exit_code=1))


def reply_with_a_remote(fake_rayd: RaydEndpoint, push: CannedReply) -> None:
    no_url_rewrites(fake_rayd)
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
        f"'git' '-C' '/repo' {REWRITE_CHECK}",
        "'git' '-C' '/repo' 'remote' 'get-url' 'origin'",
        f"'git' '-C' '/repo' 'remote' 'set-url' 'origin' 'https://u:{ENCODED_PASSWORD}@github.com/o/r.git'",
        f"'git' '-C' '/repo' {ISOLATION} 'push' '--set-upstream' 'origin'",
        RESTORE,
    ]
    assert excinfo.value.exit_code == 128


def test_pull_with_credentials_uses_the_resolved_remote(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    reply_with_a_remote(fake_rayd, CannedReply(stdout="Already up to date.\n"))
    result = sandbox.git.pull("/repo", branch="main", username="u", password=PASSWORD)
    assert result.stdout == "Already up to date.\n"
    commands = fake_rayd.process.commands()
    assert commands[4] == f"'git' '-C' '/repo' {ISOLATION} 'pull' 'origin' 'main'"
    assert commands[-1] == RESTORE


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
    no_url_rewrites(fake_rayd)
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
    no_url_rewrites(fake_rayd)
    fake_rayd.process.reply_when(
        "'clone'", CannedReply(stderr=f"fatal: Authentication failed {PASSWORD}\n", exit_code=128)
    )
    with pytest.raises(GitAuthException) as excinfo:
        sandbox.git.clone(REMOTE_URL, "/home/user/r", username="u", password=PASSWORD)
    assert excinfo.value.__cause__ is None and excinfo.value.__context__ is None


def test_successful_credentialed_clone_strips_the_origin(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    no_url_rewrites(fake_rayd)
    fake_rayd.process.reply_when("'git'", CannedReply())
    sandbox.git.clone(REMOTE_URL, username="u", password=PASSWORD, depth=1)
    assert fake_rayd.process.commands() == [
        f"'git' {REWRITE_CHECK}",
        f"'git' {ISOLATION} 'clone' 'https://u:{ENCODED_PASSWORD}@github.com/o/r.git' "
        "'--depth' '1'",
        f"'git' '-C' 'r' 'remote' 'set-url' 'origin' '{REMOTE_URL}'",
    ]


def test_an_anonymous_clone_is_not_isolated_nor_checked(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when("'git'", CannedReply())
    sandbox.git.clone(REMOTE_URL, "/home/user/r")
    assert fake_rayd.process.commands() == [f"'git' 'clone' '{REMOTE_URL}' '/home/user/r'"]


def test_a_url_rewrite_in_the_git_config_refuses_the_credentials(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    """Código del sandbox con `url."http://127.0.0.1:9999/".insteadOf
    https://` en `~/.gitconfig` recibiría la URL con el token en su propio
    listener: con una reescritura así no se envía nada."""
    fake_rayd.process.reply_when(
        "'--get-regexp'", CannedReply(stdout="url.http://127.0.0.1:9999/.insteadof https://\n")
    )
    fake_rayd.process.reply_when("'git'", CannedReply())
    with pytest.raises(GitAuthException, match="insteadOf"):
        sandbox.git.clone(REMOTE_URL, "/home/user/r", username="u", password=PASSWORD)
    with pytest.raises(GitAuthException, match="insteadOf"):
        sandbox.git.push("/repo", remote="origin", username="u", password=PASSWORD)
    sent = " ".join(fake_rayd.process.commands())
    assert ENCODED_PASSWORD not in sent and PASSWORD not in sent


def test_a_restore_that_times_out_keeps_the_push_error_and_warns(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    """Antes, sólo se suprimía `CommandExitException` al restaurar: un
    timeout ocultaba el error del push y no avisaba de que el token podía
    seguir en `.git/config`."""
    fake_rayd.process.reply_when(f"'set-url' 'origin' '{REMOTE_URL}'", CannedReply(times_out=True))
    reply_with_a_remote(fake_rayd, CannedReply(stderr="error: rejected\n", exit_code=128))
    with capture_logs("rayito") as logs, pytest.raises(CommandExitException) as excinfo:
        sandbox.git.push("/repo", username="u", password=PASSWORD)
    assert excinfo.value.exit_code == 128
    assert fake_rayd.process.commands()[-1] == RESTORE
    assert "git push" in logs.text() and ".git/config" in logs.text()
    assert PASSWORD not in logs.text() and ENCODED_PASSWORD not in logs.text()
    assert REMOTE_URL not in logs.text() and "/repo" not in logs.text()


def test_a_restore_that_times_out_after_a_successful_pull_raises_and_warns(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when(f"'set-url' 'origin' '{REMOTE_URL}'", CannedReply(times_out=True))
    reply_with_a_remote(fake_rayd, CannedReply(stdout="ok\n"))
    with capture_logs("rayito") as logs, pytest.raises(TimeoutException):
        sandbox.git.pull("/repo", remote="origin", username="u", password=PASSWORD)
    assert "git pull" in logs.text()


def test_a_credentialed_set_url_that_times_out_is_still_restored(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    """El `set-url` con credenciales puede haberse aplicado aunque la orden
    venza: también entonces se intenta devolver la URL original."""
    fake_rayd.process.reply_when("'set-url' 'origin' 'https://u:", CannedReply(times_out=True))
    reply_with_a_remote(fake_rayd, CannedReply())
    with pytest.raises(TimeoutException):
        sandbox.git.push("/repo", remote="origin", username="u", password=PASSWORD)
    assert fake_rayd.process.commands()[-1] == RESTORE


def test_a_clone_that_times_out_still_tries_to_strip_the_origin(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    no_url_rewrites(fake_rayd)
    fake_rayd.process.reply_when("'clone'", CannedReply(times_out=True))
    fake_rayd.process.reply_when("'set-url'", CannedReply())
    with pytest.raises(TimeoutException):
        sandbox.git.clone(REMOTE_URL, "/home/user/r", username="u", password=PASSWORD)
    assert fake_rayd.process.commands()[-1] == (
        f"'git' '-C' '/home/user/r' 'remote' 'set-url' 'origin' '{REMOTE_URL}'"
    )


def test_a_clone_that_exits_non_zero_does_not_touch_the_destination(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    """Con un exit distinto de cero git ya borró lo que creó (o el destino
    existía de antes y no es suyo): no se toca."""
    no_url_rewrites(fake_rayd)
    fake_rayd.process.reply_when("'clone'", CannedReply(stderr="fatal: exists\n", exit_code=128))
    with pytest.raises(CommandExitException):
        sandbox.git.clone(REMOTE_URL, "/home/user/r", username="u", password=PASSWORD)
    assert not any("'set-url'" in command for command in fake_rayd.process.commands())


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
