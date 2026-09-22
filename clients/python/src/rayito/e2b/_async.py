"""`rayito.e2b.AsyncSandbox`: la superficie de `_sync.Sandbox` como
corrutinas sobre `rayito.AsyncSandbox` (`await AsyncSandbox.create(...)`,
`async with`)."""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Mapping, Sequence
from types import TracebackType
from typing import IO, Any, Literal, Self, overload

from rayito import AsyncSandbox as NativeAsyncSandbox
from rayito._code_base import ContextLike, ErrorCallback, ResultCallback, StdoutCallback
from rayito._filesystem_base import EventCallback, ExitCallback
from rayito._models import CodeContext, EntryInfo, Execution, HostAccess, WriteEntry
from rayito._pty_base import PtyDataCallback
from rayito._sandbox_base import (
    DEFAULT_READY_TIMEOUT_SECONDS,
    DEFAULT_RECONNECT_TIMEOUT_SECONDS,
    LoggingOption,
    PortLike,
    class_method_variant,
)
from rayito.e2b._compat import (
    ignored_kwarg_warnings,
    info_from_native,
    map_create_kwargs,
    metrics_from_native,
    normalized_language_or_unimplemented,
    pty_size_to_native,
    states_for,
    unimplemented,
)
from rayito.e2b._models import (
    AsyncSandboxPaginator,
    PtySize,
    SandboxInfo,
    SandboxMetrics,
    SandboxQuery,
    SandboxState,
)
from rayito.e2b._sync import (
    BETA_CREATE_REASON,
    CLASS_METRICS_REASON,
    CONNECTION_CONFIG_REASON,
    E2B_PTY_TIMEOUT_SECONDS,
    E2B_WATCH_TIMEOUT_SECONDS,
    METRICS_RANGE_REASON,
    NEXT_TOKEN_REASON,
    SET_TIMEOUT_REASON,
    SIGNED_URL_REASON,
    emit_warnings,
)
from rayito.sandbox_async.commands import AsyncCommands
from rayito.sandbox_async.filesystem import AsyncFilesystem as NativeAsyncFilesystem
from rayito.sandbox_async.filesystem import AsyncWatchHandle
from rayito.sandbox_async.pty import AsyncPty as NativeAsyncPty
from rayito.sandbox_async.pty import AsyncPtyHandle


class AsyncSandbox:
    """Drop-in de `e2b_code_interpreter.AsyncSandbox` / `e2b.AsyncSandbox`."""

    def __init__(self, *, _native: NativeAsyncSandbox) -> None:
        self._native = _native
        self.commands: AsyncCommands = _native.commands
        self.files = AsyncFilesystem(_native.files)
        self.pty = AsyncPty(_native.pty)

    @classmethod
    async def create(
        cls,
        template: str | None = None,
        timeout: int | None = None,
        metadata: Mapping[str, str] | None = None,
        envs: Mapping[str, str] | None = None,
        api_key: str | None = None,
        domain: str | None = None,
        debug: bool = False,
        request_timeout: float | None = None,
        proxy: str | None = None,
        secure: bool = True,
        allow_internet_access: bool = True,
        *,
        region: str | None = None,
        session: Any | None = None,
        template_version: str | None = None,
        execution_role_arn: str | None = None,
        allowed_ports: Sequence[PortLike] | None = None,
        ingress: Sequence[str] | None = None,
        logging: LoggingOption = "disabled",
        access_token: str | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS,
        keep_on_failure: bool = False,
        control_plane: Any | None = None,
        transport: Any | None = None,
    ) -> Self:
        """Misma tabla de kwargs que `rayito.e2b.Sandbox`."""
        mapping = map_create_kwargs(
            template=template,
            timeout=timeout,
            metadata=metadata,
            envs=envs,
            api_key=api_key,
            domain=domain,
            debug=debug,
            request_timeout=request_timeout,
            proxy=proxy,
            secure=secure,
            allow_internet_access=allow_internet_access,
            region=region,
            session=session,
            template_version=template_version,
            execution_role_arn=execution_role_arn,
            allowed_ports=allowed_ports,
            ingress=ingress,
            logging=logging,
            access_token=access_token,
            ready_timeout=ready_timeout,
            reconnect_timeout=reconnect_timeout,
            keep_on_failure=keep_on_failure,
            control_plane=control_plane,
            transport=transport,
        )
        emit_warnings(mapping.warnings)
        return cls(_native=await NativeAsyncSandbox.create(**mapping.native_kwargs))

    @classmethod
    async def connect(
        cls,
        sandbox_id: str,
        *,
        api_key: str | None = None,
        domain: str | None = None,
        debug: bool = False,
        request_timeout: float | None = None,
        proxy: str | None = None,
        access_token: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS,
        control_plane: Any | None = None,
        transport: Any | None = None,
    ) -> Self:
        emit_warnings(
            ignored_kwarg_warnings(
                api_key=api_key, domain=domain, debug=debug, proxy=proxy, secure=True
            )
        )
        kwargs: dict[str, Any] = {
            "access_token": access_token,
            "region": region,
            "session": session,
            "ready_timeout": ready_timeout,
            "reconnect_timeout": reconnect_timeout,
            "control_plane": control_plane,
            "transport": transport,
        }
        if request_timeout is not None:
            kwargs["request_timeout"] = request_timeout
        return cls(_native=await NativeAsyncSandbox.connect(sandbox_id, **kwargs))

    @classmethod
    async def beta_create(
        cls,
        *args: Any,
        auto_pause: bool | None = None,
        network: Any | None = None,
        mcp: Any | None = None,
        **kwargs: Any,
    ) -> Self:
        if auto_pause is not None or network is not None or mcp is not None:
            raise unimplemented("beta_create(auto_pause=, network=, mcp=)", BETA_CREATE_REASON)
        return await cls.create(*args, **kwargs)

    @classmethod
    async def list(
        cls,
        *,
        api_key: str | None = None,
        query: SandboxQuery | None = None,
        state: Sequence[SandboxState] | None = None,
        limit: int | None = None,
        next_token: str | None = None,
        domain: str | None = None,
        debug: bool = False,
        request_timeout: float | None = None,
        proxy: str | None = None,
        template: str | None = None,
        template_version: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        transport: Any | None = None,
    ) -> AsyncSandboxPaginator:
        """`AsyncSandboxPaginator`; la lista nativa se recoge en la primera
        `await paginator.next_items()`."""
        if next_token is not None:
            raise unimplemented("list(next_token=...)", NEXT_TOKEN_REASON)
        states = states_for(state, query)
        emit_warnings(
            ignored_kwarg_warnings(
                api_key=api_key, domain=domain, debug=debug, proxy=proxy, secure=True
            )
        )
        kwargs: dict[str, Any] = {
            "template": template,
            "template_version": template_version,
            "states": states,
            "metadata": None if query is None else query.metadata,
            "region": region,
            "session": session,
            "control_plane": control_plane,
            "transport": transport,
        }
        if request_timeout is not None:
            kwargs["request_timeout"] = request_timeout

        async def collect() -> builtins.list[SandboxInfo]:
            return [info_from_native(item) for item in await NativeAsyncSandbox.list(**kwargs)]

        return AsyncSandboxPaginator(collect, limit=limit)

    # -------------------------------------------------------------- properties

    @property
    def native(self) -> NativeAsyncSandbox:
        return self._native

    @property
    def sandbox_id(self) -> str:
        return self._native.sandbox_id

    @property
    def sandbox_domain(self) -> str:
        return self._native.endpoint

    @property
    def connection_config(self) -> Any:
        raise unimplemented("connection_config", CONNECTION_CONFIG_REASON)

    # --------------------------------------------------------------- lifecycle

    async def is_running(self) -> bool:
        return await self._native.is_running()

    @class_method_variant("_class_kill")
    async def kill(self) -> bool:
        return bool(await self._native.kill())

    @classmethod
    async def _class_kill(cls, sandbox_id: str, **kwargs: Any) -> bool:
        return bool(await NativeAsyncSandbox.kill(sandbox_id, **kwargs))

    @class_method_variant("_class_get_info")
    async def get_info(self, request_timeout: float | None = None) -> SandboxInfo:
        return info_from_native(await self._native.get_info())

    @classmethod
    async def _class_get_info(
        cls, sandbox_id: str, request_timeout: float | None = None, **kwargs: Any
    ) -> SandboxInfo:
        if request_timeout is not None:
            kwargs["request_timeout"] = request_timeout
        return info_from_native(await NativeAsyncSandbox.get_info(sandbox_id, **kwargs))

    @class_method_variant("_class_set_timeout")
    async def set_timeout(self, timeout: int, request_timeout: float | None = None) -> None:
        raise unimplemented("set_timeout", SET_TIMEOUT_REASON)

    @classmethod
    async def _class_set_timeout(
        cls, sandbox_id: str, timeout: int, request_timeout: float | None = None, **kwargs: Any
    ) -> None:
        raise unimplemented("set_timeout", SET_TIMEOUT_REASON)

    @class_method_variant("_class_get_metrics")
    async def get_metrics(
        self,
        start: int | None = None,
        end: int | None = None,
        request_timeout: float | None = None,
    ) -> builtins.list[SandboxMetrics]:
        if start is not None or end is not None:
            raise unimplemented("get_metrics(start=, end=)", METRICS_RANGE_REASON)
        metrics = await self._native.get_metrics(request_timeout=request_timeout)
        return [metrics_from_native(metrics)]

    @classmethod
    async def _class_get_metrics(
        cls, sandbox_id: str, *args: Any, **kwargs: Any
    ) -> builtins.list[SandboxMetrics]:
        raise unimplemented("Sandbox.get_metrics(sandbox_id)", CLASS_METRICS_REASON)

    @class_method_variant("_class_pause")
    async def pause(self) -> str:
        await self._native.pause(wait=True)
        return self.sandbox_id

    @classmethod
    async def _class_pause(cls, sandbox_id: str, **kwargs: Any) -> str:
        await NativeAsyncSandbox.pause(sandbox_id, **kwargs)
        return sandbox_id

    beta_pause = pause

    async def upload_url(
        self,
        path: str | None = None,
        user: str | None = None,
        use_signature: bool = False,
        use_signature_expiration: int | None = None,
    ) -> str:
        raise unimplemented("upload_url", SIGNED_URL_REASON)

    async def download_url(
        self,
        path: str,
        user: str | None = None,
        use_signature: bool = False,
        use_signature_expiration: int | None = None,
    ) -> str:
        raise unimplemented("download_url", SIGNED_URL_REASON)

    async def get_host(self, port: int) -> HostAccess:
        return await self._native.get_host(port)

    # -------------------------------------------------------------------- code

    async def run_code(
        self,
        code: str,
        language: str | None = None,
        context: ContextLike | None = None,
        on_stdout: StdoutCallback | None = None,
        on_stderr: StdoutCallback | None = None,
        on_result: ResultCallback | None = None,
        on_error: ErrorCallback | None = None,
        envs: Mapping[str, str] | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> Execution:
        canonical = normalized_language_or_unimplemented(language, "run_code")
        kwargs: dict[str, Any] = {
            "language": canonical,
            "context": context,
            "on_stdout": on_stdout,
            "on_stderr": on_stderr,
            "on_result": on_result,
            "on_error": on_error,
            "envs": envs,
            "request_timeout": request_timeout,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        return await self._native.run_code(code, **kwargs)

    async def create_code_context(
        self,
        cwd: str | None = None,
        language: str | None = None,
        request_timeout: float | None = None,
    ) -> CodeContext:
        canonical = normalized_language_or_unimplemented(language, "create_code_context")
        return await self._native.create_code_context(
            cwd=cwd, language=canonical, request_timeout=request_timeout
        )

    # --------------------------------------------------------- context manager

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.kill()

    def __repr__(self) -> str:
        return f"e2b.AsyncSandbox(sandbox_id={self.sandbox_id!r})"


class AsyncFilesystem:
    """`sbx.files` async con las firmas de E2B."""

    def __init__(self, native: NativeAsyncFilesystem) -> None:
        self._native = native

    @overload
    async def write(
        self,
        path: str,
        data: str | bytes | IO[bytes] | IO[str],
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> EntryInfo: ...

    @overload
    async def write(
        self,
        path: Sequence[WriteEntry],
        data: None = None,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> builtins.list[EntryInfo]: ...

    async def write(
        self,
        path: str | Sequence[WriteEntry],
        data: str | bytes | IO[bytes] | IO[str] | None = None,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> EntryInfo | builtins.list[EntryInfo]:
        if isinstance(path, str):
            if data is None:
                raise TypeError("write(path, data): falta data")
            return await self._native.write(path, data, user=user, request_timeout=request_timeout)
        return await self._native.write_files(path, user=user, request_timeout=request_timeout)

    @overload
    async def read(
        self,
        path: str,
        format: Literal["text"] = "text",
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> str: ...

    @overload
    async def read(
        self,
        path: str,
        format: Literal["bytes"],
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> bytes: ...

    @overload
    async def read(
        self,
        path: str,
        format: Literal["stream"],
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> AsyncIterator[bytes]: ...

    async def read(
        self,
        path: str,
        format: Literal["text", "bytes", "stream"] = "text",
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> str | bytes | AsyncIterator[bytes]:
        return await self._native.read(
            path, format=format, user=user, request_timeout=request_timeout
        )

    async def list(
        self,
        path: str,
        depth: int = 1,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> builtins.list[EntryInfo]:
        return await self._native.list(
            path, depth=depth, user=user, request_timeout=request_timeout
        )

    async def exists(
        self, path: str, user: str | None = None, request_timeout: float | None = None
    ) -> bool:
        return await self._native.exists(path, user=user, request_timeout=request_timeout)

    async def get_info(
        self, path: str, user: str | None = None, request_timeout: float | None = None
    ) -> EntryInfo:
        return await self._native.get_info(path, user=user, request_timeout=request_timeout)

    async def remove(
        self, path: str, user: str | None = None, request_timeout: float | None = None
    ) -> None:
        await self._native.remove(path, user=user, request_timeout=request_timeout)

    async def rename(
        self,
        old_path: str,
        new_path: str,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> EntryInfo:
        return await self._native.rename(
            old_path, new_path, user=user, request_timeout=request_timeout
        )

    async def make_dir(
        self, path: str, user: str | None = None, request_timeout: float | None = None
    ) -> bool:
        return await self._native.make_dir(path, user=user, request_timeout=request_timeout)

    async def watch_dir(
        self,
        path: str,
        on_event: EventCallback | None = None,
        on_exit: ExitCallback | None = None,
        user: str | None = None,
        request_timeout: float | None = None,
        timeout: float | None = E2B_WATCH_TIMEOUT_SECONDS,
        recursive: bool = False,
    ) -> AsyncWatchHandle:
        return await self._native.watch_dir(
            path,
            on_event=on_event,
            on_exit=on_exit,
            recursive=recursive,
            user=user,
            timeout=timeout,
            request_timeout=request_timeout,
        )


class AsyncPty:
    """`sbx.pty` async con `PtySize(rows, cols)` de E2B y `send_stdin`."""

    def __init__(self, native: NativeAsyncPty) -> None:
        self._native = native

    async def create(
        self,
        size: PtySize,
        on_data: PtyDataCallback | None = None,
        user: str | None = None,
        cwd: str | None = None,
        envs: Mapping[str, str] | None = None,
        timeout: float | None = E2B_PTY_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> AsyncPtyHandle:
        return await self._native.create(
            size=pty_size_to_native(size),
            user=user,
            cwd=cwd,
            envs=envs,
            on_data=on_data,
            timeout=timeout,
            request_timeout=request_timeout,
        )

    async def send_stdin(self, pid: int, data: bytes, request_timeout: float | None = None) -> None:
        await self._native.send_input(pid, data, request_timeout=request_timeout)

    async def resize(self, pid: int, size: PtySize, request_timeout: float | None = None) -> None:
        await self._native.resize(pid, pty_size_to_native(size), request_timeout=request_timeout)

    async def kill(self, pid: int, request_timeout: float | None = None) -> bool:
        return await self._native.kill(pid, request_timeout=request_timeout)
