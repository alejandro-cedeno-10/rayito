"""`rayito.e2b.AsyncSandbox`: la superficie de `_sync.Sandbox` como
corrutinas sobre `rayito.AsyncSandbox` (`await AsyncSandbox.create(...)`,
`async with`), con los órdenes asíncronos de E2B 2.51 para `watch_dir`,
`pty.create`/`pty.connect` y `commands.connect(on_stdout, on_stderr)`.
`envd_api_url`, `envd_direct_url`, `traffic_access_token` y
`connection_config` son propiedades normales."""

from __future__ import annotations

import builtins
import logging
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from types import TracebackType
from typing import IO, Any, ClassVar, Literal, Self, Unpack, overload

from rayito import AsyncSandbox as NativeAsyncSandbox
from rayito._code_base import ContextLike, ErrorCallback, ResultCallback, StdoutCallback
from rayito._filesystem_base import EventCallback, ExitCallback
from rayito._limits import DEFAULT_PORT
from rayito._models import (
    AsyncUploadTicket,
    CodeContext,
    CommandResult,
    DownloadLink,
    EntryInfo,
    Execution,
    HostAccess,
    NetworkState,
    ProcessInfo,
    WriteEntry,
)
from rayito._process_base import OutputCallback
from rayito._pty_base import PtyDataCallback
from rayito._sandbox_base import (
    DEFAULT_READY_TIMEOUT_SECONDS,
    DEFAULT_RECONNECT_TIMEOUT_SECONDS,
    LoggingOption,
    PortLike,
    class_method_variant,
)
from rayito.e2b._compat import (
    NativeCall,
    class_metrics_unimplemented,
    history_falls_back_to_snapshot,
    info_from_native,
    lifecycle_unimplemented,
    list_item_to_info,
    list_mapping,
    map_create_kwargs,
    map_network_update,
    metrics_from_native,
    native_background,
    native_call_kwargs,
    native_stdin,
    needs_metrics_snapshot,
    normalized_language_or_unimplemented,
    pty_size_to_native,
    reject_callable_in_user_slot,
    resolve_metrics_token,
    unimplemented_language,
    validate_keep_memory,
    validate_on_resume,
)
from rayito.e2b._connection import (
    ApiParams,
    ConnectionConfig,
    ConnectionParams,
    snapshot_config,
    split_api_params,
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
    E2B_COMMAND_TIMEOUT_SECONDS,
    E2B_PTY_TIMEOUT_SECONDS,
    E2B_WATCH_TIMEOUT_SECONDS,
    EMPTY_PARAMS,
    MISSING_UPLOAD_PATH_MESSAGE,
    emit_warnings,
    request_timeout_of,
)
from rayito.e2b._unimplemented import UnimplementedMember
from rayito.exceptions import (
    InvalidArgumentException,
    LifecycleUnsupportedException,
    UnimplementedError,
)
from rayito.sandbox_async.commands import AsyncCommandHandle
from rayito.sandbox_async.commands import AsyncCommands as NativeAsyncCommands
from rayito.sandbox_async.filesystem import AsyncFilesystem as NativeAsyncFilesystem
from rayito.sandbox_async.filesystem import AsyncWatchHandle
from rayito.sandbox_async.git import AsyncGit
from rayito.sandbox_async.pty import AsyncPty as NativeAsyncPty
from rayito.sandbox_async.pty import AsyncPtyHandle


class AsyncSandbox:
    """Drop-in de `e2b_code_interpreter.AsyncSandbox` / `e2b.AsyncSandbox` (2.51)."""

    _bound_params: ClassVar[Mapping[str, Any]] = EMPTY_PARAMS

    def __init__(
        self, *, _native: NativeAsyncSandbox, _connection: ConnectionConfig | None = None
    ) -> None:
        self._native = _native
        self._connection = _connection
        self.commands = AsyncCommands(_native.commands)
        self.files = AsyncFilesystem(_native.files)
        self.pty = AsyncPty(_native.pty)

    @classmethod
    def _native_call(
        cls,
        native_kwargs: Mapping[str, Any],
        api_params: Mapping[str, Any],
        *,
        call: str,
        with_transport: bool = True,
        with_request_timeout: bool = True,
    ) -> NativeCall:
        resolved = native_call_kwargs(
            cls._bound_params,
            native_kwargs,
            api_params,
            call=call,
            with_transport=with_transport,
            with_request_timeout=with_request_timeout,
        )
        emit_warnings(resolved.warnings)
        return resolved

    @classmethod
    async def _launch(cls, create_kwargs: Mapping[str, Any], api_params: Mapping[str, Any]) -> Self:
        mapping = map_create_kwargs(**create_kwargs)
        resolved = cls._native_call(mapping.native_kwargs, api_params, call="create")
        emit_warnings(mapping.warnings)
        try:
            native = await NativeAsyncSandbox.create(**resolved.kwargs)
        except LifecycleUnsupportedException as exc:
            raise lifecycle_unimplemented() from exc
        config = snapshot_config(
            resolved.settings,
            region=native.region,
            logger=resolved.kwargs.get("logger"),
            integration=resolved.integration,
        )
        return cls(_native=native, _connection=config)

    # ------------------------------------------------------------ classmethods

    @classmethod
    async def create(
        cls,
        template: str | None = None,
        timeout: int | None = None,
        metadata: Mapping[str, str] | None = None,
        envs: Mapping[str, str] | None = None,
        secure: bool | None = None,
        allow_internet_access: bool | None = None,
        mcp: Any | None = None,
        network: Mapping[str, Any] | None = None,
        iam: Any | None = None,
        lifecycle: Mapping[str, Any] | None = None,
        volume_mounts: Any | None = None,
        logger: logging.Logger | None = None,
        *,
        max_lifetime: int | None = None,
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
        **api_params: Unpack[ApiParams],
    ) -> Self:
        """Misma tabla de kwargs que `rayito.e2b.Sandbox.create`."""
        return await cls._launch(
            {
                "template": template,
                "timeout": timeout,
                "metadata": metadata,
                "envs": envs,
                "secure": secure,
                "allow_internet_access": allow_internet_access,
                "mcp": mcp,
                "network": network,
                "iam": iam,
                "lifecycle": lifecycle,
                "volume_mounts": volume_mounts,
                "logger": logger,
                "max_lifetime": max_lifetime,
                "region": region,
                "session": session,
                "template_version": template_version,
                "execution_role_arn": execution_role_arn,
                "allowed_ports": allowed_ports,
                "ingress": ingress,
                "logging": logging,
                "access_token": access_token,
                "ready_timeout": ready_timeout,
                "reconnect_timeout": reconnect_timeout,
                "keep_on_failure": keep_on_failure,
                "control_plane": control_plane,
                "transport": transport,
            },
            dict(api_params),
        )

    @classmethod
    async def beta_create(
        cls,
        template: str | None = None,
        timeout: int | None = None,
        auto_pause: bool = False,
        allow_internet_access: bool | None = None,
        metadata: Mapping[str, str] | None = None,
        envs: Mapping[str, str] | None = None,
        secure: bool | None = None,
        mcp: Any | None = None,
        network: Mapping[str, Any] | None = None,
        *,
        lifecycle: Mapping[str, Any] | None = None,
        logger: logging.Logger | None = None,
        max_lifetime: int | None = None,
        **kwargs: Any,
    ) -> Self:
        """Misma semántica que `rayito.e2b.Sandbox.beta_create`."""
        api_params = {
            key: kwargs.pop(key) for key in list(kwargs) if key in ApiParams.__optional_keys__
        }
        return await cls._launch(
            {
                "template": template,
                "timeout": timeout,
                "metadata": metadata,
                "envs": envs,
                "secure": secure,
                "allow_internet_access": allow_internet_access,
                "mcp": mcp,
                "network": network,
                "lifecycle": lifecycle,
                "logger": logger,
                "max_lifetime": max_lifetime,
                "auto_pause": True if auto_pause else None,
                **kwargs,
            },
            api_params,
        )

    @class_method_variant("_class_connect")
    async def connect(
        self,
        timeout: int | None = None,
        *,
        on_resume: Literal["restore", "reboot"] = "restore",
        **api_params: Unpack[ApiParams],
    ) -> AsyncSandbox:
        """Misma semántica que `rayito.e2b.Sandbox.connect` (instancia)."""
        validate_on_resume(on_resume)
        request_timeout = request_timeout_of(api_params, call="connect")
        try:
            await self._native.connect(timeout=timeout, request_timeout=request_timeout)
        except LifecycleUnsupportedException as exc:
            raise lifecycle_unimplemented() from exc
        return self

    @classmethod
    async def _class_connect(
        cls,
        sandbox_id: str,
        timeout: int | None = None,
        *,
        on_resume: Literal["restore", "reboot"] = "restore",
        logger: logging.Logger | None = None,
        access_token: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS,
        control_plane: Any | None = None,
        transport: Any | None = None,
        **api_params: Unpack[ApiParams],
    ) -> Self:
        """Misma semántica que `rayito.e2b.Sandbox.connect(sandbox_id, timeout)`."""
        validate_on_resume(on_resume)
        resolved = cls._native_call(
            {
                "timeout": timeout,
                "logger": logger,
                "access_token": access_token,
                "region": region,
                "session": session,
                "ready_timeout": ready_timeout,
                "reconnect_timeout": reconnect_timeout,
                "control_plane": control_plane,
                "transport": transport,
            },
            api_params,
            call="connect",
        )
        try:
            native = await NativeAsyncSandbox.connect(sandbox_id, **resolved.kwargs)
        except LifecycleUnsupportedException as exc:
            raise lifecycle_unimplemented() from exc
        config = snapshot_config(
            resolved.settings,
            region=native.region,
            logger=logger,
            integration=resolved.integration,
        )
        return cls(_native=native, _connection=config)

    @classmethod
    async def list(
        cls,
        query: SandboxQuery | None = None,
        limit: int | None = None,
        next_token: str | None = None,
        *,
        state: Sequence[SandboxState] | None = None,
        order: Literal["asc", "desc"] | None = None,
        template: str | None = None,
        template_version: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        transport: Any | None = None,
        **api_params: Unpack[ApiParams],
    ) -> AsyncSandboxPaginator:
        """`AsyncSandboxPaginator` sobre `rayito.AsyncSandbox.paginate`."""
        mapping = list_mapping(query, state, template)
        resolved = cls._native_call(
            {
                "template": mapping.template,
                "template_version": template_version,
                "states": mapping.states,
                "metadata": mapping.metadata,
                "started_after": mapping.started_after,
                "order": order,
                "limit": limit,
                "next_token": next_token,
                "region": region,
                "session": session,
                "control_plane": control_plane,
                "transport": transport,
            },
            api_params,
            call="list",
        )
        return AsyncSandboxPaginator(
            NativeAsyncSandbox.paginate(**resolved.kwargs), mapper=list_item_to_info
        )

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
    def envd_api_url(self) -> str:
        """`https://<endpoint>`; toda petición necesita además `get_host(8080).headers`."""
        return self._native.endpoint_url

    @property
    def envd_direct_url(self) -> str:
        return self._native.endpoint_url

    @property
    def traffic_access_token(self) -> str:
        """El JWE del proxy vigente para el puerto 8080 de `rayd` (credencial
        al portador del endpoint, 60 min como mucho): nunca la loguees."""
        return self._native._current_jwe(DEFAULT_PORT)

    @property
    def connection_config(self) -> ConnectionConfig:
        if self._connection is None:
            self._connection = snapshot_config(
                split_api_params({}, call="connection_config")[0],
                region=self._native.region,
                logger=None,
                integration=ConnectionConfig.current_integration(),
            )
        return self._connection

    @property
    def git(self) -> AsyncGit:
        """El `rayito.AsyncGit` nativo (E2B lo llama `Git` en ambos SDKs)."""
        return self._native.git

    # --------------------------------------------------------------- lifecycle

    async def is_running(self, request_timeout: float | None = None) -> bool:
        return await self._native.is_running(request_timeout=request_timeout)

    @class_method_variant("_class_kill")
    async def kill(self, **api_params: Unpack[ApiParams]) -> bool:
        request_timeout_of(api_params, call="kill")
        return bool(await self._native.kill())

    @classmethod
    async def _class_kill(
        cls,
        sandbox_id: str,
        *,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        **api_params: Unpack[ApiParams],
    ) -> bool:
        resolved = cls._native_call(
            {"region": region, "session": session, "control_plane": control_plane},
            api_params,
            call="kill",
            with_transport=False,
            with_request_timeout=False,
        )
        return bool(await NativeAsyncSandbox.kill(sandbox_id, **resolved.kwargs))

    @class_method_variant("_class_get_info")
    async def get_info(self, request_timeout: float | None = None) -> SandboxInfo:
        """Misma semántica que `rayito.e2b.Sandbox.get_info`."""
        info = await self._native.get_info()
        network, network_read = await self._read_network(info.state, request_timeout)
        return info_from_native(info, network=network, network_read=network_read)

    async def _read_network(
        self, state: str, request_timeout: float | None
    ) -> tuple[NetworkState | None, bool]:
        if state != "RUNNING":
            return None, False
        try:
            return await self._native.get_network(request_timeout=request_timeout), True
        except UnimplementedError:
            return None, True

    @classmethod
    async def _class_get_info(
        cls,
        sandbox_id: str,
        request_timeout: float | None = None,
        *,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        transport: Any | None = None,
        **api_params: Unpack[ConnectionParams],
    ) -> SandboxInfo:
        resolved = cls._native_call(
            {
                "region": region,
                "session": session,
                "control_plane": control_plane,
                "transport": transport,
            },
            {**api_params, "request_timeout": request_timeout},
            call="get_info",
        )
        return info_from_native(await NativeAsyncSandbox.get_info(sandbox_id, **resolved.kwargs))

    @class_method_variant("_class_set_timeout")
    async def set_timeout(self, timeout: int, request_timeout: float | None = None) -> None:
        """Misma semántica que `rayito.e2b.Sandbox.set_timeout`."""
        await self._native.set_timeout(timeout, request_timeout=request_timeout)

    @classmethod
    async def _class_set_timeout(
        cls,
        sandbox_id: str,
        timeout: int,
        request_timeout: float | None = None,
        *,
        access_token: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        transport: Any | None = None,
        **api_params: Unpack[ConnectionParams],
    ) -> None:
        resolved = cls._native_call(
            {
                "access_token": access_token,
                "region": region,
                "session": session,
                "control_plane": control_plane,
                "transport": transport,
            },
            {**api_params, "request_timeout": request_timeout},
            call="set_timeout",
        )
        await NativeAsyncSandbox.set_timeout(sandbox_id, timeout, **resolved.kwargs)

    @class_method_variant("_class_get_metrics")
    async def get_metrics(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        request_timeout: float | None = None,
    ) -> builtins.list[SandboxMetrics]:
        """Misma semántica que `rayito.e2b.Sandbox.get_metrics`."""
        ranged = start is not None or end is not None
        try:
            samples = await self._native.get_metrics_history(
                start=start, end=end, request_timeout=request_timeout
            )
        except UnimplementedError as exc:
            if not history_falls_back_to_snapshot(exc, ranged=ranged):
                raise
            samples = []
        if needs_metrics_snapshot(samples, ranged=ranged):
            return [
                metrics_from_native(await self._native.get_metrics(request_timeout=request_timeout))
            ]
        return [metrics_from_native(sample) for sample in samples]

    @classmethod
    async def _class_get_metrics(
        cls,
        sandbox_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        request_timeout: float | None = None,
        *,
        access_token: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        transport: Any | None = None,
        **api_params: Unpack[ConnectionParams],
    ) -> builtins.list[SandboxMetrics]:
        token = resolve_metrics_token(access_token, os.environ)
        resolved = cls._native_call(
            {
                "region": region,
                "session": session,
                "control_plane": control_plane,
                "transport": transport,
            },
            {**api_params, "request_timeout": request_timeout},
            call="get_metrics",
        )
        try:
            samples = await NativeAsyncSandbox.get_metrics_history(
                sandbox_id, access_token=token, start=start, end=end, **resolved.kwargs
            )
        except UnimplementedError as exc:
            mapped = class_metrics_unimplemented(exc)
            if mapped is None:
                raise
            raise mapped from exc
        return [metrics_from_native(sample) for sample in samples]

    @class_method_variant("_class_pause")
    async def pause(self, keep_memory: bool | None = None, **api_params: Unpack[ApiParams]) -> bool:
        """Misma semántica que `rayito.e2b.Sandbox.pause`."""
        validate_keep_memory(keep_memory)
        request_timeout_of(api_params, call="pause")
        return await self._native.pause(wait=True)

    @classmethod
    async def _class_pause(
        cls,
        sandbox_id: str,
        keep_memory: bool | None = None,
        *,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        **api_params: Unpack[ApiParams],
    ) -> bool:
        validate_keep_memory(keep_memory)
        resolved = cls._native_call(
            {"region": region, "session": session, "control_plane": control_plane},
            api_params,
            call="pause",
            with_transport=False,
            with_request_timeout=False,
        )
        return await NativeAsyncSandbox.pause(sandbox_id, **resolved.kwargs)

    beta_pause = pause

    @class_method_variant("_class_update_network")
    async def update_network(
        self,
        network: Mapping[str, Any] | None = None,
        request_timeout: float | None = None,
    ) -> None:
        """Misma semántica que `rayito.e2b.Sandbox.update_network`."""
        policy, allow_internet_access = map_network_update(network)
        await self._native.update_network(
            policy, allow_internet_access=allow_internet_access, request_timeout=request_timeout
        )

    @classmethod
    async def _class_update_network(
        cls,
        sandbox_id: str,
        network: Mapping[str, Any] | None = None,
        *,
        access_token: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        transport: Any | None = None,
        **api_params: Unpack[ApiParams],
    ) -> None:
        policy, allow_internet_access = map_network_update(network)
        resolved = cls._native_call(
            {
                "access_token": access_token,
                "region": region,
                "session": session,
                "control_plane": control_plane,
                "transport": transport,
            },
            api_params,
            call="update_network",
        )
        await NativeAsyncSandbox.update_network(
            sandbox_id, policy, allow_internet_access=allow_internet_access, **resolved.kwargs
        )

    fork = UnimplementedMember("fork")
    create_snapshot = UnimplementedMember("create_snapshot")
    list_snapshots = UnimplementedMember("list_snapshots")
    delete_snapshot = UnimplementedMember("delete_snapshot")
    get_mcp_url = UnimplementedMember("get_mcp_url")
    get_mcp_token = UnimplementedMember("get_mcp_token")

    async def upload_url(
        self,
        path: str | None = None,
        user: str | None = None,
        use_signature: bool = False,
        use_signature_expiration: int | None = None,
    ) -> AsyncUploadTicket:
        """Misma semántica que `rayito.e2b.Sandbox.upload_url`; es corrutina
        porque armar la importación es una llamada al agente (divergencia
        documentada: la de E2B es síncrona)."""
        if path is None:
            raise InvalidArgumentException(MISSING_UPLOAD_PATH_MESSAGE)
        return await self._native.upload_url(
            path, user=user, use_signature_expiration=use_signature_expiration
        )

    async def download_url(
        self,
        path: str,
        user: str | None = None,
        use_signature: bool = False,
        use_signature_expiration: int | None = None,
    ) -> DownloadLink:
        """Misma semántica que `rayito.e2b.Sandbox.download_url`."""
        return await self._native.download_url(
            path, user=user, use_signature_expiration=use_signature_expiration
        )

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
        """Mismo contrato de `language` que `rayito.e2b.Sandbox.run_code`."""
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
        try:
            return await self._native.run_code(code, **kwargs)
        except InvalidArgumentException as exc:
            mapped = unimplemented_language(exc, "run_code", language)
            if mapped is None:
                raise
            raise mapped from exc

    async def create_code_context(
        self,
        cwd: str | None = None,
        language: str | None = None,
        request_timeout: float | None = None,
    ) -> CodeContext:
        """Mismo contrato de `language` que `rayito.e2b.Sandbox.create_code_context`."""
        canonical = normalized_language_or_unimplemented(language, "create_code_context")
        try:
            return await self._native.create_code_context(
                cwd=cwd, language=canonical, request_timeout=request_timeout
            )
        except InvalidArgumentException as exc:
            mapped = unimplemented_language(exc, "create_code_context", language)
            if mapped is None:
                raise
            raise mapped from exc

    async def list_code_contexts(
        self, request_timeout: float | None = None
    ) -> builtins.list[CodeContext]:
        return await self._native.list_code_contexts(request_timeout=request_timeout)

    async def remove_code_context(
        self, context: CodeContext | str, request_timeout: float | None = None
    ) -> None:
        await self._native.remove_code_context(context, request_timeout=request_timeout)

    async def restart_code_context(
        self, context: CodeContext | str, request_timeout: float | None = None
    ) -> None:
        await self._native.restart_code_context(context, request_timeout=request_timeout)

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


class AsyncCommands:
    """`sbx.commands` asíncrono con las firmas posicionales de E2B 2.x."""

    def __init__(self, native: NativeAsyncCommands) -> None:
        self._native = native

    @property
    def native(self) -> NativeAsyncCommands:
        return self._native

    @overload
    async def run(
        self,
        cmd: str,
        background: Literal[False] | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
        stdin: bool | None = None,
        timeout: float | None = E2B_COMMAND_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> CommandResult: ...

    @overload
    async def run(
        self,
        cmd: str,
        background: Literal[True],
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
        stdin: bool | None = None,
        timeout: float | None = E2B_COMMAND_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> AsyncCommandHandle: ...

    async def run(
        self,
        cmd: str,
        background: bool | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
        stdin: bool | None = None,
        timeout: float | None = E2B_COMMAND_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> CommandResult | AsyncCommandHandle:
        return await self._native.run(
            cmd,
            background=native_background(background),
            envs=envs,
            user=user,
            cwd=cwd,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            stdin=native_stdin(stdin),
            timeout=timeout,
            request_timeout=request_timeout,
        )

    async def connect(
        self,
        pid: int,
        timeout: float | None = E2B_COMMAND_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
    ) -> AsyncCommandHandle:
        return await self._native.connect(
            pid,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            timeout=timeout,
            request_timeout=request_timeout,
        )

    async def list(self, request_timeout: float | None = None) -> builtins.list[ProcessInfo]:
        return await self._native.list(request_timeout=request_timeout)

    async def kill(self, pid: int, request_timeout: float | None = None) -> bool:
        return await self._native.kill(pid, request_timeout=request_timeout)

    async def send_stdin(
        self, pid: int, data: str | bytes, request_timeout: float | None = None
    ) -> None:
        await self._native.send_stdin(pid, data, request_timeout=request_timeout)

    async def close_stdin(self, pid: int, request_timeout: float | None = None) -> None:
        await self._native.close_stdin(pid, request_timeout=request_timeout)


class AsyncFilesystem:
    """`sbx.files` asíncrono con las firmas de E2B 2.x."""

    def __init__(self, native: NativeAsyncFilesystem) -> None:
        self._native = native

    @overload
    async def write(
        self,
        path: str,
        data: str | bytes | IO[bytes] | IO[str],
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        metadata: Mapping[str, str] | None = None,
        use_octet_stream: bool = False,
    ) -> EntryInfo: ...

    @overload
    async def write(
        self,
        path: Sequence[WriteEntry],
        data: None = None,
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        metadata: Mapping[str, str] | None = None,
        use_octet_stream: bool = False,
    ) -> builtins.list[EntryInfo]: ...

    async def write(
        self,
        path: str | Sequence[WriteEntry],
        data: str | bytes | IO[bytes] | IO[str] | None = None,
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        metadata: Mapping[str, str] | None = None,
        use_octet_stream: bool = False,
    ) -> EntryInfo | builtins.list[EntryInfo]:
        if isinstance(path, str):
            if data is None:
                raise TypeError("write(path, data): falta data")
            return await self._native.write(
                path,
                data,
                user=user,
                gzip=gzip,
                metadata=metadata,
                use_octet_stream=use_octet_stream,
                request_timeout=request_timeout,
            )
        return await self.write_files(
            path,
            user,
            request_timeout,
            gzip=gzip,
            metadata=metadata,
            use_octet_stream=use_octet_stream,
        )

    async def write_files(
        self,
        files: Sequence[WriteEntry],
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        metadata: Mapping[str, str] | None = None,
        use_octet_stream: bool = False,
    ) -> builtins.list[EntryInfo]:
        return await self._native.write_files(
            files,
            user=user,
            gzip=gzip,
            metadata=metadata,
            use_octet_stream=use_octet_stream,
            request_timeout=request_timeout,
        )

    @overload
    async def read(
        self,
        path: str,
        format: Literal["text"] = "text",
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        stream_idle_timeout: float | None = None,
    ) -> str: ...

    @overload
    async def read(
        self,
        path: str,
        format: Literal["bytes"],
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        stream_idle_timeout: float | None = None,
    ) -> bytes: ...

    @overload
    async def read(
        self,
        path: str,
        format: Literal["stream"],
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        stream_idle_timeout: float | None = None,
    ) -> AsyncIterator[bytes]: ...

    async def read(
        self,
        path: str,
        format: Literal["text", "bytes", "stream"] = "text",
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        stream_idle_timeout: float | None = None,
    ) -> str | bytes | AsyncIterator[bytes]:
        return await self._native.read(
            path,
            format=format,
            gzip=gzip,
            stream_idle_timeout=stream_idle_timeout,
            user=user,
            request_timeout=request_timeout,
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
        on_event: EventCallback,
        on_exit: ExitCallback | None = None,
        user: str | None = None,
        request_timeout: float | None = None,
        timeout: float | None = E2B_WATCH_TIMEOUT_SECONDS,
        recursive: bool = False,
        include_entry: bool = False,
        allow_network_mounts: bool = False,
    ) -> AsyncWatchHandle:
        """`watch_dir` asíncrono de E2B 2.x: `on_event` posicional y 60 s de
        vida por defecto (termina en `TimeoutException`); `0`/`None` es
        ilimitado. `allow_network_mounts` se acepta sin efecto."""
        return await self._native.watch_dir(
            path,
            on_event=on_event,
            on_exit=on_exit,
            recursive=recursive,
            include_entry=include_entry,
            user=user,
            timeout=timeout,
            request_timeout=request_timeout,
        )


class AsyncPty:
    """`sbx.pty` asíncrono: `create(size, on_data, ...)` y `connect(pid, on_data, ...)`."""

    def __init__(self, native: NativeAsyncPty) -> None:
        self._native = native

    async def create(
        self,
        size: PtySize,
        on_data: PtyDataCallback | None,
        user: str | None = None,
        cwd: str | None = None,
        envs: Mapping[str, str] | None = None,
        timeout: float | None = E2B_PTY_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> AsyncPtyHandle:
        reject_callable_in_user_slot(user, "pty.create")
        return await self._native.create(
            size=pty_size_to_native(size),
            user=user,
            cwd=cwd,
            envs=envs,
            on_data=on_data,
            timeout=timeout,
            request_timeout=request_timeout,
        )

    async def connect(
        self,
        pid: int,
        on_data: PtyDataCallback | None,
        timeout: float | None = E2B_PTY_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> AsyncPtyHandle:
        return await self._native.connect(
            pid, from_seq=0, on_data=on_data, timeout=timeout, request_timeout=request_timeout
        )

    async def send_stdin(self, pid: int, data: bytes, request_timeout: float | None = None) -> None:
        await self._native.send_input(pid, data, request_timeout=request_timeout)

    async def resize(self, pid: int, size: PtySize, request_timeout: float | None = None) -> None:
        await self._native.resize(pid, pty_size_to_native(size), request_timeout=request_timeout)

    async def kill(self, pid: int, request_timeout: float | None = None) -> bool:
        return await self._native.kill(pid, request_timeout=request_timeout)
