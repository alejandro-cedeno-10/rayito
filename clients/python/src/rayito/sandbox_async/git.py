"""`AsyncSandbox.git`: la misma superficie que `sandbox_sync.git` como
corrutinas sobre `AsyncCommands`, con los mismos helpers puros de
`rayito._git_base`. Tampoco emite registros de log."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
    from rayito.sandbox_async.commands import AsyncCommands

T = TypeVar("T")


class AsyncGit:
    """`Git` como corrutinas (la clase `Git` del SDK async de E2B 2.51). E2B
    marca su módulo como obsoleto a favor de `commands.run()`; aquí se
    mantiene por paridad.

    `timeout=None` (por defecto) es sin plazo de servidor. Un fallo de
    autenticación contra un remoto es `GitAuthException`, un push/pull sin
    upstream `GitUpstreamException` y cualquier otro fallo el
    `CommandExitException` del comando, sin la contraseña cuando la llamada
    llevaba credenciales.
    """

    def __init__(self, commands: AsyncCommands) -> None:
        self._commands = commands

    async def clone(
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

        async def clone_and_strip() -> CommandResult:
            result = await self._run(plan.args, None, options)
            if plan.should_strip and plan.repo_path and plan.sanitized_url:
                await self._run(
                    remote_set_url_args("origin", plan.sanitized_url), plan.repo_path, options
                )
            return result

        return await self._guarded(clone_and_strip, policy)

    async def init(
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
        return await self._run(init_args(path, bare, initial_branch), None, options)

    async def remote_add(
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
            return await self._guarded(lambda: self._run(args, path, options), policy)
        command = remote_add_overwrite_command(path, name, url, fetch)
        return await self._guarded(lambda: self._shell(command, options), policy)

    async def remote_get(
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
        output = (await self._shell(remote_get_command(path, name), options)).stdout.strip()
        return output or None

    async def status(
        self,
        path: str,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> GitStatus:
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        return parse_git_status((await self._run(status_args(), path, options)).stdout)

    async def branches(
        self,
        path: str,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> GitBranches:
        options = GitCommandOptions(envs, user, cwd, timeout, request_timeout)
        return parse_git_branches((await self._run(branches_args(), path, options)).stdout)

    async def create_branch(
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
        return await self._run(create_branch_args(branch), path, options)

    async def checkout_branch(
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
        return await self._run(checkout_branch_args(branch), path, options)

    async def delete_branch(
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
        return await self._run(delete_branch_args(branch, force), path, options)

    async def add(
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
        return await self._run(add_args(files, all), path, options)

    async def commit(
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
        return await self._run(args, path, options)

    async def reset(
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
        return await self._run(args, path, options)

    async def restore(
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
        return await self._run(args, path, options)

    async def push(
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
            return await self._guarded(lambda: self._run(args, path, options), policy)

        async def push_with_credentials() -> CommandResult:
            remote_name = await self._remote_name(path, remote, options)
            args = push_args(remote_name, remote=remote, branch=branch, set_upstream=set_upstream)
            return await self._with_remote_credentials(
                path,
                remote_name,
                username,
                password,
                options,
                lambda: self._run(args, path, options),
            )

        return await self._guarded(push_with_credentials, policy)

    async def pull(
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
        if not remote and not branch and not await self._has_upstream(path, options):
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
            return await self._guarded(lambda: self._run(args, path, options), policy)

        async def pull_with_credentials() -> CommandResult:
            remote_name = await self._remote_name(path, remote, options)
            args = pull_args(remote, branch, remote_name)
            return await self._with_remote_credentials(
                path,
                remote_name,
                username,
                password,
                options,
                lambda: self._run(args, path, options),
            )

        return await self._guarded(pull_with_credentials, policy)

    async def set_config(
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
        return await self._run(args, repo_path, options)

    async def get_config(
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
        return (await self._shell(command, options)).stdout.strip() or None

    async def dangerously_authenticate(
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
        await self.set_config(
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
        return await self._guarded(lambda: self._shell(command, options), policy)

    async def configure_user(
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
        await self.set_config(
            "user.name", name, scope, path, envs, user, cwd, timeout, request_timeout
        )
        return await self.set_config(
            "user.email", email, scope, path, envs, user, cwd, timeout, request_timeout
        )

    async def _run(
        self, args: Sequence[str], repo_path: str | None, options: GitCommandOptions
    ) -> CommandResult:
        return await self._shell(git_command(args, repo_path), options)

    async def _shell(self, command: str, options: GitCommandOptions) -> CommandResult:
        return await self._commands.run(
            command,
            envs=options.merged_envs(),
            user=options.user,
            cwd=options.cwd,
            timeout=options.timeout,
            request_timeout=options.request_timeout,
        )

    async def _has_upstream(self, path: str, options: GitCommandOptions) -> bool:
        try:
            return bool((await self._run(has_upstream_args(), path, options)).stdout.strip())
        except CommandExitException:
            return False

    async def _remote_name(self, path: str, remote: str | None, options: GitCommandOptions) -> str:
        if remote:
            return remote
        return resolve_remote_name(
            None, (await self._run(remote_list_args(), path, options)).stdout
        )

    async def _with_remote_credentials(
        self,
        path: str,
        remote: str,
        username: str,
        password: str,
        options: GitCommandOptions,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        """La URL original se restaura siempre. Si la operación falla, su
        error es el que sale aunque la restauración también falle (como en
        E2B); si sólo falla la restauración, sale ese error."""
        original = parse_remote_url(
            (await self._run(remote_get_url_args(remote), path, options)).stdout, remote
        )
        credentialed = with_credentials(original, username, password)
        restore = remote_set_url_args(remote, original)
        await self._run(remote_set_url_args(remote, credentialed), path, options)
        try:
            result = await operation()
        except BaseException:
            with contextlib.suppress(CommandExitException):
                await self._run(restore, path, options)
            raise
        await self._run(restore, path, options)
        return result

    async def _guarded(self, operation: Callable[[], Awaitable[T]], policy: FailurePolicy) -> T:
        """Traduce el `CommandExitException` según `policy`. Con secretos de
        por medio la excepción se lanza fuera del `except` y `from None`, así
        que ni `__cause__` ni `__context__` guardan el texto sin redactar."""
        try:
            return await operation()
        except CommandExitException as exc:
            failure = git_failure(exc, policy)
            if failure is exc:
                raise
            if not policy.secrets:
                raise failure from exc
        raise failure from None


__all__ = ["AsyncGit"]
