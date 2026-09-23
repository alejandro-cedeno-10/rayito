"""`Sandbox.git`: el módulo git de E2B 2.x como envoltorio de
`commands.run` (cada operación es un `git ...` en foreground dentro del
sandbox, con `GIT_TERMINAL_PROMPT=0`). No hay RPC propio ni cambio en el
agente, y el módulo no emite ningún registro de log: el comando, que lleva
las credenciales cuando se pasan, sólo viaja dentro de `StartRequest`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, TypeVar

from rayito._git_base import (
    FailurePolicy,
    GitBranches,
    GitCommandOptions,
    GitResetMode,
    GitStatus,
    add_args,
    branches_args,
    build_clone_plan,
    checkout_branch_args,
    commit_args,
    config_get_command,
    config_set_args,
    create_branch_args,
    credential_approve_command,
    credential_secrets,
    delete_branch_args,
    git_command,
    git_failure,
    has_upstream_args,
    init_args,
    parse_git_branches,
    parse_git_status,
    parse_remote_url,
    pull_args,
    push_args,
    remote_add_args,
    remote_add_overwrite_command,
    remote_get_command,
    remote_get_url_args,
    remote_list_args,
    remote_set_url_args,
    require_both_credentials,
    require_config_key,
    require_user_identity,
    require_username_for_password,
    reset_args,
    resolve_config_scope,
    resolve_remote_name,
    restore_args,
    status_args,
    upstream_error_message,
    with_credentials,
)
from rayito._models import CommandResult
from rayito.exceptions import CommandExitException, GitUpstreamException

if TYPE_CHECKING:
    from rayito.sandbox_sync.commands import Commands

T = TypeVar("T")


class Git:
    """Operaciones git en el sandbox con la superficie de E2B 2.51
    (`sandbox.git`). E2B marca su módulo como obsoleto a favor de
    `commands.run()`; aquí se mantiene por paridad.

    `timeout=None` (por defecto) es sin plazo de servidor. Un fallo de
    autenticación contra un remoto es `GitAuthException`, un push/pull sin
    upstream `GitUpstreamException` y cualquier otro fallo el
    `CommandExitException` del comando, sin la contraseña cuando la llamada
    llevaba credenciales.
    """

    def __init__(self, commands: Commands) -> None:
        self._commands = commands

    def clone(
        self,
        url: str,
        path: str | None = None,
        branch: str | None = None,
        depth: int | None = None,
        username: str | None = None,
        password: str | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
        dangerously_store_credentials: bool = False,
    ) -> CommandResult:
        """`git clone`. Con `username` y `password` clona desde la URL con
        las credenciales y, salvo `dangerously_store_credentials=True`,
        deja `origin` apuntando a la URL sin ellas."""
        require_username_for_password(username, password, "clone")
        plan = build_clone_plan(
            url, path, branch, depth, username, password, dangerously_store_credentials
        )
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        policy = FailurePolicy(
            "clone",
            remote=True,
            missing_password=bool(username) and not password,
            secrets=credential_secrets(password),
        )

        def clone_and_strip() -> CommandResult:
            result = self._run(plan.args, None, options)
            if plan.should_strip and plan.repo_path and plan.sanitized_url:
                self._run(
                    remote_set_url_args("origin", plan.sanitized_url), plan.repo_path, options
                )
            return result

        return self._guarded(clone_and_strip, policy)

    def init(
        self,
        path: str,
        bare: bool = False,
        initial_branch: str | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandResult:
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        return self._run(init_args(path, bare, initial_branch), None, options)

    def remote_add(
        self,
        path: str,
        name: str,
        url: str,
        fetch: bool = False,
        overwrite: bool = False,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandResult:
        """`git remote add [-f]`; con `overwrite` cae a `remote set-url` si
        el remoto ya existe."""
        args = remote_add_args(name, url, fetch)
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        policy = FailurePolicy("remote_add", remote=fetch)
        if not overwrite:
            return self._guarded(lambda: self._run(args, path, options), policy)
        command = remote_add_overwrite_command(path, name, url, fetch)
        return self._guarded(lambda: self._shell(command, options), policy)

    def remote_get(
        self,
        path: str,
        name: str,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> str | None:
        """La URL del remoto, o `None` si no existe."""
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        output = self._shell(remote_get_command(path, name), options).stdout.strip()
        return output or None

    def status(
        self,
        path: str,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> GitStatus:
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        return parse_git_status(self._run(status_args(), path, options).stdout)

    def branches(
        self,
        path: str,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> GitBranches:
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        return parse_git_branches(self._run(branches_args(), path, options).stdout)

    def create_branch(
        self,
        path: str,
        branch: str,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandResult:
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        return self._run(create_branch_args(branch), path, options)

    def checkout_branch(
        self,
        path: str,
        branch: str,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandResult:
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        return self._run(checkout_branch_args(branch), path, options)

    def delete_branch(
        self,
        path: str,
        branch: str,
        force: bool = False,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandResult:
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        return self._run(delete_branch_args(branch, force), path, options)

    def add(
        self,
        path: str,
        files: Sequence[str] | None = None,
        all: bool = True,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandResult:
        """`git add -- <files>`; sin `files`, `-A` (o `.` con `all=False`)."""
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        return self._run(add_args(files, all), path, options)

    def commit(
        self,
        path: str,
        message: str,
        author_name: str | None = None,
        author_email: str | None = None,
        allow_empty: bool = False,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandResult:
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        args = commit_args(message, author_name, author_email, allow_empty)
        return self._run(args, path, options)

    def reset(
        self,
        path: str,
        mode: GitResetMode | None = None,
        target: str | None = None,
        paths: Sequence[str] | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandResult:
        """Un `mode` fuera de soft/mixed/hard/merge/keep es
        `InvalidArgumentException` sin ejecutar nada."""
        args = reset_args(mode, target, paths)
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        return self._run(args, path, options)

    def restore(
        self,
        path: str,
        paths: Sequence[str],
        staged: bool | None = None,
        worktree: bool | None = None,
        source: str | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandResult:
        """Sin `staged` ni `worktree` restaura el árbol de trabajo;
        `staged=True` solo saca del índice. Una lista vacía es
        `InvalidArgumentException` sin ejecutar nada."""
        args = restore_args(paths, staged, worktree, source)
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        return self._run(args, path, options)

    def push(
        self,
        path: str,
        remote: str | None = None,
        branch: str | None = None,
        set_upstream: bool = True,
        username: str | None = None,
        password: str | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandResult:
        """`git push`. Con credenciales: `remote get-url`, `remote set-url`
        con la URL con credenciales, el push y, pase lo que pase, `remote
        set-url` con la URL original."""
        require_username_for_password(username, password, "push")
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        policy = FailurePolicy(
            "push",
            remote=True,
            upstream=True,
            missing_password=bool(username) and not password,
            secrets=credential_secrets(password),
        )
        if not (username and password):
            args = push_args(None, remote=remote, branch=branch, set_upstream=set_upstream)
            return self._guarded(lambda: self._run(args, path, options), policy)

        def push_with_credentials() -> CommandResult:
            remote_name = self._remote_name(path, remote, options)
            args = push_args(remote_name, remote=remote, branch=branch, set_upstream=set_upstream)
            return self._with_remote_credentials(
                path,
                remote_name,
                username,
                password,
                options,
                lambda: self._run(args, path, options),
            )

        return self._guarded(push_with_credentials, policy)

    def pull(
        self,
        path: str,
        remote: str | None = None,
        branch: str | None = None,
        username: str | None = None,
        password: str | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandResult:
        """`git pull`. Sin `remote` ni `branch` exige una rama con upstream
        (`GitUpstreamException` si no la hay); las credenciales se inyectan y
        se retiran como en `push`."""
        require_username_for_password(username, password, "pull")
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        if not remote and not branch and not self._has_upstream(path, options):
            raise GitUpstreamException(upstream_error_message("pull"))
        policy = FailurePolicy(
            "pull",
            remote=True,
            upstream=True,
            missing_password=bool(username) and not password,
            secrets=credential_secrets(password),
        )
        if not (username and password):
            args = pull_args(remote, branch)
            return self._guarded(lambda: self._run(args, path, options), policy)

        def pull_with_credentials() -> CommandResult:
            remote_name = self._remote_name(path, remote, options)
            args = pull_args(remote, branch, remote_name)
            return self._with_remote_credentials(
                path,
                remote_name,
                username,
                password,
                options,
                lambda: self._run(args, path, options),
            )

        return self._guarded(pull_with_credentials, policy)

    def set_config(
        self,
        key: str,
        value: str,
        scope: str = "global",
        path: str | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandResult:
        """`git config --global|--local|--system`; `scope="local"` necesita `path`."""
        scope_flag, repo_path = resolve_config_scope(scope, path)
        args = config_set_args(scope_flag, require_config_key(key), value)
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        return self._run(args, repo_path, options)

    def get_config(
        self,
        key: str,
        scope: str = "global",
        path: str | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> str | None:
        """El valor, o `None` si la clave no está en ese scope."""
        scope_flag, repo_path = resolve_config_scope(scope, path)
        command = config_get_command(scope_flag, require_config_key(key), repo_path)
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        return self._shell(command, options).stdout.strip() or None

    def dangerously_authenticate(
        self,
        username: str,
        password: str,
        host: str = "github.com",
        protocol: str = "https",
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandResult:
        """Autentica git para todo el sandbox con el helper `store`:
        `credential.helper=store` global y `git credential approve`.

        **Las credenciales quedan en `~/.git-credentials` del usuario del
        sandbox y son visibles para cualquier código que corra en él**, igual
        que en E2B. Usa tokens de vida corta. El SDK nunca las loguea."""
        require_both_credentials(username, password)
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        self.set_config(
            "credential.helper",
            "store",
            scope="global",
            envs=envs,
            user=user,
            cwd=cwd,
            timeout=timeout,
            request_timeout=request_timeout,
        )
        command = credential_approve_command(username, password, host, protocol)
        policy = FailurePolicy("credential", secrets=credential_secrets(password))
        return self._guarded(lambda: self._shell(command, options), policy)

    def configure_user(
        self,
        name: str,
        email: str,
        scope: str = "global",
        path: str | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> CommandResult:
        """`user.name` y `user.email` en el scope pedido."""
        require_user_identity(name, email)
        self.set_config("user.name", name, scope, path, envs, user, cwd, timeout, request_timeout)
        return self.set_config(
            "user.email", email, scope, path, envs, user, cwd, timeout, request_timeout
        )

    def _run(
        self, args: Sequence[str], repo_path: str | None, options: GitCommandOptions
    ) -> CommandResult:
        return self._shell(git_command(args, repo_path), options)

    def _shell(self, command: str, options: GitCommandOptions) -> CommandResult:
        return self._commands.run(
            command,
            envs=options.merged_envs(),
            user=options.user,
            cwd=options.cwd,
            timeout=options.timeout,
            request_timeout=options.request_timeout,
        )

    def _has_upstream(self, path: str, options: GitCommandOptions) -> bool:
        try:
            return bool(self._run(has_upstream_args(), path, options).stdout.strip())
        except CommandExitException:
            return False

    def _remote_name(self, path: str, remote: str | None, options: GitCommandOptions) -> str:
        if remote:
            return remote
        return resolve_remote_name(None, self._run(remote_list_args(), path, options).stdout)

    def _with_remote_credentials(
        self,
        path: str,
        remote: str,
        username: str,
        password: str,
        options: GitCommandOptions,
        operation: Callable[[], T],
    ) -> T:
        """La URL original se restaura siempre. Si la operación falla, su
        error es el que sale aunque la restauración también falle (como en
        E2B); si sólo falla la restauración, sale ese error."""
        original = parse_remote_url(
            self._run(remote_get_url_args(remote), path, options).stdout, remote
        )
        credentialed = with_credentials(original, username, password)
        restore = remote_set_url_args(remote, original)
        self._run(remote_set_url_args(remote, credentialed), path, options)
        try:
            result = operation()
        except BaseException:
            with contextlib.suppress(CommandExitException):
                self._run(restore, path, options)
            raise
        self._run(restore, path, options)
        return result

    def _guarded(self, operation: Callable[[], T], policy: FailurePolicy) -> T:
        """Traduce el `CommandExitException` según `policy`. Con secretos de
        por medio la excepción se lanza fuera del `except` y `from None`, así
        que ni `__cause__` ni `__context__` guardan el texto sin redactar."""
        try:
            return operation()
        except CommandExitException as exc:
            failure = git_failure(exc, policy)
            if failure is exc:
                raise
            if not policy.secrets:
                raise failure from exc
        raise failure from None


__all__ = ["Git"]
