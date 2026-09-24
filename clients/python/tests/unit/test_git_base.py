"""Núcleo puro del módulo git: argv idénticos a E2B, parsers porcelain,
credenciales, redacción y clasificación de fallos."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from rayito._git_base import (
    AUTH_FAILURE_SNIPPETS,
    GIT_ENV,
    MISSING_UPSTREAM_SNIPPETS,
    FailurePolicy,
    GitCommandOptions,
    add_args,
    branches_args,
    build_clone_plan,
    checkout_branch_args,
    commit_args,
    config_get_command,
    create_branch_args,
    credential_approve_command,
    credential_secrets,
    delete_branch_args,
    derive_repo_dir_from_url,
    git_command,
    git_failure,
    has_upstream_args,
    init_args,
    is_auth_failure,
    is_missing_upstream,
    parse_git_branches,
    parse_git_status,
    parse_remote_url,
    pull_args,
    push_args,
    redact,
    remote_add_args,
    remote_add_overwrite_command,
    remote_get_command,
    reset_args,
    resolve_config_scope,
    resolve_remote_name,
    restore_args,
    shell_quote,
    status_args,
    strip_credentials,
    with_credentials,
)
from rayito.exceptions import (
    AuthenticationException,
    CommandExitException,
    GitAuthException,
    GitUpstreamException,
    InvalidArgumentException,
)


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (status_args, ["status", "--porcelain=1", "-b"]),
        (branches_args, ["branch", "--format=%(refname:short)\t%(HEAD)"]),
        (lambda: create_branch_args("feat"), ["checkout", "-b", "feat"]),
        (lambda: checkout_branch_args("main"), ["checkout", "main"]),
        (lambda: delete_branch_args("old", False), ["branch", "-d", "old"]),
        (lambda: delete_branch_args("old", True), ["branch", "-D", "old"]),
        (lambda: add_args(None, True), ["add", "-A"]),
        (lambda: add_args([], False), ["add", "."]),
        (lambda: add_args(["a.txt", "b c"], True), ["add", "--", "a.txt", "b c"]),
        (lambda: commit_args("msg", None, None, False), ["commit", "-m", "msg"]),
        (lambda: commit_args("msg", None, None, True), ["commit", "-m", "msg", "--allow-empty"]),
        (
            lambda: commit_args("msg", "Ana", "ana@example.com", False),
            ["-c", "user.name=Ana", "-c", "user.email=ana@example.com", "commit", "-m", "msg"],
        ),
        (lambda: reset_args(None, None, None), ["reset"]),
        (lambda: reset_args("soft", "HEAD~1", None), ["reset", "--soft", "HEAD~1"]),
        (lambda: reset_args("hard", None, ["a", "b"]), ["reset", "--hard", "--", "a", "b"]),
        (lambda: restore_args(["f"], None, None, None), ["restore", "--worktree", "--", "f"]),
        (lambda: restore_args(["f"], True, None, None), ["restore", "--staged", "--", "f"]),
        (lambda: restore_args(["f"], None, True, None), ["restore", "--worktree", "--", "f"]),
        (
            lambda: restore_args(["f"], True, True, "HEAD~2"),
            ["restore", "--worktree", "--staged", "--source", "HEAD~2", "--", "f"],
        ),
        (lambda: init_args("/r", False, None), ["init", "/r"]),
        (
            lambda: init_args("/r.git", True, "main"),
            ["init", "--initial-branch", "main", "--bare", "/r.git"],
        ),
        (lambda: remote_add_args("origin", "u", False), ["remote", "add", "origin", "u"]),
        (lambda: remote_add_args("origin", "u", True), ["remote", "add", "-f", "origin", "u"]),
        (
            has_upstream_args,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        ),
        (lambda: push_args(None, remote=None, branch=None, set_upstream=True), ["push"]),
        (
            lambda: push_args(None, remote="origin", branch="main", set_upstream=True),
            ["push", "--set-upstream", "origin", "main"],
        ),
        (
            lambda: push_args("origin", remote=None, branch=None, set_upstream=False),
            ["push", "origin"],
        ),
        (lambda: pull_args(None, None), ["pull"]),
        (lambda: pull_args("origin", "main"), ["pull", "origin", "main"]),
        (lambda: pull_args(None, "main", "upstream"), ["pull", "upstream", "main"]),
    ],
)
def test_argv_matches_e2b(build: Callable[[], list[str]], expected: list[str]) -> None:
    assert build() == expected


def test_shell_quote_survives_single_quotes() -> None:
    assert shell_quote("it's") == "'it'\"'\"'s'"
    assert shell_quote("") == "''"


def test_git_command_quotes_every_part_and_uses_dash_c() -> None:
    assert git_command(status_args(), "/repo") == "'git' '-C' '/repo' 'status' '--porcelain=1' '-b'"
    assert git_command(["init", "/r"]) == "'git' 'init' '/r'"


def test_remote_helper_commands() -> None:
    assert (
        remote_get_command("/r", "origin") == "'git' '-C' '/r' 'remote' 'get-url' 'origin' || true"
    )
    add = "'git' '-C' '/r' 'remote' 'add' 'o' 'u'"
    set_url = "'git' '-C' '/r' 'remote' 'set-url' 'o' 'u'"
    assert remote_add_overwrite_command("/r", "o", "u", False) == f"{add} || {set_url}"
    fetching = remote_add_overwrite_command("/r", "o", "u", True)
    assert fetching == (
        f"('git' '-C' '/r' 'remote' 'add' '-f' 'o' 'u' || {set_url}) && 'git' '-C' '/r' 'fetch' 'o'"
    )
    with pytest.raises(InvalidArgumentException):
        remote_get_command("/r", "")
    with pytest.raises(InvalidArgumentException):
        remote_add_args("", "u", False)


def test_config_scope_resolution() -> None:
    assert resolve_config_scope("global", None) == ("--global", None)
    assert resolve_config_scope(" System ", "/ignored") == ("--system", None)
    assert resolve_config_scope("local", "/repo") == ("--local", "/repo")
    with pytest.raises(InvalidArgumentException, match="path"):
        resolve_config_scope("local", None)
    with pytest.raises(InvalidArgumentException):
        resolve_config_scope("worktree", None)
    assert config_get_command("--local", "core.editor", "/repo") == (
        "'git' '-C' '/repo' 'config' '--local' '--get' 'core.editor' || true"
    )


def test_credential_approve_pipes_the_four_lines() -> None:
    command = credential_approve_command("ana", "t0k'en", "", "")
    lines = "protocol=https\nhost=github.com\nusername=ana\npassword=t0k'en\n\n"
    assert command == f"printf %s {shell_quote(lines)} | 'git' 'credential' 'approve'"


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: reset_args("bogus", None, None), "modo"),
        (lambda: restore_args([], None, None, None), "ruta"),
        (lambda: restore_args(["f"], False, False, None), "staged o worktree"),
        (lambda: restore_args(["f"], False, None, None), "staged o worktree"),
    ],
)
def test_invalid_arguments_are_refused(build: Callable[[], list[str]], message: str) -> None:
    with pytest.raises(InvalidArgumentException, match=message):
        build()


CLEAN = "## main...origin/main\n"
AHEAD_BEHIND = "## main...origin/main [ahead 2, behind 1]\n M src/app.py\n"
DETACHED = "## HEAD (no branch)\n"
DETACHED_AT = "## HEAD (detached at 1a2b3c4)\n"
UNBORN = "## No commits yet on main\n?? README.md\n"
RENAMED = "## main\nR  old.txt -> new.txt\n"
CONFLICT = (
    "## main\nUU merge.txt\nAA both.txt\nDU gone.txt\nDD d.txt\nAU au.txt\nUD ud.txt\nUA ua.txt\n"
)
UNTRACKED = "## main\n?? notes.txt\nA  added.txt\n M changed.txt\n"


def test_porcelain_clean() -> None:
    status = parse_git_status(CLEAN)
    assert (status.current_branch, status.upstream) == ("main", "origin/main")
    assert status.is_clean and not status.has_changes and status.total_count == 0
    assert (status.ahead, status.behind, status.detached) == (0, 0, False)


def test_porcelain_ahead_and_behind() -> None:
    status = parse_git_status(AHEAD_BEHIND)
    assert (status.ahead, status.behind) == (2, 1)
    assert status.file_status[0].name == "src/app.py"
    assert status.file_status[0].status == "modified"
    assert status.unstaged_count == 1 and status.staged_count == 0


@pytest.mark.parametrize("output", [DETACHED, DETACHED_AT])
def test_porcelain_detached(output: str) -> None:
    status = parse_git_status(output)
    assert status.detached is True
    assert status.current_branch is None


def test_porcelain_unborn_branch() -> None:
    status = parse_git_status(UNBORN)
    assert status.current_branch == "main"
    assert status.upstream is None
    assert status.has_untracked and status.untracked_count == 1


def test_porcelain_rename() -> None:
    entry = parse_git_status(RENAMED).file_status[0]
    assert (entry.name, entry.renamed_from, entry.status, entry.staged) == (
        "new.txt",
        "old.txt",
        "renamed",
        True,
    )


def test_porcelain_conflicts() -> None:
    status = parse_git_status(CONFLICT)
    assert status.has_conflicts
    assert status.conflict_count == 7
    assert [entry.status for entry in status.file_status] == ["conflict"] * 7


def test_porcelain_untracked_staged_and_unstaged() -> None:
    status = parse_git_status(UNTRACKED)
    assert status.untracked_count == 1
    assert status.has_staged and status.staged_count == 1
    assert status.unstaged_count == 2
    assert [entry.status for entry in status.file_status] == ["untracked", "added", "modified"]


def test_porcelain_empty_output() -> None:
    status = parse_git_status("")
    assert status.current_branch is None and status.is_clean


def test_branches_listing() -> None:
    branches = parse_git_branches("feature\t \nmain\t*\n\n")
    assert branches.branches == ["feature", "main"]
    assert branches.current_branch == "main"
    assert parse_git_branches("").current_branch is None


def exit_failure(
    stderr: str = "", stdout: str = "", message: str = "falló"
) -> CommandExitException:
    return CommandExitException(
        message, exit_code=128, stdout=stdout, stderr=stderr, error="exited"
    )


@pytest.mark.parametrize("snippet", AUTH_FAILURE_SNIPPETS)
def test_auth_snippets(snippet: str) -> None:
    assert is_auth_failure(exit_failure(stderr=f"fatal: {snippet.upper()}"))
    assert not is_missing_upstream(exit_failure(stderr=f"fatal: {snippet}"))


@pytest.mark.parametrize("snippet", MISSING_UPSTREAM_SNIPPETS)
def test_upstream_snippets(snippet: str) -> None:
    assert is_missing_upstream(exit_failure(stdout=snippet))


def test_classifiers_ignore_other_exceptions() -> None:
    assert not is_auth_failure(RuntimeError("authentication failed"))
    assert not is_missing_upstream(RuntimeError("no upstream branch"))


def test_with_credentials_percent_encodes() -> None:
    url = with_credentials("https://github.com/o/r.git", "ana@corp", "p:ss/w@rd")
    assert url == "https://ana%40corp:p%3Ass%2Fw%40rd@github.com/o/r.git"
    assert strip_credentials(url) == "https://github.com/o/r.git"
    assert with_credentials("https://h/r", None, None) == "https://h/r"
    with pytest.raises(InvalidArgumentException):
        with_credentials("https://h/r", "ana", None)
    with pytest.raises(InvalidArgumentException, match="http"):
        with_credentials("git@github.com:o/r.git", "ana", "x")


def test_strip_credentials_keeps_ports_and_non_http() -> None:
    assert strip_credentials("https://u:p@h:8443/r") == "https://h:8443/r"
    assert strip_credentials("ssh://u@h/r") == "ssh://u@h/r"
    assert strip_credentials("https://h/r") == "https://h/r"


def test_redact_removes_raw_and_encoded_passwords() -> None:
    secrets = credential_secrets("p:ss")
    assert secrets == ("p:ss", "p%3Ass")
    text = "fatal: https://ana:p%3Ass@h/r and p:ss"
    assert redact(text, secrets) == "fatal: https://ana:***@h/r and ***"
    assert credential_secrets(None) == ()
    assert credential_secrets("plain") == ("plain",)


def test_repo_dir_derivation() -> None:
    assert derive_repo_dir_from_url("https://github.com/octocat/Hello-World.git") == "Hello-World"
    assert derive_repo_dir_from_url("https://h/o/repo/") == "repo"
    assert derive_repo_dir_from_url("https://h") is None
    assert derive_repo_dir_from_url("git@github.com:o/r.git") is None


def test_clone_plan_with_credentials_strips_them_afterwards() -> None:
    plan = build_clone_plan("https://h/o/repo.git", None, "dev", 1, "ana", "s3cr3t", False)
    assert plan.args == (
        "clone",
        "https://ana:s3cr3t@h/o/repo.git",
        "--branch",
        "dev",
        "--single-branch",
        "--depth",
        "1",
    )
    assert (plan.repo_path, plan.sanitized_url, plan.should_strip) == (
        "repo",
        "https://h/o/repo.git",
        True,
    )


def test_clone_plan_without_credentials_or_when_storing_them() -> None:
    plain = build_clone_plan("https://h/o/r.git", "/dst", None, None, None, None, False)
    assert plain.args == ("clone", "https://h/o/r.git", "/dst")
    assert not plain.should_strip and plain.sanitized_url is None
    stored = build_clone_plan("https://h/o/r.git", None, None, None, "a", "b", True)
    assert stored.args == ("clone", "https://a:b@h/o/r.git")
    assert not stored.should_strip


def test_clone_plan_needs_a_path_when_it_cannot_be_derived() -> None:
    with pytest.raises(InvalidArgumentException, match="path"):
        build_clone_plan("https://h", None, None, None, "ana", "s3cr3t", False)


def test_remote_resolution() -> None:
    assert resolve_remote_name("up", "") == "up"
    assert resolve_remote_name(None, "origin\n") == "origin"
    assert resolve_remote_name(None, "fork\norigin\n") == "origin"
    with pytest.raises(InvalidArgumentException):
        resolve_remote_name(None, "a\nb\n")
    assert parse_remote_url(" https://h/r \n", "origin") == "https://h/r"
    with pytest.raises(InvalidArgumentException, match="origin"):
        parse_remote_url("\n", "origin")


def test_git_env_goes_under_the_caller_envs() -> None:
    assert dict(GIT_ENV) == {"GIT_TERMINAL_PROMPT": "0"}
    assert GitCommandOptions().merged_envs() == {"GIT_TERMINAL_PROMPT": "0"}
    options = GitCommandOptions(envs={"GIT_TERMINAL_PROMPT": "1", "A": "b"})
    assert options.merged_envs() == {"GIT_TERMINAL_PROMPT": "1", "A": "b"}


def test_failure_policy_classification() -> None:
    auth = exit_failure(stderr="fatal: could not read Username for 'https://h': terminal")
    mapped = git_failure(auth, FailurePolicy("push", remote=True, upstream=True))
    assert isinstance(mapped, GitAuthException)
    assert isinstance(mapped, AuthenticationException)
    assert str(mapped) == "git push necesita credenciales para repositorios privados"
    token = git_failure(auth, FailurePolicy("clone", remote=True, missing_password=True))
    assert "password/token" in str(token)
    upstream = exit_failure(stderr="There is no tracking information for the current branch.")
    assert isinstance(
        git_failure(upstream, FailurePolicy("pull", upstream=True)), GitUpstreamException
    )
    local = exit_failure(stderr="error: open('x'): Permission denied")
    assert git_failure(local, FailurePolicy("status")) is local


def test_failure_policy_redacts_every_field() -> None:
    leaked = exit_failure(stderr="fatal: https://a:s3cr3t@h", stdout="s3cr3t", message="x s3cr3t")
    redacted = git_failure(leaked, FailurePolicy("clone", secrets=("s3cr3t",)))
    assert isinstance(redacted, CommandExitException)
    assert redacted is not leaked
    assert "s3cr3t" not in f"{redacted} {redacted.stderr} {redacted.stdout} {redacted.error}"
    assert redacted.exit_code == 128
